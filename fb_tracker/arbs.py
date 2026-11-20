"""Arbitrage ("sure bet") check for one match across bookmakers.

For each way of covering every outcome, take the best price for each leg from
any bookmaker. If the stakes needed to win the same amount on every outcome
add up to less than that amount (sum of 1/price < 1), every result pays back
more than was staked: margin = 1 / sum(1/price) - 1.
"""

COVERS = {
    "1X2": [("1X2", "1"), ("1X2", "X"), ("1X2", "2")],
    "Over/Under 2.5": [("OU2.5", "O"), ("OU2.5", "U")],
    "Home v Draw-or-Away": [("1X2", "1"), ("DC", "X2")],
    "Away v Home-or-Draw": [("1X2", "2"), ("DC", "1X")],
    "Draw v Home-or-Away": [("1X2", "X"), ("DC", "12")],
}
SUSPICIOUS_MARGIN = 0.10     # above 10% it's far more likely a mismatched game or a stale price than a real arb


def closest(match):
    """How near this match is to an arb: the lowest sum of 1/best-price over all
    covers, using 2+ bookmakers (below 1.0 = arb; 1.02 = 2% short)."""
    sums = []
    for legs in COVERS.values():
        best = []
        for leg in legs:
            offers = [(e.odds[leg], book) for book, e in match.items() if leg in e.odds]
            if not offers:
                break
            best.append(max(offers))
        else:
            if len({book for _, book in best}) >= 2:
                sums.append(sum(1 / p for p, _ in best))
    return min(sums) if sums else None


def find_arbs(match, total_stake=10_000):
    """match: {book: Event}. Returns [{cover, margin, legs: [(market, sel, book, price, stake)], suspicious}]."""
    out = []
    for cover, legs in COVERS.items():
        best = []
        for leg in legs:
            offers = [(e.odds[leg], book) for book, e in match.items() if leg in e.odds]
            if not offers:
                break
            best.append((leg, *max(offers)))
        else:
            inv = sum(1 / price for _, price, _ in best)
            if inv < 1 and len({book for _, _, book in best}) >= 2:       # a real arb uses 2+ bookmakers
                margin = 1 / inv - 1
                out.append({"cover": cover, "margin": margin, "suspicious": margin > SUSPICIOUS_MARGIN,
                            "legs": [(m, s, book, price, round(total_stake * (1 / price) / inv))
                                     for (m, s), price, book in best]})
    return out
