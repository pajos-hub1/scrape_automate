"""Watch list (track/watchlist.py): fixed rules, counted only after WATCH_START."""
from track import watchlist as W


def _cand(sel, p, price, won):
    return {"selection": sel, "odds_p": p, "price": price, "won": won}


def _round(sort, games, status="settled"):
    return {"round": {"status": status, "sort": sort, "label": "S40 R1",
                      "games": {f"g{i}": {"teams": (f"A{i}", f"B{i}"), "markets": m} for i, m in enumerate(games)}},
            "slips": []}


GAMES = [
    {"BTTS": [_cand("Yes", 0.40, 2.35, False), _cand("No", 0.60, 1.58, True)],          # BTTS No 60%: bet, won
     "1X2": [_cand("Home", 0.85, 1.12, True), _cand("Draw", 0.10, 9.0, False), _cand("Away", 0.05, 18.0, False)]},
    {"BTTS": [_cand("Yes", 0.43, 2.2, True), _cand("No", 0.57, 1.66, False)],           # 57%: below the line
     "1X2": [_cand("Home", 0.50, 1.8, False), _cand("Draw", 0.25, 3.6, False), _cand("Away", 0.25, 3.6, True)]},
    {"BTTS": [_cand("Yes", 0.62, 1.52, True), _cand("No", 0.38, 2.5, False)],           # Yes side: never bet
     "1X2": [_cand("Home", 0.20, 4.5, False), _cand("Draw", 0.15, 6.0, False), _cand("Away", 0.83, 1.14, False)]},
]


def test_rules_pick_only_qualifying_games():
    bets = W.watch_bets([_round("2099-01-01T00:00:00+00:00", GAMES)])
    assert [(b["selection"], b["chance"]) for b in bets["btts_no_58"]] == [("No", 0.60)]
    assert [(b["selection"], b["won"]) for b in bets["fav_1x2_82"]] == [("Home", True), ("Away", False)]
    s = W.summarize(bets["fav_1x2_82"])
    assert s["bets"] == 2 and s["won"] == 1 and round(s["profit"]) == round(500 * 0.12 - 500)


def test_only_rounds_after_the_start_count():
    old = _round("2026-11-08T23:00:00+00:00", GAMES)          # before WATCH_START
    pending = _round("2099-01-01T00:00:00+00:00", GAMES, status="upcoming")
    assert W.watch_bets([old, pending]) == {"btts_no_58": [], "fav_1x2_82": []}
    assert W.summarize([]) == {"bets": 0}
