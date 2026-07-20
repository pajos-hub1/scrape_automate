"""Accuracy report: model vs dumb baseline vs odds-implied, per market --
the "prove or disprove edge, never assume it works" check from the brief.

Once more than one model_version has live predictions (baseline_v0 and
ml_v1 running in parallel), the report prints one table per model plus a
head-to-head comparison, rather than blending both models' predictions
into one misleading "model" number.
"""
from collections import defaultdict


def accuracy_stats(conn, model_version=None):
    """model_version=None blends every model's predictions together (the
    original single-model behavior, kept as the default so existing
    callers -- the dashboard's overall tile -- don't need to change).
    Pass a specific model_version to isolate just that model's track record.
    """
    query = """SELECT pr.market, pr.correct, pr.baseline_correct, pr.odds_implied_correct
               FROM prediction_results pr
               JOIN predictions p ON p.prediction_id = pr.prediction_id"""
    params = ()
    if model_version is not None:
        query += " WHERE p.model_version = ?"
        params = (model_version,)
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


def list_model_versions(conn):
    return [r["model_version"] for r in conn.execute(
        "SELECT DISTINCT model_version FROM predictions ORDER BY model_version"
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
    print("\n(Overall/per-market blends CorrectScore's exact-match difficulty with three-way")
    print(" 1X2 etc -- read individual market rows for real signal, not just Overall.)")
