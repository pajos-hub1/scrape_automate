"""Shared read-queries used by more than one pipeline stage -- kept in one
place so "what counts as the current fixture batch" is answered identically
everywhere, rather than each consumer (features, predict, dashboard)
re-deriving its own slightly-different version of the same question.
"""
from config import MATCHES_PER_ROUND


def get_current_fixture_batch(conn, season_id, batch_size=MATCHES_PER_ROUND):
    """The single most-recently-scraped batch of not-yet-played fixtures
    for a season, in match_number order.

    Identified by fixture_id recency (strictly increasing with insertion
    order), NOT round_number. A fixture's round_number is only ever our
    own guess (last_played_round + 1), made against a page that carries no
    round label at all -- polling it more than once before that round
    actually locks in can return a genuinely different pairing while still
    guessing the same round_number (confirmed live: two consecutive scrapes
    returned different 10-team pairings, both labeled the same guessed
    round). Two such polls collide under one label and corrupt anything
    that groups by round_number to find "the current batch."

    fixture_id can't collide this way: it's assigned once, in real scrape
    order, so "the last N fixture rows inserted for this season" is always
    exactly the freshest poll, regardless of what round number got guessed
    for it. A stale/superseded batch just silently stops being selected --
    nothing needs deleting, no migration -- so this also self-heals
    whatever stale batches are already sitting in the DB from before this
    was fixed.
    """
    max_played_row = conn.execute(
        "SELECT MAX(round_number) AS mr FROM matches WHERE season_id = ?", (season_id,)
    ).fetchone()
    max_played_round = max_played_row["mr"] or 0

    rows = conn.execute(
        """SELECT fixture_id, round_number, match_number, team_a, team_b, kickoff_time
           FROM fixtures
           WHERE season_id = ? AND round_number > ?
           ORDER BY fixture_id DESC
           LIMIT ?""",
        (season_id, max_played_round, batch_size),
    ).fetchall()
    return sorted((dict(r) for r in rows), key=lambda r: r["match_number"])
