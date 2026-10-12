"""Paper betslips: simulated accumulators, ₦500 per slip, built every round.

Two slip types per round:
  - one per market (MARKETS): the BEST_PER_MARKET games whose best pick in
    that market scores highest, as a single accumulator.
  - one "10-game" slip: every game in the round, each with its single
    highest-scoring pick across all MARKETS (markets can differ per game;
    one pick per game, as Bet9ja won't combine two markets of one game).

"Best" = highest combined win probability, the average of three views:
  - model:   the live model's predicted probability (predictions table)
  - odds:    Bet9ja's closing price with its margin removed
  - history: how often similar past picks won -- same market, same
             selection, same 5%-wide odds band -- shrunk toward the odds
             view while the sample is small (HISTORY_PRIOR pseudo-picks).
             Built ONLY from rounds strictly before the one being picked,
             so the backtest never sees a result it couldn't have known.

Nothing here places real bets. Slips are recomputed from the DB on every
run; because each round's picks only use earlier rounds' results and odds
never move once posted (see the 2026-10-10 edge analysis), the result is
identical to having recorded the slips before kickoff.
"""
import json
from collections import defaultdict

from track.reconcile import actual_labels
from track.report import _ensure_closing_prices

MARKETS = ["1X2", "OU1.5", "HT_OU0.5", "BTTS", "HT_OU1.5", "OU2.5"]
STAKE_NAIRA = 500
BEST_PER_MARKET = 5
HISTORY_PRIOR = 30
MODEL_VERSION = "baseline_v0"
# How picks are ranked: "combined" (model + odds + history, as requested),
# or "odds". The 745-round backtest (2026-10-12) lost 22.4% ranked
# "combined" vs 14.6% ranked by "odds" alone -- the history view mostly
# added noise. Kept as requested; switch here to compare live.
SCORE = "combined"


def _bucket(p):
    return min(int(p * 20), 19)


def load_rounds(conn, model_version=MODEL_VERSION):
    """Chronological rounds: [{key, label, status, games: {game_id: {
    "teams": (a, b), "markets": {market: [candidate, ...]}}}}].

    A candidate is one selection of one market of one game:
    {selection, price, model_p, odds_p, won (bool, or None if unplayed)}.
    Played rounds come from reconciled predictions (one fixture per match);
    the single upcoming round is the latest unreconciled fixture batch.
    """
    _ensure_closing_prices(conn)
    in_markets = ",".join("?" for _ in MARKETS)

    prices = defaultdict(dict)
    for r in conn.execute(
        f"SELECT fixture_id, market, selection, price FROM temp.closing_prices WHERE market IN ({in_markets})",
        MARKETS,
    ):
        prices[(r["fixture_id"], r["market"])][r["selection"]] = r["price"]

    preds = defaultdict(dict)
    for r in conn.execute(
        f"""SELECT fixture_ref, market, probabilities FROM all_predictions
            WHERE model_version = ? AND market IN ({in_markets})""",
        [model_version] + MARKETS,
    ):
        preds[r["fixture_ref"]][r["market"]] = json.loads(r["probabilities"])

    # Played: fixture -> match via any reconciled prediction; keep the
    # newest fixture per match (repeat polls can create several).
    fixture_for_match = {}
    for r in conn.execute(
        """SELECT DISTINCT p.fixture_ref, pr.match_ref
           FROM all_prediction_results pr JOIN all_predictions p ON p.prediction_id = pr.prediction_id
           WHERE p.model_version = ?""",
        (model_version,),
    ):
        if r["fixture_ref"] > fixture_for_match.get(r["match_ref"], -1):
            fixture_for_match[r["match_ref"]] = r["fixture_ref"]

    matches = {r["match_id"]: dict(r) for r in conn.execute(
        """SELECT match_id, season_id, round_number, team_a, team_b, ft_a, ft_b, ht_a, ht_b, scraped_at
           FROM matches WHERE ft_a IS NOT NULL"""
    )}

    rounds = {}
    for match_id, fixture_id in fixture_for_match.items():
        m = matches.get(match_id)
        if m is None:
            continue
        key = (m["season_id"], m["round_number"])
        rnd = rounds.setdefault(key, {
            "key": key, "label": f"S{key[0]} R{key[1]}", "status": "settled",
            "sort": m["scraped_at"], "games": {},
        })
        rnd["sort"] = min(rnd["sort"], m["scraped_at"])
        labels = actual_labels(m)
        rnd["games"][match_id] = {
            "teams": (m["team_a"], m["team_b"]),
            "markets": _candidates(fixture_id, preds, prices, labels),
        }

    ordered = sorted(rounds.values(), key=lambda r: r["sort"])

    # Upcoming: the latest batch of fixtures with live, unreconciled predictions.
    latest = conn.execute(
        """SELECT f.scraped_at, f.round_number FROM predictions p
           JOIN fixtures f ON f.fixture_id = p.fixture_ref
           LEFT JOIN prediction_results pr ON pr.prediction_id = p.prediction_id
           WHERE pr.result_id IS NULL AND p.model_version = ?
           ORDER BY f.scraped_at DESC LIMIT 1""",
        (model_version,),
    ).fetchone()
    if latest:
        upcoming = {"key": None, "label": f"Round {latest['round_number']} (upcoming)",
                    "status": "upcoming", "sort": latest["scraped_at"], "games": {}}
        for f in conn.execute(
            "SELECT fixture_id, team_a, team_b FROM fixtures WHERE scraped_at = ? ORDER BY match_number",
            (latest["scraped_at"],),
        ):
            cands = _candidates(f["fixture_id"], preds, prices, None)
            if cands:
                upcoming["games"][f"fx{f['fixture_id']}"] = {"teams": (f["team_a"], f["team_b"]), "markets": cands}
        if upcoming["games"]:
            ordered.append(upcoming)
    return ordered


