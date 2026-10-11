"""Accuracy report: model vs dumb baseline vs odds-implied, per market --
the "prove or disprove edge, never assume it works" check from the brief.

Once more than one model_version has live predictions (baseline_v0 and
ml_v1 running in parallel), the report prints one table per model plus a
head-to-head comparison, rather than blending both models' predictions
into one misleading "model" number.
"""
import json
import math
from collections import defaultdict


def _season_scope(season_ids):
    """(join, where-clause, params) restricting prediction_results `pr` to
    matches from the given seasons. season_ids=None means all-time."""
    if season_ids is None:
        return "", None, []
    placeholders = ",".join("?" for _ in season_ids) or "NULL"
    return (" JOIN matches sm ON sm.match_id = pr.match_ref",
            f"sm.season_id IN ({placeholders})", list(season_ids))


def accuracy_stats(conn, model_version=None, season_ids=None):
    """model_version=None blends every model's predictions together (the
    original single-model behavior, kept as the default so existing
    callers -- the dashboard's overall tile -- don't need to change).
    Pass a specific model_version (or a list of them) to isolate just that
    track record,
    and/or season_ids to restrict to matches from those seasons.
    """
    join, season_where, params = _season_scope(season_ids)
    query = """SELECT pr.market, pr.correct, pr.baseline_correct, pr.odds_implied_correct
               FROM all_prediction_results pr
               JOIN all_predictions p ON p.prediction_id = pr.prediction_id""" + join
    where = [season_where] if season_where else []
    if isinstance(model_version, (list, tuple)):
        where.append(f"p.model_version IN ({','.join('?' for _ in model_version)})")
        params.extend(model_version)
    elif model_version is not None:
        where.append("p.model_version = ?")
        params.append(model_version)
    if where:
        query += " WHERE " + " AND ".join(where)
    rows = conn.execute(query, params).fetchall()

    stats = defaultdict(lambda: {"n": 0, "model_correct": 0,
                                  "baseline_n": 0, "baseline_correct": 0,
                                  "odds_n": 0, "odds_correct": 0})
    for r in rows:
        s = stats[r["market"]]
        s["n"] += 1
        s["model_correct"] += r["correct"]
        if r["baseline_correct"] is not None:
            s["baseline_n"] += 1
            s["baseline_correct"] += r["baseline_correct"]
        if r["odds_implied_correct"] is not None:
            s["odds_n"] += 1
            s["odds_correct"] += r["odds_implied_correct"]
    return stats


