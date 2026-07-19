"""Accuracy report: model vs dumb baseline vs odds-implied, per market --
the "prove or disprove edge, never assume it works" check from the brief.
"""
from collections import defaultdict


def _accuracy_stats(conn):
    rows = conn.execute(
        "SELECT market, correct, baseline_correct, odds_implied_correct FROM prediction_results"
    ).fetchall()

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


def _verdict(model_acc, ref_acc, ref_name):
    if ref_acc is None:
        return None
    if model_acc > ref_acc:
        return f"beats {ref_name}"
    if model_acc < ref_acc:
        return f"loses to {ref_name}"
    return f"ties {ref_name}"


def print_report(conn):
    stats = _accuracy_stats(conn)
    if not stats:
        print("No reconciled predictions yet -- nothing to report.")
        return

    header = f"{'Market':14s} {'N':>4s} {'Model':>8s} {'Baseline':>10s} {'Odds':>8s}   Verdict"
    print(header)
    print("-" * len(header))

    def _print_row(label, s):
        model_acc = s["model_correct"] / s["n"]
        baseline_acc = s["baseline_correct"] / s["baseline_n"] if s["baseline_n"] else None
        odds_acc = s["odds_correct"] / s["odds_n"] if s["odds_n"] else None

        verdicts = [v for v in [_verdict(model_acc, baseline_acc, "baseline"),
                                 _verdict(model_acc, odds_acc, "odds")] if v]

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
    print("(Overall blends markets of very different difficulty -- e.g. exact-score")
    print(" CorrectScore vs three-way 1X2 -- read per-market rows for real signal.)")
