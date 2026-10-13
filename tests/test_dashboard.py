from conftest import add_played_fixture

from dashboard.build import build_dashboard
from db.connection import get_connection


def test_dashboard_builds_end_to_end(env, tmp_path):
    db_path, _ = env
    out = tmp_path / "index.html"
    with get_connection(db_path) as conn:
        for n in range(1, 11):
            add_played_fixture(conn, n, 1, 0)
        build_dashboard(conn, out_path=out)
    page = out.read_text()
    assert "Paper betslips" in page and "Season by season" in page and "</html>" in page
