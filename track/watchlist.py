"""Watch list: two paper-only single-bet rules, tracked on Premier games from
WATCH_START onwards (pre-registered 2026-11-09, user's choice).

They came out positive in a backtest over ~20 Premier seasons, but they were
the best of ~30 rules tried, and BTTS No 58%+ lost (-6.6%) on the other six
leagues. So the only fair test is games they have never seen: the rules below
are fixed as written and only games after WATCH_START count. About 200 bets
(BTTS No: roughly 3-4 weeks) are needed before the result means anything.

Backtest before WATCH_START, these exact rules (for reference, not part of the test):
  BTTS No 58%+   599 bets, won 392 vs 359.6 expected, +3.6%
  1X2 fav 82%+    55 bets, won 49 vs 45.3 expected, +2.5%
"""

WATCH_START = "2026-11-09T07:00:00+00:00"
STAKE = 500
RULES = [
    {"key": "btts_no_58", "name": "BTTS No single, chance 58%+", "market": "BTTS", "selection": "No", "min": 0.58,
     "backtest": "599 bets, +3.6%"},
    {"key": "fav_1x2_82", "name": "1X2 favourite single, chance 82%+", "market": "1X2", "selection": None, "min": 0.82,
     "backtest": "55 bets, +2.5%"},
]


def _pick(rule, cands):
    """The rule's selection in one game, or None."""
    if rule["selection"]:
        c = next((c for c in cands if c["selection"] == rule["selection"]), None)
    else:
        c = max(cands, key=lambda c: c["odds_p"]) if cands else None      # the favourite
    return c if c and c["odds_p"] >= rule["min"] else None


def watch_bets(built, since=WATCH_START):
    """{rule key: [bet dicts]} for every settled Premier round whose results came in after `since`."""
    out = {r["key"]: [] for r in RULES}
    for e in built:
        rnd = e["round"]
        if rnd["status"] != "settled" or (rnd.get("sort") or "") < since:
            continue
        for game in rnd["games"].values():
            for rule in RULES:
                c = _pick(rule, game["markets"].get(rule["market"], []))
                if c and c.get("won") is not None:
                    out[rule["key"]].append({"round": rnd["label"], "teams": game["teams"], "selection": c["selection"],
                                             "chance": c["odds_p"], "price": c["price"], "won": bool(c["won"])})
    return out


def summarize(bets):
    n = len(bets)
    if not n:
        return {"bets": 0}
    profit = sum(STAKE * (b["price"] - 1) if b["won"] else -STAKE for b in bets)
    return {"bets": n, "won": sum(b["won"] for b in bets), "expected": sum(b["chance"] for b in bets),
            "profit": profit, "return": profit / (n * STAKE)}
