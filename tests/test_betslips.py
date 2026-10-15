import pytest

from track.betslips import build_slips, summarize


def _cand(sel, price, won, p=None):
    return {"selection": sel, "price": price, "model_p": p or 1 / price, "odds_p": p or 1 / price, "won": won}


def _round(label, games, status="settled"):
    return {"key": label, "label": label, "status": status, "sort": label, "games": games}


def _game(i, over_won, home_won=None):
    markets = {"OU1.5": [_cand("Over", 1.25, over_won, 0.78), _cand("Under", 3.8, not over_won, 0.22)]}
    if home_won is not None:
        markets["1X2"] = [_cand("Home", 1.5, home_won, 0.62), _cand("Draw", 4.0, False, 0.2),
                          _cand("Away", 5.0, not home_won, 0.18)]
    return {"teams": (f"A{i}", f"B{i}"), "markets": markets}


def test_slip_pays_product_of_odds_only_when_all_legs_win():
    rnd = _round("R1", {i: _game(i, True) for i in range(10)})
    slip = next(s for s in build_slips([rnd])[0]["slips"] if s["name"] == "OU1.5 top 5")
    assert len(slip["legs"]) == 5 and slip["won"]
    assert slip["returned"] == pytest.approx(500 * 1.25 ** 5)

    rnd2 = _round("R2", {i: _game(i, i != 0) for i in range(10)})   # game 0's Over loses
    slips = build_slips([rnd2])[0]["slips"]
    all_games = next(s for s in slips if s["name"].endswith("-game"))
    assert not all_games["won"] and all_games["returned"] == 0
    assert all_games["legs_won"] == 9


def test_one_pick_per_game_and_never_both_sides():
    rnd = _round("R1", {i: _game(i, True, home_won=True) for i in range(10)})
    slips = build_slips([rnd])[0]["slips"]
    all_games = next(s for s in slips if s["name"].endswith("-game"))
    assert len({leg["teams"] for leg in all_games["legs"]}) == 10
    for s in slips:
        assert len({leg["teams"] for leg in s["legs"]}) == len(s["legs"])


def test_history_only_uses_earlier_rounds():
    # Round 2's picks must not be influenced by round 2's own results.
    r1 = _round("R1", {i: _game(i, True) for i in range(10)})
    r2_win = _round("R2", {i: _game(i, True) for i in range(10)})
    r2_lose = _round("R2", {i: _game(i, False) for i in range(10)})
    score_win = [leg["hist_p"] for leg in build_slips([r1, r2_win])[1]["slips"][0]["legs"]]
    score_lose = [leg["hist_p"] for leg in build_slips([r1, r2_lose])[1]["slips"][0]["legs"]]
    assert score_win == score_lose


def test_upcoming_round_is_pending_and_excluded_from_totals():
    games = {i: _game(i, None) for i in range(10)}
    for g in games.values():
        for c in g["markets"]["OU1.5"]:
            c["won"] = None
    built = build_slips([_round("R1", {i: _game(i, True) for i in range(10)}), _round("UP", games, "upcoming")])
    assert all(s["won"] is None for s in built[1]["slips"])
    totals = summarize(built, include_unplayed=True)
    assert totals["OU1.5 top 5"]["slips"] == 1 and totals["OU1.5 top 5"]["won"] == 1


def test_round_in_play_is_shown_but_not_counted_or_learned_from():
    games = {i: _game(i, None) for i in range(10)}
    for g in games.values():
        for c in g["markets"]["OU1.5"]:
            c["won"] = None
    settled = _round("R1", {i: _game(i, True) for i in range(10)})
    playing = _round("R2", games, "playing")
    built = build_slips([settled, playing, _round("R3", {i: _game(i, True) for i in range(10)})])
    assert all(s["won"] is None for s in built[1]["slips"])             # in play: unsettled
    assert summarize(built, include_unplayed=True)["OU1.5 top 5"]["slips"] == 2                 # only R1 and R3 count
    # R3's history only learned from R1, not from the unfinished R2
    assert built[2]["slips"][0]["legs"][0]["hist_p"] == build_slips([settled, built[2]["round"]])[1]["slips"][0]["legs"][0]["hist_p"]


def test_stakes_per_slip_type():
    from track.betslips import round_stake, stake_for
    rnd = _round("R1", {i: _game(i, True, home_won=True) for i in range(10)})
    slips = {s["name"]: s for s in build_slips([rnd])[0]["slips"]}
    assert slips["1X2 top 5"]["stake"] == 500
    assert slips["1X2 top 5"]["returned"] == pytest.approx(500 * 1.5 ** 5)
    assert stake_for("BTTS top 5") == 1000
    assert round_stake() == 2000                       # 1X2 500 + HT O/U 0.5 500 + BTTS 1,000
    assert not slips["OU1.5 top 5"]["played"]
