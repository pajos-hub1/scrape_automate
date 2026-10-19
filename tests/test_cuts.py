import pytest

from track.cuts import cut_stats, verdict


def _leg(p, won):
    return {"odds_p": p, "won": won, "price": 1 / p, "teams": ("A", "B")}


def _entry(slip_legs, fav_results):
    games = {i: {"teams": (f"A{i}", f"B{i}"), "markets": {"1X2": [
        {"selection": "Home", "price": 1.5, "odds_p": 0.6, "won": w},
        {"selection": "Away", "price": 3.0, "odds_p": 0.3, "won": not w}]}} for i, w in enumerate(fav_results)}
    won = all(l["won"] for l in slip_legs)
    slip = {"name": "1X2 top 5", "legs": slip_legs, "played": True, "won": won,
            "legs_won": sum(l["won"] for l in slip_legs)}
    return {"round": {"status": "settled", "season": 32, "games": games}, "slips": [slip]}


def test_counts_and_expectations():
    built = [_entry([_leg(0.8, True), _leg(0.8, True), _leg(0.8, False)], [True, False, True])]
    rows = {r["label"]: r for r in cut_stats(built)}
    fav = rows["Favourites won (1X2, every game)"]
    assert fav["n"] == 3 and fav["actual"] == 2 and fav["expected"] == pytest.approx(1.8)
    picks = rows["Our ticket picks won"]
    assert picks["actual"] == 2 and picks["expected"] == pytest.approx(2.4)
    one = rows["Tickets lost by exactly 1 pick"]
    assert one["actual"] == 1 and one["expected"] == pytest.approx(3 * 0.8 * 0.8 * 0.2)


def test_verdict_flags_only_large_drift():
    normal = [_entry([_leg(0.6, i % 5 != 0)], [i % 5 != 0] * 1) for i in range(200)]   # 80% vs 60% expected
    rows = cut_stats(normal)
    assert any(r["unusual"] for r in rows) and verdict(rows).startswith("⚠️")
    fair = [_entry([_leg(0.5, i % 2 == 0)], [i % 2 == 0] * 0) for i in range(200)]
    assert verdict(cut_stats(fair)).startswith("✅")
