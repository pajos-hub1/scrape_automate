import pytest
from conftest import add_played_fixture

from db.connection import get_connection
from track.report import probability_stats, profit_stats


def test_profit_stats_flat_stake(env):
    db_path, _ = env
    with get_connection(db_path) as conn:
        add_played_fixture(conn, 1, 1, 0)   # Home @2.0 wins  -> +1
        add_played_fixture(conn, 2, 0, 1)   # Home @2.0 loses -> -1
        add_played_fixture(conn, 3, 2, 0)   # Home @2.0 wins  -> +1
        s = profit_stats(conn, "baseline_v0")["1X2"]
    assert s["bets"] == 3
    assert s["returns"] == pytest.approx(4.0)   # profit +1 on 3 units staked
    assert s["fav_bets"] == 3 and s["fav_returns"] == pytest.approx(4.0)


def test_profit_skips_unpriced_picks(env):
    db_path, _ = env
    with get_connection(db_path) as conn:
        add_played_fixture(conn, 1, 1, 0, picks={"baseline_v0": "Other"})
        assert profit_stats(conn, "baseline_v0")["1X2"]["bets"] == 0


def test_probability_stats_against_odds(env):
    db_path, _ = env
    with get_connection(db_path) as conn:
        add_played_fixture(conn, 1, 1, 0)
        s = probability_stats(conn, "baseline_v0")["1X2"]
    assert s["n"] == 1
    import math
    assert s["model_ll"] == pytest.approx(-math.log(0.5))
    # Odds 2.0/3.5/4.0 de-margined: home = 0.5 / (0.5 + 1/3.5 + 0.25)
    home = 0.5 / (0.5 + 1 / 3.5 + 0.25)
    assert s["odds_ll"] == pytest.approx(-math.log(home))