def _ensure_closing_prices(conn):
    """TEMP table of each (fixture, market, selection)'s last captured
    price -- the closing price a bet placed at prediction time would have
    got. Built once per connection from all_odds (live + archives)."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_temp_master WHERE type = 'table' AND name = 'closing_prices'"
    ).fetchone()
    if exists:
        return
    # SQLite's bare-column rule: with MAX() in the select list, `price`
    # comes from the same row as the latest captured_at.
    conn.execute(
        """CREATE TEMP TABLE closing_prices AS
           SELECT fixture_id, market, selection, price, MAX(captured_at) AS captured_at
           FROM all_odds GROUP BY fixture_id, market, selection"""
    )
    conn.execute("CREATE INDEX temp.idx_closing_prices ON closing_prices(fixture_id, market, selection)")


def profit_stats(conn, model_version, season_ids=None):
    """Flat 1-unit stake on every reconciled prediction, at the closing
    price of the predicted selection -- the number accuracy can't show.
    Accuracy says how often a pick wins; profit says whether the price
    paid for those wins covers the losses. In a market whose odds are
    well calibrated (Zoom's are: across ~7,400 fixtures, 1X2 outcomes the
    odds priced at 25% happened 25.0% of the time, 55% -> 54.4%, etc.),
    every strategy trends toward minus the bookmaker margin (~5.7% on 1X2).

    Also computes the same flat-stake result for always backing the odds
    favourite, as the reference point. Predictions with no matching price
    (e.g. old CorrectScore "Other" picks) are skipped, not counted as losses.
    """
    _ensure_closing_prices(conn)
    join, season_where, params = _season_scope(season_ids)
    rows = conn.execute(
        """SELECT pr.market, pr.correct, pr.odds_implied_correct,
                  mp.price AS model_price, fp.price AS fav_price
           FROM all_prediction_results pr
           JOIN all_predictions p ON p.prediction_id = pr.prediction_id
           LEFT JOIN temp.closing_prices mp
             ON mp.fixture_id = p.fixture_ref AND mp.market = pr.market
            AND mp.selection = pr.predicted_label
           LEFT JOIN temp.closing_prices fp
             ON fp.fixture_id = p.fixture_ref AND fp.market = pr.market
            AND fp.selection = pr.odds_implied_label"""
        + join + " WHERE p.model_version = ?" + (f" AND {season_where}" if season_where else ""),
        [model_version] + params,
    ).fetchall()

    stats = defaultdict(lambda: {"bets": 0, "returns": 0.0, "fav_bets": 0, "fav_returns": 0.0})
    for r in rows:
        s = stats[r["market"]]
        if r["model_price"] is not None:
            s["bets"] += 1
            s["returns"] += r["model_price"] if r["correct"] else 0.0
        if r["fav_price"] is not None and r["odds_implied_correct"] is not None:
            s["fav_bets"] += 1
            s["fav_returns"] += r["fav_price"] if r["odds_implied_correct"] else 0.0
    return stats


PROB_MARKETS = ["1X2", "BTTS", "OU2.5", "OU1.5", "HT_1X2", "HT_OU1.5", "HT_OU0.5"]


def probability_stats(conn, model_version):
    """How good the predicted *probabilities* are, not just the top pick:
    mean log-loss (-ln p(actual); punishes confident misses hard) and mean
    multi-class Brier score (sum of squared errors over every outcome), both
    lower-is-better. Each is scored against the de-margined closing odds on
    exactly the same predictions, so "model log-loss < odds log-loss" would
    mean the model's probabilities beat the market's.

    CorrectScore is left out: the model reports a top-8 + "Other" grid
    while the odds price ~35 exact scores, so the two aren't comparable.
    """
    _ensure_closing_prices(conn)
    book = {}
    for r in conn.execute(
        "SELECT fixture_id, market, selection, price FROM temp.closing_prices "
        f"WHERE market IN ({','.join('?' for _ in PROB_MARKETS)})", PROB_MARKETS
    ):
        book.setdefault((r["fixture_id"], r["market"]), {})[r["selection"]] = r["price"]

    rows = conn.execute(
        f"""SELECT pr.market, pr.actual_label, p.fixture_ref, p.probabilities
            FROM all_prediction_results pr
            JOIN all_predictions p ON p.prediction_id = pr.prediction_id
            WHERE p.model_version = ? AND pr.market IN ({','.join('?' for _ in PROB_MARKETS)})""",
        [model_version] + PROB_MARKETS,
    ).fetchall()

    stats = defaultdict(lambda: {"n": 0, "model_ll": 0.0, "odds_ll": 0.0, "model_brier": 0.0, "odds_brier": 0.0})
    for r in rows:
        prices = book.get((r["fixture_ref"], r["market"]))
        if not prices:
            continue
        inv = {sel: 1 / price for sel, price in prices.items()}
        total = sum(inv.values())
        odds_probs = {sel: v / total for sel, v in inv.items()}
        model_probs = json.loads(r["probabilities"])
        if r["actual_label"] not in odds_probs:
            continue

        s = stats[r["market"]]
        s["n"] += 1
        s["model_ll"] += _log_loss(model_probs, r["actual_label"])
        s["odds_ll"] += _log_loss(odds_probs, r["actual_label"])
        s["model_brier"] += _brier(model_probs, r["actual_label"])
        s["odds_brier"] += _brier(odds_probs, r["actual_label"])
    return stats


def _log_loss(probs, actual):
    return -math.log(max(probs.get(actual, 0.0), 1e-6))


def _brier(probs, actual):
    labels = set(probs) | {actual}
    return sum((probs.get(k, 0.0) - (1.0 if k == actual else 0.0)) ** 2 for k in labels)


def roi(returns, bets):
    return (returns - bets) / bets if bets else None


def list_model_versions(conn):
    return [r["model_version"] for r in conn.execute(
        "SELECT DISTINCT model_version FROM all_predictions ORDER BY model_version"
    )]


def verdict(model_acc, ref_acc, ref_name):
    if ref_acc is None:
        return None
    if model_acc > ref_acc:
        return f"beats {ref_name}"
    if model_acc < ref_acc:
        return f"loses to {ref_name}"
    return f"ties {ref_name}"


def _print_table(stats, model_label):
    header = f"{'Market':14s} {'N':>4s} {model_label:>8s} {'Baseline':>10s} {'Odds':>8s}   Verdict"
    print(header)
    print("-" * len(header))

    def _print_row(label, s):
        model_acc = s["model_correct"] / s["n"]
        baseline_acc = s["baseline_correct"] / s["baseline_n"] if s["baseline_n"] else None
        odds_acc = s["odds_correct"] / s["odds_n"] if s["odds_n"] else None

        verdicts = [v for v in [verdict(model_acc, baseline_acc, "baseline"),
                                 verdict(model_acc, odds_acc, "odds")] if v]

        baseline_str = f"{baseline_acc * 100:8.1f}%" if baseline_acc is not None else "     n/a"
        odds_str = f"{odds_acc * 100:6.1f}%" if odds_acc is not None else "   n/a"
        print(f"{label:14s} {s['n']:4d} {model_acc * 100:7.1f}% {baseline_str}   {odds_str}   "
              f"{', '.join(verdicts) if verdicts else 'n/a'}")

    for market in sorted(stats):
        _print_row(market, stats[market])

    overall = {"n": 0, "model_correct": 0, "baseline_n": 0, "baseline_correct": 0,
               "odds_n": 0, "odds_correct": 0}
    for s in stats.values():
        for k in overall:
            overall[k] += s[k]
    print("-" * len(header))
    _print_row("Overall", overall)


def _print_head_to_head(conn, models):
    if len(models) < 2:
        return
    print("\n=== Head-to-head (live-tracked models, same reconciled predictions) ===")
    per_model = {m: accuracy_stats(conn, model_version=m) for m in models}
    markets = sorted(set().union(*(s.keys() for s in per_model.values())))

    header = f"{'Market':14s} " + " ".join(f"{m:>14s}" for m in models)
    print(header)
    print("-" * len(header))
    for market in markets:
        cells = []
        for m in models:
            s = per_model[m].get(market)
            cells.append(f"{s['model_correct'] / s['n'] * 100:13.1f}%" if s else f"{'n/a':>14s}")
        print(f"{market:14s} " + " ".join(cells))


def _print_profit_table(stats):
    header = f"{'Market':14s} {'Bets':>6s} {'Profit':>9s} {'ROI':>7s}   {'Fav ROI':>7s}"
    print(header)
    print("-" * len(header))
    tot = {"bets": 0, "returns": 0.0, "fav_bets": 0, "fav_returns": 0.0}

    def _row(label, s):
        r = roi(s["returns"], s["bets"])
        fr = roi(s["fav_returns"], s["fav_bets"])
        r_str = f"{r * 100:6.1f}%" if r is not None else "    n/a"
        fr_str = f"{fr * 100:6.1f}%" if fr is not None else "    n/a"
        print(f"{label:14s} {s['bets']:6d} {s['returns'] - s['bets']:+9.1f} {r_str}   {fr_str}")

    for market in sorted(stats):
        _row(market, stats[market])
        for k in tot:
            tot[k] += stats[market][k]
    print("-" * len(header))
    _row("Overall", tot)


def _print_probability_table(stats):
    header = f"{'Market':10s} {'N':>6s} {'LogLoss':>8s} {'Odds LL':>8s} {'Brier':>7s} {'Odds Br':>8s}"
    print(header)
    print("-" * len(header))
    for market in PROB_MARKETS:
        s = stats.get(market)
        if not s or not s["n"]:
            continue
        n = s["n"]
        print(f"{market:10s} {n:6d} {s['model_ll'] / n:8.4f} {s['odds_ll'] / n:8.4f} "
              f"{s['model_brier'] / n:7.4f} {s['odds_brier'] / n:8.4f}")


def print_report(conn):
    models = list_model_versions(conn)
    if not models:
        print("No reconciled predictions yet -- nothing to report.")
        return

    for m in models:
        stats = accuracy_stats(conn, model_version=m)
        if not stats:
            print(f"=== {m}: no reconciled predictions yet ===\n")
            continue
        print(f"=== {m} ===")
        _print_table(stats, m)
        print()

    _print_head_to_head(conn, models)

    print("\n=== Flat-stake profit (1 unit per prediction, at closing odds) ===")
    print("(Fav ROI = always backing the odds favourite instead, same fixtures.)")
    for m in models:
        print(f"\n--- {m} ---")
        _print_profit_table(profit_stats(conn, m))

    print("\n=== Probability quality (lower is better; same predictions scored both ways) ===")
    for m in models:
        print(f"\n--- {m} ---")
        _print_probability_table(probability_stats(conn, m))
    print("\n(Overall/per-market blends CorrectScore's exact-match difficulty with three-way")
    print(" 1X2 etc -- read individual market rows for real signal, not just Overall.)")
