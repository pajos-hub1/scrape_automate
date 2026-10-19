"""Cut tracker: are favourites and our ticket picks losing more often than
Bet9ja's own odds say they should?

Answers the suspicion that the bookmaker makes low-odds picks ("sure
games") lose more than their prices imply, especially on accumulators.
Every number is compared with what the odds predicted (margin removed),
plus a z-score: |z| < 2 is ordinary luck, |z| >= 2 is worth a look, and
only a gap that keeps growing over seasons would mean anything.

As of 2026-10-19 (all history): favourites at every price won as often as
the odds said (e.g. 1.00-1.30: 77.4% expected, 77.8% actual), and tickets
lost by exactly one pick as often as chance predicts (1X2 top 5: 223 vs
223 expected).
"""
import math

from track.betslips import played

UNUSUAL_Z = 2.0


def _poisson_binomial(ps):
    """P(k picks lost) for independent picks with win chances ps."""
    dist = [1.0]
    for p in ps:
        nxt = [0.0] * (len(dist) + 1)
        for k, v in enumerate(dist):
            nxt[k] += v * p
            nxt[k + 1] += v * (1 - p)
        dist = nxt
    return dist


def _row(label, actual, expected, variance, n, unit):
    z = (actual - expected) / math.sqrt(variance) if variance > 0 else 0.0
    return {"label": label, "n": n, "actual": actual, "expected": expected, "z": z,
            "unusual": abs(z) >= UNUSUAL_Z, "unit": unit}


def cut_stats(built, season=None):
    """Rows comparing actual results with what the odds predicted, over
    settled rounds (one season, or all of them)."""
    rounds = [e for e in built if e["round"]["status"] == "settled"
              and (season is None or e["round"].get("season") == season)]

    fav_n = fav_w = fav_e = fav_v = 0.0
    for e in rounds:
        for g in e["round"]["games"].values():
            cands = g["markets"].get("1X2")
            if not cands:
                continue
            fav = min(cands, key=lambda c: c["price"])
            fav_n += 1
            fav_w += fav["won"]
            fav_e += fav["odds_p"]
            fav_v += fav["odds_p"] * (1 - fav["odds_p"])

    pick_n = pick_w = pick_e = pick_v = 0.0
    tick_n = tick_w = tick_e = tick_v = 0.0
    one_w = one_e = one_v = 0.0
    for e in rounds:
        for slip in played(e["slips"]):
            ps = [leg["odds_p"] for leg in slip["legs"]]
            for leg, p in zip(slip["legs"], ps):
                pick_n += 1
                pick_w += leg["won"]
                pick_e += p
                pick_v += p * (1 - p)
            p_all = math.prod(ps)
            tick_n += 1
            tick_w += bool(slip["won"])
            tick_e += p_all
            tick_v += p_all * (1 - p_all)
            p_one = _poisson_binomial(ps)[1]
            one_w += slip["legs_won"] == len(slip["legs"]) - 1
            one_e += p_one
            one_v += p_one * (1 - p_one)

    rows = []
    if fav_n:
        rows.append(_row("Favourites won (1X2, every game)", fav_w, fav_e, fav_v, int(fav_n), "games"))
    if pick_n:
        rows.append(_row("Our ticket picks won", pick_w, pick_e, pick_v, int(pick_n), "picks"))
    if tick_n:
        rows.append(_row("Tickets won", tick_w, tick_e, tick_v, int(tick_n), "tickets"))
        rows.append(_row("Tickets lost by exactly 1 pick", one_w, one_e, one_v, int(tick_n), "tickets"))
    return rows


def verdict(rows):
    unusual = [r for r in rows if r["unusual"]]
    if not rows:
        return "not enough results yet"
    if not unusual:
        return "✅ normal -- results match the odds"
    return "⚠️ unusual: " + ", ".join(r["label"].lower() for r in unusual)
