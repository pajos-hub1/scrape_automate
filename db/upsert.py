"""Idempotent writes for the scraper. Every function here is safe to call
repeatedly with the same data -- re-running a scrape must never create
duplicate rows.
"""
import json
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


def upsert_fixture(conn, season_id, round_number, match_number, team_a, team_b, kickoff_time=None, scraped_at=None):
    """Returns (fixture_id, was_new). Idempotent on fixture_key
    (round/match/teams) -- season_id may be resolved later than the
    fixture itself is first seen, so a second scrape can attach it without
    creating a duplicate row.

    scraped_at should be computed ONCE by the caller and passed in the same
    for every fixture in one scrape's batch (see run.py cmd_scrape) --
    db/queries.get_current_fixture_batch relies on every fixture from one
    poll sharing an identical scraped_at to tell "this poll's batch" apart
    from an older, superseded one that happens to include some of the same
    reused rows. Computing it fresh per-call here would let genuinely
    same-poll fixtures drift apart by however long the upsert loop takes.
    """
    ts = scraped_at or now_iso()
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


FEATURE_COLUMNS = [
    "match_ref", "fixture_ref", "season_id", "round_number", "team_a", "team_b",
    "a_form_games", "a_form_pts", "a_form_wins", "a_form_draws", "a_form_losses", "a_form_gf", "a_form_ga",
    "b_form_games", "b_form_pts", "b_form_wins", "b_form_draws", "b_form_losses", "b_form_gf", "b_form_ga",
    "h2h_games", "h2h_a_wins", "h2h_b_wins", "h2h_draws", "h2h_avg_goals",
    "a_home_played", "a_home_wins", "a_home_draws", "a_home_losses", "a_home_gf", "a_home_ga",
    "b_away_played", "b_away_wins", "b_away_draws", "b_away_losses", "b_away_gf", "b_away_ga",
    "a_season_pts_rate", "a_season_gf_avg", "a_season_ga_avg",
    "b_season_pts_rate", "b_season_gf_avg", "b_season_ga_avg",
    "odds_home_prob", "odds_draw_prob", "odds_away_prob", "odds_over25_prob", "odds_under25_prob",
    "odds_btts_yes_prob", "odds_btts_no_prob",
]


def upsert_feature(conn, row):
    """row: dict with a subset of FEATURE_COLUMNS keys (missing keys ->
    NULL). Exactly one of match_ref/fixture_ref must be set -- that's the
    natural key this upserts on (both carry a UNIQUE constraint in the
    schema), matching whichever one this row represents.
    """
    full_row = {c: row.get(c) for c in FEATURE_COLUMNS}
    full_row["computed_at"] = now_iso()

    conflict_col = "match_ref" if full_row["match_ref"] is not None else "fixture_ref"
    if full_row[conflict_col] is None:
        raise ValueError("upsert_feature requires match_ref or fixture_ref to be set")

    cols = list(full_row.keys())
    col_list = ",".join(cols)
    placeholders = ",".join("?" for _ in cols)
    update_clause = ",".join(f"{c}=excluded.{c}" for c in cols if c != conflict_col)

    conn.execute(
        f"""INSERT INTO features ({col_list}) VALUES ({placeholders})
            ON CONFLICT({conflict_col}) DO UPDATE SET {update_clause}""",
        [full_row[c] for c in cols],
    )


def upsert_prediction(conn, fixture_ref, model_version, market, label, probabilities, confidence):
    """Idempotent on (fixture_ref, model_version, market) -- re-running the
    same model against the same fixture updates the prediction in place
    rather than accumulating stale duplicates.
    """
    conn.execute(
        """INSERT INTO predictions
           (fixture_ref, model_version, market, label, probabilities, confidence, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(fixture_ref, model_version, market) DO UPDATE SET
               label = excluded.label,
               probabilities = excluded.probabilities,
               confidence = excluded.confidence,
               created_at = excluded.created_at""",
        (fixture_ref, model_version, market, label, json.dumps(probabilities), confidence, now_iso()),
    )


def upsert_prediction_result(conn, prediction_id, match_ref, market, predicted_label, actual_label,
                              baseline_label, odds_implied_label):
    """Idempotent on prediction_id (UNIQUE in the schema) -- reconciling the
    same prediction twice updates in place rather than duplicating.
    """
    correct = int(predicted_label == actual_label)
    baseline_correct = int(baseline_label == actual_label) if baseline_label is not None else None
    odds_implied_correct = int(odds_implied_label == actual_label) if odds_implied_label is not None else None

    conn.execute(
        """INSERT INTO prediction_results
           (prediction_id, match_ref, market, predicted_label, actual_label, correct,
            baseline_label, baseline_correct, odds_implied_label, odds_implied_correct, reconciled_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(prediction_id) DO UPDATE SET
               match_ref = excluded.match_ref,
               predicted_label = excluded.predicted_label,
               actual_label = excluded.actual_label,
               correct = excluded.correct,
               baseline_label = excluded.baseline_label,
               baseline_correct = excluded.baseline_correct,
               odds_implied_label = excluded.odds_implied_label,
               odds_implied_correct = excluded.odds_implied_correct,
               reconciled_at = excluded.reconciled_at""",
        (prediction_id, match_ref, market, predicted_label, actual_label, correct,
         baseline_label, baseline_correct, odds_implied_label, odds_implied_correct, now_iso()),
    )