def _candidates(fixture_id, preds, prices, labels):
    out = {}
    for market in MARKETS:
        model = preds.get(fixture_id, {}).get(market)
        book = prices.get((fixture_id, market))
        if not model or not book:
            continue
        total = sum(1 / p for p in book.values())
        cands = []
        for sel, price in book.items():
            if sel not in model:
                continue
            won = None
            if labels is not None:
                actual = labels.get(market)
                if actual is None:
                    continue
                won = actual == sel
            cands.append({"selection": sel, "price": price, "model_p": model[sel],
                          "odds_p": (1 / price) / total, "won": won})
        if cands:
            out[market] = cands
    return out


def build_slips(rounds, score=SCORE):
    """Walks rounds in order, building each round's slips from history
    learned on earlier rounds only, then folding that round's results into
    history. score: "combined" (default), or "odds"/"model" for comparison.
    Returns [{round, slips: [slip, ...]}]."""
    history = defaultdict(lambda: [0, 0])   # (market, selection, bucket) -> [n, wins]
    out = []
    for rnd in rounds:
        scored_games = {}
        for gid, game in rnd["games"].items():
            scored = {}
            for market, cands in game["markets"].items():
                for c in cands:
                    n, w = history[(market, c["selection"], _bucket(c["odds_p"]))]
                    hist_p = (w + HISTORY_PRIOR * c["odds_p"]) / (n + HISTORY_PRIOR)
                    s = {"combined": (c["model_p"] + c["odds_p"] + hist_p) / 3,
                         "odds": c["odds_p"], "model": c["model_p"]}[score]
                    leg = dict(c, market=market, hist_p=hist_p, score=s, teams=game["teams"])
                    if market not in scored or s > scored[market]["score"]:
                        scored[market] = leg       # best selection of this market for this game
            scored_games[gid] = scored

        slips = []
        for market in MARKETS:
            legs = [g[market] for g in scored_games.values() if market in g]
            legs.sort(key=lambda leg: leg["score"], reverse=True)
            if len(legs) >= BEST_PER_MARKET:
                slips.append(_slip(f"{market} top {BEST_PER_MARKET}", legs[:BEST_PER_MARKET]))
        best_per_game = [max(g.values(), key=lambda leg: leg["score"]) for g in scored_games.values() if g]
        if len(best_per_game) >= 8:   # a full round is 10; tolerate a couple of unpriced games
            slips.append(_slip(f"{len(best_per_game)}-game", best_per_game))
        out.append({"round": rnd, "slips": slips})

        if rnd["status"] == "settled":
            for game in rnd["games"].values():
                for market, cands in game["markets"].items():
                    for c in cands:
                        h = history[(market, c["selection"], _bucket(c["odds_p"]))]
                        h[0] += 1
                        h[1] += int(c["won"])
    return out


def _slip(name, legs):
    odds = 1.0
    for leg in legs:
        odds *= leg["price"]
    settled = all(leg["won"] is not None for leg in legs)
    won = settled and all(leg["won"] for leg in legs)
    return {
        "name": name, "legs": legs, "odds": odds, "stake": STAKE_NAIRA,
        "settled": settled, "won": won if settled else None,
        "returned": STAKE_NAIRA * odds if won else 0.0,
        "legs_won": sum(1 for leg in legs if leg["won"]),
    }


def summarize(built):
    """{slip type: totals} over settled slips. Slip type = name without the
    game count, so '10-game' and '9-game' slips pool together."""
    totals = defaultdict(lambda: {"slips": 0, "won": 0, "staked": 0.0, "returned": 0.0,
                                  "biggest_win": 0.0, "longest_losing": 0, "_streak": 0})
    for entry in built:
        for slip in entry["slips"]:
            if not slip["settled"]:
                continue
            kind = "All-games slip" if slip["name"].endswith("-game") else slip["name"]
            t = totals[kind]
            t["slips"] += 1
            t["staked"] += slip["stake"]
            t["returned"] += slip["returned"]
            if slip["won"]:
                t["won"] += 1
                t["biggest_win"] = max(t["biggest_win"], slip["returned"])
                t["_streak"] = 0
            else:
                t["_streak"] += 1
                t["longest_losing"] = max(t["longest_losing"], t["_streak"])
    for t in totals.values():
        t.pop("_streak")
        t["profit"] = t["returned"] - t["staked"]
    return dict(totals)
