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
    assert stake_for("BTTS top 5", 31) == 1000 and stake_for("BTTS top 5", 32) == 500
    assert round_stake(30) == 2000                     # 1X2 500 + HT O/U 0.5 500 + BTTS 1,000
    assert round_stake(31) == 2500                     # + Best 6 500
    assert round_stake(32) == 3000                     # 3 x top 5 + 3 x top 3, ₦500 each
    rnd = _round("R1", {i: _game(i, True, home_won=True) for i in range(10)})
    rnd["season"] = 31
    slips = {s["name"]: s for s in build_slips([rnd])[0]["slips"]}
    assert slips["1X2 top 5"]["stake"] == 500 and slips["1X2 top 5"]["played"]
    assert slips["1X2 top 5"]["returned"] == pytest.approx(500 * 1.5 ** 5)
    assert not slips["OU1.5 top 5"]["played"]


def _full_game(i):
    g = _game(i, True, home_won=True)
    # BTTS favours the LOW-numbered games, so the 3 slips don't all pick the same 5
    g["markets"]["BTTS"] = [_cand("Yes", 1.9, True, 0.62 - i * 0.01), _cand("No", 1.9, False, 0.38 + i * 0.01)]
    g["markets"]["HT_OU0.5"] = [_cand("Over", 1.3, True, 0.74 + i * 0.005), _cand("Under", 3.5, False, 0.26)]
    return g


def test_best6_takes_strongest_picks_one_per_game():
    import track.betslips as b
    games = {i: _full_game(i) for i in range(10)}
    for i, g in games.items():                    # make every game's 1X2 Home a distinct strength
        g["markets"]["1X2"][0]["model_p"] = g["markets"]["1X2"][0]["odds_p"] = 0.5 + i * 0.03
    rnd = _round("R1", games)
    rnd["season"] = 31
    slips = {s["name"]: s for s in build_slips([rnd])[0]["slips"]}
    best6 = slips["Best 6"]
    assert len(best6["legs"]) == 6
    assert len({leg["teams"] for leg in best6["legs"]}) == 6          # one pick per game
    pool = [leg for s in slips.values() if s["played"] and s["name"].endswith("top 5") for leg in s["legs"]]
    strongest = {}
    for leg in pool:
        if leg["teams"] not in strongest or leg["score"] > strongest[leg["teams"]]["score"]:
            strongest[leg["teams"]] = leg
    expected = sorted(strongest.values(), key=lambda l: l["score"], reverse=True)[:6]
    assert [l["teams"] for l in best6["legs"]] == [l["teams"] for l in expected]
    assert best6["played"]


def test_best6_not_staked_before_its_start_season():
    import track.betslips as b
    rnd = _round("R1", {i: _full_game(i) for i in range(10)})
    for season, staked in ((30, False), (31, True), (32, False)):    # Best 6 only in S31's plan
        rnd["season"] = season
        best6 = next(s for s in build_slips([rnd])[0]["slips"] if s["name"] == "Best 6")
        assert best6["played"] is staked, season



def test_season32_top5_and_top3_tickets():
    from track.betslips import season_totals
    rnd = _round("R1", {i: _full_game(i) for i in range(10)})
    rnd["season"], rnd["key"] = 32, (32, 1)
    slips = {s["name"]: s for s in build_slips([rnd])[0]["slips"]}
    staked = sorted(n for n, s in slips.items() if s["played"])
    assert staked == sorted(["1X2 top 5", "HT_OU0.5 top 5", "BTTS top 5", "1X2 top 3", "HT_OU0.5 top 3", "BTTS top 3"])
    for m in ("1X2", "HT_OU0.5", "BTTS"):
        assert slips[f"{m} top 3"]["legs"] == slips[f"{m} top 5"]["legs"][:3]   # 3 strongest of the 5
        assert slips[f"{m} top 3"]["stake"] == slips[f"{m} top 5"]["stake"] == 500
    t = season_totals(build_slips([rnd]))[32]
    assert t["staked"] == 3000 and set(t["by_legs"]) == {5, 3}
    assert sum(t["by_legs"].values()) == pytest.approx(t["profit"])


def test_no_top3_comparison_before_season32():
    from track.betslips import season_totals
    rnd = _round("R1", {i: _full_game(i) for i in range(10)})
    rnd["season"], rnd["key"] = 31, (31, 1)
    t = season_totals(build_slips([rnd]))[31]
    assert t["by_legs"] == {} and t["staked"] == 2500
