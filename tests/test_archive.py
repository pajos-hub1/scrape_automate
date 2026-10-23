from conftest import add_played_fixture

from db.archive import archive_files, archive_if_needed, merge_archives_if_needed
from db.connection import get_connection
from track.report import accuracy_stats


def _counts(conn, prefix=""):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {prefix}{t}").fetchone()[0]
            for t in ("odds", "predictions", "prediction_results")}


def test_archive_moves_rows_and_views_keep_history(populated, env):
    with get_connection(populated) as conn:
        before = accuracy_stats(conn)["1X2"]
        summary = archive_if_needed(conn, force=True)
        assert summary["archived"]
        assert _counts(conn, "main.") == {"odds": 0, "predictions": 0, "prediction_results": 0}

    assert len(archive_files()) == 1
    with get_connection(populated) as conn:   # fresh connection attaches the new archive
        assert _counts(conn, "all_") == {"odds": 12, "predictions": 4, "prediction_results": 4}
        assert accuracy_stats(conn)["1X2"] == before


def test_unreconciled_predictions_and_their_odds_stay_live(env):
    db_path, _ = env
    with get_connection(db_path) as conn:
        fid = add_played_fixture(conn, 1, 1, 0)
        conn.execute("DELETE FROM prediction_results")   # now pending
        archive_if_needed(conn, force=True)
        assert conn.execute("SELECT COUNT(*) FROM main.predictions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM main.odds WHERE fixture_id = ?", (fid,)).fetchone()[0] == 3


def test_merge_combines_archives_without_losing_rows(env):
    db_path, _ = env
    for batch in range(3):
        with get_connection(db_path) as conn:
            for n in range(batch * 2 + 1, batch * 2 + 3):
                add_played_fixture(conn, n, 1, 0)
            archive_if_needed(conn, force=True)
    assert len(archive_files()) == 3

    result = merge_archives_if_needed(force=True, db_path=db_path)
    assert result["merged"]
    assert len(archive_files()) == 1
    with get_connection(db_path) as conn:
        assert _counts(conn, "all_") == {"odds": 18, "predictions": 6, "prediction_results": 6}


def test_merge_not_needed_below_threshold(populated, env):
    with get_connection(populated) as conn:
        archive_if_needed(conn, force=True)
    assert merge_archives_if_needed(db_path=populated) == {"merged": False, "archive_files": 1}


def _lg_game(conn, ext, cid, season_first, rnd, scraped="2026-01-01T00:00:00+00:00"):
    conn.execute("INSERT INTO lg_matches VALUES (?, ?, 'x', ?, ?, 1, 'a', 'b', 1, 0, 0, 0, 't', ?)",
                 (ext, cid, season_first, rnd, scraped))
    conn.execute("INSERT INTO lg_events VALUES (?, ?, 'x', ?, 'a', 'b', 'k', 'f')", (ext, cid, ext))
    conn.execute("INSERT INTO lg_odds VALUES (?, '1X2', 'Home', 1.5, 'c')", (ext,))


def test_archive_moves_finished_league_seasons_and_keeps_them_visible(populated, env):
    with get_connection(populated) as conn:
        _lg_game(conn, 101, 2, 100, 1)                    # finished season (a newer one exists)
        _lg_game(conn, 501, 2, 500, 1)                    # current season: stays live
        summary = archive_if_needed(conn, force=True)
        assert summary["league_games_moved"] == 1
        assert [r[0] for r in conn.execute("SELECT ext_match_id FROM main.lg_matches")] == [501]
    with get_connection(populated) as conn:
        assert sorted(r[0] for r in conn.execute("SELECT ext_match_id FROM all_lg_matches")) == [101, 501]
        assert conn.execute("SELECT COUNT(*) FROM all_lg_odds").fetchone()[0] == 2


def test_collector_does_not_reinsert_archived_league_games(populated, env, monkeypatch):
    import track.leagues as lg
    with get_connection(populated) as conn:
        _lg_game(conn, 101, 2, 100, 1)
        _lg_game(conn, 501, 2, 500, 1)
        archive_if_needed(conn, force=True)
    monkeypatch.setattr(lg, "get_connection", lambda: get_connection(populated))
    monkeypatch.setattr(lg.zoom_api, "competitions", lambda: {2: {"name": "Liga-Zoom", "teams": {}}})
    monkeypatch.setattr(lg.zoom_api, "league_groups", lambda: {})
    old = {"first_id": 100, "games_per_round": 1, "round_times": {}, "rounds": {1: [
        {"ext_match_id": 101, "match_number": 1, "team_a": "a", "team_b": "b", "ft_a": 1, "ft_b": 0, "ht_a": 0, "ht_b": 0}]}}
    monkeypatch.setattr(lg.zoom_api, "season_results", lambda cid, teams, previous=False: old)
    assert lg.collect_leagues()["new_results"] == 0           # already archived: not duplicated
    with get_connection(populated) as conn:
        assert conn.execute("SELECT COUNT(*) FROM all_lg_matches WHERE ext_match_id = 101").fetchone()[0] == 1


def test_merge_with_an_old_archive_that_has_no_league_tables(env):
    db_path, _ = env
    with get_connection(db_path) as conn:
        add_played_fixture(conn, 1, 1, 0)
        archive_if_needed(conn, force=True)                   # archive 1: no finished league seasons
    with get_connection(db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS arc0.lg_matches")   # simulate a pre-2026-10-23 archive
        _lg_game(conn, 101, 2, 100, 1)
        _lg_game(conn, 501, 2, 500, 1)
        add_played_fixture(conn, 2, 1, 0)
        archive_if_needed(conn, force=True)                   # archive 2: has league data
    assert merge_archives_if_needed(force=True, db_path=db_path)["merged"]
    with get_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM all_lg_matches").fetchone()[0] == 2
