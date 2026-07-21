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
    """
    max_played_row = conn.execute(
        "SELECT MAX(round_number) AS mr FROM matches WHERE season_id = ?", (season_id,)
    ).fetchone()
    max_played_round = max_played_row["mr"] or 0

    rows = conn.execute(
        """SELECT fixture_id, round_number, match_number, team_a, team_b, kickoff_time
           FROM fixtures
           WHERE season_id = ? AND round_number > ?
           ORDER BY scraped_at DESC, fixture_id DESC
           LIMIT ?""",
        (season_id, max_played_round, batch_size),
    ).fetchall()
    return sorted((dict(r) for r in rows), key=lambda r: r["match_number"])
