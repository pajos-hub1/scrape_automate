"""Regression tests for the 2026-10-23 fault: S33 got archived for good,
fixtures went untagged / onto an old season's reused row, and ~13 rounds of
predictions never reconciled."""
from conftest import add_played_fixture

from db.connection import get_connection
from db.upsert import find_season_by_results, get_or_create_season, upsert_fixture
from track.reconcile import reconcile


def test_archived_season_seen_again_becomes_current(env):
    with get_connection(env[0]) as conn:
        sid = get_or_create_season(conn, "fp-x", 20)
        conn.execute("UPDATE seasons SET status = 'archived' WHERE season_id = ?", (sid,))
        assert get_or_create_season(conn, "fp-x", 21) == sid
        assert conn.execute("SELECT status FROM seasons WHERE season_id = ?", (sid,)).fetchone()[0] == "current"


def test_season_recognised_without_round_1(env):
    with get_connection(env[0]) as conn:
        sid = get_or_create_season(conn, "fp-y", 5)
        rows = [{"match_number": i, "team_a": f"A{i}", "team_b": f"B{i}", "ft_a": i % 3, "ft_b": 1, "ht_a": 0, "ht_b": 0}
                for i in range(1, 11)]
        for i, m in enumerate(rows, start=1):
            conn.execute("""INSERT INTO matches (season_id, round_number, match_number, team_a, team_b, ft_a, ft_b, ht_a, ht_b, scraped_at)
                            VALUES (?, 4, ?, ?, ?, ?, ?, 0, 0, '2026-10-23T10:00:00+00:00')""",
                         (sid, i, m["team_a"], m["team_b"], m["ft_a"], m["ft_b"]))
        assert find_season_by_results(conn, {4: rows, 5: []}) == sid
        assert find_season_by_results(conn, {4: rows[:2]}) is None           # too little evidence


def test_old_fixture_row_not_reused_for_a_later_season(env):
    with get_connection(env[0]) as conn:
        conn.execute("INSERT INTO seasons (season_id, fingerprint, status, first_seen, last_seen, round_count) "
                     "VALUES (8, 'fp8', 'archived', '2026-08-01T00:00:00+00:00', '2026-08-07T00:00:00+00:00', 38)")
        old, _ = upsert_fixture(conn, 8, 31, 9, "Ipswich", "Palace", scraped_at="2026-08-07T10:00:00+00:00")
        new, was_new = upsert_fixture(conn, None, 31, 9, "Ipswich", "Palace", scraped_at="2026-10-23T18:58:41+00:00")
        assert new != old and was_new
        again, was_new = upsert_fixture(conn, None, 31, 9, "Ipswich", "Palace", scraped_at="2026-10-23T19:13:41+00:00")
        assert again == new and not was_new                                  # live re-poll reuses it
        assert conn.execute("SELECT season_id FROM fixtures WHERE fixture_id = ?", (new,)).fetchone()[0] is None


def test_reconcile_ignores_old_season_result_and_finds_the_real_game(env):
    with get_connection(env[0]) as conn:
        conn.execute("INSERT INTO seasons (season_id, fingerprint, status, first_seen, last_seen, round_count) "
                     "VALUES (8, 'fp8', 'archived', '2026-08-01T00:00:00+00:00', '2026-08-07T00:00:00+00:00', 38)")
        conn.execute("INSERT INTO seasons (season_id, fingerprint, status, first_seen, last_seen, round_count) "
                     "VALUES (33, 'fp33', 'current', '2026-10-21T00:00:00+00:00', '2026-10-23T00:00:00+00:00', 30)")
        conn.execute("""INSERT INTO matches (season_id, round_number, match_number, team_a, team_b, ft_a, ft_b, ht_a, ht_b, scraped_at)
                        VALUES (8, 31, 9, 'Ipswich', 'Palace', 0, 3, 0, 1, '2026-08-07T11:00:00+00:00')""")
        cur = conn.execute("""INSERT INTO matches (season_id, round_number, match_number, team_a, team_b, ft_a, ft_b, ht_a, ht_b, scraped_at)
                              VALUES (33, 31, 9, 'Ipswich', 'Palace', 2, 0, 1, 0, '2026-10-23T20:30:00+00:00')""")
        real = cur.lastrowid
        fx = conn.execute("""INSERT INTO fixtures (season_id, round_number, match_number, team_a, team_b, scraped_at, fixture_key)
                             VALUES (8, 31, 9, 'Ipswich', 'Palace', '2026-10-23T18:58:41+00:00', 'k')""").lastrowid
        conn.execute("""INSERT INTO predictions (fixture_ref, model_version, market, label, probabilities, confidence, created_at)
                        VALUES (?, 'baseline_v0', '1X2', 'Home', '{"Home":0.5,"Draw":0.3,"Away":0.2}', 0.5, 'x')""", (fx,))
        assert reconcile(conn)["reconciled"] == 1
        assert conn.execute("SELECT match_ref, actual_label FROM prediction_results").fetchone()[:] == (real, "Home")
