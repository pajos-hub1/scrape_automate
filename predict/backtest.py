"""Honest, out-of-fold evaluation of MLPredictor's approach BEFORE trusting
it for live predictions -- "prove or disprove edge, never assume it works"
applies to model selection itself, not just the live tracking loop.

Every number here comes from cross_val_predict: each match is scored by a
model that never saw it during training, same spirit as the live pipeline
never seeing a fixture's outcome before predicting it.
"""
from collections import Counter

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, PoissonRegressor
from sklearn.model_selection import KFold, StratifiedKFold, cross_val_predict
from sklearn.preprocessing import StandardScaler

from predict.poisson import poisson_over_under, score_grid
from predict.training_data import STAT_FEATURE_COLS, load_training_frame

HT_FT_GOAL_RATIO = 0.5
N_SPLITS = 5
RANDOM_STATE = 42


def run_backtest(conn):
    df = load_training_frame(conn)
    n = len(df)
    Xs = StandardScaler().fit_transform(df[STAT_FEATURE_COLS].values)

    results = {}
    skf = StratifiedKFold(n_splits=N_SPLITS, shuffle=True, random_state=RANDOM_STATE)
    kf = KFold(n_splits=N_SPLITS, shuffle=True, random_state=RANDOM_STATE)

    # --- 1X2 ---
    y_1x2 = df["result_1x2"].values
    preds = cross_val_predict(LogisticRegression(max_iter=2000, C=0.5), Xs, y_1x2, cv=skf)
    baseline_acc = (y_1x2 == "Home").mean()
    results["1X2"] = {"n": n, "model_acc": (preds == y_1x2).mean(),
                       "baseline_acc": baseline_acc, "baseline_label": "always-Home"}

    # --- BTTS ---
    y_btts = df["btts"].values
    preds = cross_val_predict(LogisticRegression(max_iter=2000, C=0.5), Xs, y_btts, cv=skf)
    mc = pd.Series(y_btts).value_counts().idxmax()
    results["BTTS"] = {"n": n, "model_acc": (preds == y_btts).mean(),
                        "baseline_acc": (y_btts == mc).mean(), "baseline_label": f"always-{mc}"}

    # --- Goal regressions feed OU2.5, CorrectScore, HT_1X2, HT_OU1.5 ---
    pred_home = np.clip(cross_val_predict(PoissonRegressor(alpha=1.0, max_iter=2000), Xs, df["ft_a"].values, cv=kf), 0.05, None)
    pred_away = np.clip(cross_val_predict(PoissonRegressor(alpha=1.0, max_iter=2000), Xs, df["ft_b"].values, cv=kf), 0.05, None)

    actual_total = (df["ft_a"] + df["ft_b"]).values
    actual_over25 = actual_total > 2.5
    pred_over25 = np.array([poisson_over_under(h + a, 2.5) for h, a in zip(pred_home, pred_away)]) > 0.5
    baseline_ou25 = max(actual_over25.mean(), 1 - actual_over25.mean())
    results["OU2.5"] = {"n": n, "model_acc": (pred_over25 == actual_over25).mean(),
                         "baseline_acc": baseline_ou25, "baseline_label": "most-common"}

    correct_hits = 0
    for i in range(n):
        grid = score_grid(pred_home[i], pred_away[i])
        best = max(grid, key=grid.get)
        correct_hits += int(best == (int(df["ft_a"].iloc[i]), int(df["ft_b"].iloc[i])))
    score_counts = Counter(zip(df["ft_a"], df["ft_b"]))
    mc_score, mc_count = score_counts.most_common(1)[0]
    results["CorrectScore"] = {"n": n, "model_acc": correct_hits / n,
                                "baseline_acc": mc_count / n, "baseline_label": f"always-{mc_score[0]}-{mc_score[1]}"}

    ht_mask = df["ht_result"].notna()
    n_ht = int(ht_mask.sum())
    if n_ht:
        ht_actual_result = df.loc[ht_mask, "ht_result"].values
        ht_actual_total = (df.loc[ht_mask, "ht_a"] + df.loc[ht_mask, "ht_b"]).values
        idx = np.where(ht_mask.values)[0]
        ht_pred_home = pred_home[idx] * HT_FT_GOAL_RATIO
        ht_pred_away = pred_away[idx] * HT_FT_GOAL_RATIO

        ht_pred_result = []
        for h, a in zip(ht_pred_home, ht_pred_away):
            grid = score_grid(h, a)
            p_home = sum(p for (i, j), p in grid.items() if i > j)
            p_draw = sum(p for (i, j), p in grid.items() if i == j)
            p_away = sum(p for (i, j), p in grid.items() if i < j)
            ht_pred_result.append(max([("Home", p_home), ("Draw", p_draw), ("Away", p_away)], key=lambda kv: kv[1])[0])
        ht_pred_result = np.array(ht_pred_result)

        mc_ht = pd.Series(ht_actual_result).value_counts().idxmax()
        results["HT_1X2"] = {"n": n_ht, "model_acc": (ht_pred_result == ht_actual_result).mean(),
                              "baseline_acc": (ht_actual_result == mc_ht).mean(), "baseline_label": f"always-{mc_ht}"}

        ht_actual_over15 = ht_actual_total > 1.5
        ht_pred_over15 = np.array([poisson_over_under(h + a, 1.5) for h, a in zip(ht_pred_home, ht_pred_away)]) > 0.5
        baseline_ht_ou = max(ht_actual_over15.mean(), 1 - ht_actual_over15.mean())
        results["HT_OU1.5"] = {"n": n_ht, "model_acc": (ht_pred_over15 == ht_actual_over15).mean(),
                                "baseline_acc": baseline_ht_ou, "baseline_label": "most-common"}

    return results


def print_backtest(results):
    header = f"{'Market':14s} {'N':>4s} {'ml_v1':>8s} {'Baseline':>10s}   Verdict"
    print(header)
    print("-" * len(header))
    for market in ["1X2", "BTTS", "OU2.5", "CorrectScore", "HT_1X2", "HT_OU1.5"]:
        r = results.get(market)
        if not r:
            continue
        model_acc, baseline_acc = r["model_acc"], r["baseline_acc"]
        verdict = "beats baseline" if model_acc > baseline_acc else (
            "ties baseline" if abs(model_acc - baseline_acc) < 1e-9 else "loses to baseline")
        print(f"{market:14s} {r['n']:4d} {model_acc * 100:7.1f}% {baseline_acc * 100:9.1f}%   "
              f"{verdict} ({r['baseline_label']})")
    print()
    print("5-fold cross-validated -- every prediction above is out-of-fold (the model")
    print("never saw that match during the fold it was scored in).")
