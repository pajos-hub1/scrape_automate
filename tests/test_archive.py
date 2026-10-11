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
