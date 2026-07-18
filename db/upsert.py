"""Idempotent writes for the scraper. Every function here is safe to call
repeatedly with the same data -- re-running a scrape must never create
duplicate rows.
"""
from datetime import datetime, timezone

from config import ROUNDS_PER_SEASON


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _status_for_round_count(round_count):
    return "previous" if round_count >= ROUNDS_PER_SEASON else "current"


def get_or_create_season(conn, fingerprint, round_count):
    """Look up a season by its fingerprint (stable identity, see
    scraper/fingerprint.py). Create it if new, otherwise refresh
    last_seen/round_count/status. Returns season_id.

    Status is derived from the best round_count ever observed for this
    fingerprint, never from a single pass in isolation -- a carousel walk
    that gets cut short (flaky click, slow animation) must not be able to
    demote a season that's already been confirmed complete back to
    'current'. round_count itself never regresses for the same reason.
    'archived' is handled separately in run.py, once a season stops
    appearing in a scrape at all.
    """
    ts = now_iso()
    row = conn.execute(
        "SELECT season_id, round_count, status FROM seasons WHERE fingerprint = ?",
        (fingerprint,),
    ).fetchone()

    if row is None:
        status = _status_for_round_count(round_count)
        cur = conn.execute(
            """INSERT INTO seasons (fingerprint, status, first_seen, last_seen, round_count)
               VALUES (?, ?, ?, ?, ?)""",
            (fingerprint, status, ts, ts, round_count),
        )
        return cur.lastrowid

    season_id = row["season_id"]
    effective_round_count = max(round_count, row["round_count"])
    status = row["status"] if row["status"] == "archived" else _status_for_round_count(effective_round_count)
    conn.execute(
        """UPDATE seasons SET status = ?, last_seen = ?, round_count = ?
           WHERE season_id = ?""",
        (status, ts, effective_round_count, season_id),
    )
    return season_id


def archive_missing_seasons(conn, touched_season_ids):
    """Only two seasons are ever visible on the site at once. Any season
    we'd previously seen (status current/previous) that this run's scrape
    didn't touch at all has dropped off the site -- preserve it as
    'archived' rather than leaving a stale 'current'/'previous' label.
    Returns the list of season_ids just archived.
    """
    placeholders = ",".join("?" for _ in touched_season_ids) or "NULL"
    rows = conn.execute(
        f"""SELECT season_id FROM seasons
            WHERE status IN ('current', 'previous')
              AND season_id NOT IN ({placeholders})""",
        touched_season_ids,
    ).fetchall()
    archived = [r["season_id"] for r in rows]
    if archived:
        conn.executemany(
            "UPDATE seasons SET status = 'archived' WHERE season_id = ?",
            [(sid,) for sid in archived],
        )
    return archived


def upsert_matches(conn, season_id, matches):
    """matches: list of dicts with round_number, match_number, team_a, team_b,
    ft_a, ft_b, ht_a, ht_b. Returns count of newly inserted rows.
    """
    ts = now_iso()
    inserted = 0
    for m in matches:
        cur = conn.execute(
            """INSERT OR IGNORE INTO matches
               (season_id, round_number, match_number, team_a, team_b,
                ft_a, ft_b, ht_a, ht_b, played_at, scraped_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                season_id,
                m["round_number"],
                m["match_number"],
                m["team_a"],
                m["team_b"],
                m.get("ft_a"),
                m.get("ft_b"),
                m.get("ht_a"),
                m.get("ht_b"),
                m.get("played_at"),
                ts,
            ),
        )
        if cur.rowcount:
            inserted += 1
    return inserted


def make_fixture_key(round_number, match_number, team_a, team_b):
    return f"{round_number}:{match_number}:{team_a}:{team_b}"


def upsert_fixture(conn, season_id, round_number, match_number, team_a, team_b, kickoff_time=None):
    """Returns (fixture_id, was_new). Idempotent on fixture_key
    (round/match/teams) -- season_id may be resolved later than the
    fixture itself is first seen, so a second scrape can attach it without
    creating a duplicate row.
    """
    ts = now_iso()
    key = make_fixture_key(round_number, match_number, team_a, team_b)

    row = conn.execute(
        "SELECT fixture_id FROM fixtures WHERE fixture_key = ?", (key,)
    ).fetchone()

    if row is None:
        cur = conn.execute(
            """INSERT INTO fixtures
               (season_id, round_number, match_number, team_a, team_b,
                kickoff_time, scraped_at, fixture_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (season_id, round_number, match_number, team_a, team_b, kickoff_time, ts, key),
        )
        return cur.lastrowid, True

    fixture_id = row["fixture_id"]
    conn.execute(
        """UPDATE fixtures SET season_id = COALESCE(?, season_id),
           kickoff_time = COALESCE(?, kickoff_time), scraped_at = ?
           WHERE fixture_id = ?""",
        (season_id, kickoff_time, ts, fixture_id),
    )
    return fixture_id, False


def insert_odds(conn, fixture_id, market, selection, price, implied_prob):
    """Append-only time series, but skips writing a new row if the price is
    unchanged from the most recent capture for this fixture/market/selection
    -- keeps idempotent 90-minute re-runs from bloating the table with
    identical odds.
    """
    ts = now_iso()
    last = conn.execute(
        """SELECT price FROM odds
           WHERE fixture_id = ? AND market = ? AND selection = ?
           ORDER BY captured_at DESC LIMIT 1""",
        (fixture_id, market, selection),
    ).fetchone()

    if last is not None and abs(last["price"] - price) < 1e-9:
        return False

    conn.execute(
        """INSERT INTO odds (fixture_id, market, selection, price, implied_prob, captured_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (fixture_id, market, selection, price, implied_prob, ts),
    )
    return True
