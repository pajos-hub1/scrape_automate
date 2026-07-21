"""Shared read-queries used by more than one pipeline stage -- kept in one
place so "what counts as the current fixture batch" is answered identically
everywhere, rather than each consumer (features, predict, dashboard)
re-deriving its own slightly-different version of the same question.
"""
from config import MATCHES_PER_ROUND


def get_current_fixture_batch(conn, season_id, batch_size=MATCHES_PER_ROUND):
    """The single most-recently-scraped batch of not-yet-played fixtures
    for a season, in match_number order.

    NOT identified by round_number: a fixture's round_number is only ever
    our own guess (last_played_round + 1), made against a page that carries
    no round label at all -- polling it more than once before that round
    actually locks in can return a genuinely different pairing while still
    guessing the same round_number (confirmed live: two consecutive scrapes
    returned different 10-team pairings, both labeled the same guessed
    round). Two such polls collide under one label and corrupt anything
    that groups by round_number to find "the current batch."

    Ordered by scraped_at, NOT fixture_id: upsert_fixture (db/upsert.py)
    reuses the same fixture_id when a pairing repeats across polls --
    correct, avoids duplicate rows for something unchanged -- but that
    means fixture_id recency is NOT reliable here. If 9 of a round's 10
    pairings happen to match an older poll while only 1 is genuinely new,
    "top 10 by fixture_id" would pick that one new row plus 9 unrelated
    stale rows from other discarded polls that happen to have higher IDs,
    not this round's real companions. scraped_at is refreshed on EVERY
    poll regardless of whether a row is new or reused (see upsert_fixture),
    so all 10 fixtures from one poll always share it -- that's the
    reliable "which poll was freshest" signal. fixture_id stays as a
    tiebreaker for rows written in the same second.

    A stale/superseded batch just silently stops being selected -- nothing
    needs deleting, no migration -- so this also self-heals whatever stale
    batches are already sitting in the DB from before this was fixed.

    season_id may be None: at a season boundary, run.py's cmd_scrape can
    detect (via the Live Score page's round number) that the fixtures page
    has already rolled over to a NEW season's Round 1, before that season
    exists in our DB at all -- it only gets created once its own Round 1
    finishes and gets fingerprinted. Such fixtures are stored with
    season_id left unset rather than wrongly tagged to the old season.
    Passing season_id=None here looks up exactly that orphaned batch;
    passing a real season_id looks up its own upcoming batch OR an
    orphaned one, since at any moment there's only ever one genuine
    current batch, tagged or not.
    """
    if season_id is not None:
        max_played_row = conn.execute(
            "SELECT MAX(round_number) AS mr FROM matches WHERE season_id = ?", (season_id,)
        ).fetchone()
        max_played_round = max_played_row["mr"] or 0
        where_clause = "(season_id = ? AND round_number > ?) OR season_id IS NULL"
        params = (season_id, max_played_round)
    else:
        where_clause = "season_id IS NULL"
        params = ()

    rows = conn.execute(
        f"""SELECT fixture_id, round_number, match_number, team_a, team_b, kickoff_time
            FROM fixtures
            WHERE {where_clause}
            ORDER BY scraped_at DESC, fixture_id DESC
            LIMIT ?""",
        (*params, batch_size),
    ).fetchall()
    return sorted((dict(r) for r in rows), key=lambda r: r["match_number"])


ODDS_FIELD_MAP = {
    ("1X2", "Home"): "odds_home_prob",
    ("1X2", "Draw"): "odds_draw_prob",
    ("1X2", "Away"): "odds_away_prob",
    ("BTTS", "Yes"): "odds_btts_yes_prob",
    ("BTTS", "No"): "odds_btts_no_prob",
    ("OU2.5", "Over"): "odds_over25_prob",
    ("OU2.5", "Under"): "odds_under25_prob",
}


def get_latest_odds_by_fixture(conn, fixture_ids):
    """{fixture_id: {(market, selection): implied_prob}} using each
    fixture's most recently captured odds. Shared by features/build.py
    (persisted features) and predict/build.py (the orphan-batch path,
    which predicts directly without a features row -- see its docstring).
    """
    if not fixture_ids:
        return {}
    placeholders = ",".join("?" for _ in fixture_ids)
    rows = conn.execute(
        f"""SELECT o.fixture_id, o.market, o.selection, o.implied_prob
            FROM odds o
            JOIN (
                SELECT fixture_id, market, selection, MAX(captured_at) AS latest
                FROM odds WHERE fixture_id IN ({placeholders})
                GROUP BY fixture_id, market, selection
            ) latest_o
            ON o.fixture_id = latest_o.fixture_id AND o.market = latest_o.market
               AND o.selection = latest_o.selection AND o.captured_at = latest_o.latest""",
        fixture_ids,
    ).fetchall()

    odds_map = {}
    for r in rows:
        odds_map.setdefault(r["fixture_id"], {})[(r["market"], r["selection"])] = r["implied_prob"]
    return odds_map
