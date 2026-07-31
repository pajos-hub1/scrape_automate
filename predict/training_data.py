"""Historical match feature/outcome pairs for training and backtesting --
shared between predict/ml_model.py and predict/backtest.py so both draw on
the exact same data and feature set.

Deliberately excludes odds_* columns: none of the historical rows have them
(odds are only ever scraped for the live upcoming fixture, never
backfilled), and training on odds would make "beats odds" a circular
comparison anyway -- see predict/ml_model.py's module docstring.
"""
import numpy as np
import pandas as pd

_RAW_STAT_COLS = [
    "a_form_games", "a_form_pts", "a_form_wins", "a_form_draws", "a_form_losses", "a_form_gf", "a_form_ga",
    "b_form_games", "b_form_pts", "b_form_wins", "b_form_draws", "b_form_losses", "b_form_gf", "b_form_ga",
    "h2h_games", "h2h_a_wins", "h2h_b_wins", "h2h_draws", "h2h_avg_goals",
    "a_home_played", "a_home_wins", "a_home_draws", "a_home_losses", "a_home_gf", "a_home_ga",
    "b_away_played", "b_away_wins", "b_away_draws", "b_away_losses", "b_away_gf", "b_away_ga",
    "a_season_pts_rate", "a_season_gf_avg", "a_season_ga_avg",
    "b_season_pts_rate", "b_season_gf_avg", "b_season_ga_avg",
    "a_prior_pts_rate", "a_prior_gf_avg", "a_prior_ga_avg",
    "b_prior_pts_rate", "b_prior_gf_avg", "b_prior_ga_avg",
]

_SHRINKAGE_COLS = ["a_prior_x_early", "a_season_x_late", "b_prior_x_early", "b_season_x_late"]

STAT_FEATURE_COLS = _RAW_STAT_COLS + _SHRINKAGE_COLS

MAX_ROUND = 38  # season length -- season_progress denominator, see _add_shrinkage_features


def _add_shrinkage_features(df):
    """Dynamic prior-vs-current-season blend: season_progress = round_number
    / 38, clipped to [0,1] -- 0 at a season's very start, 1 by its end.
    a_prior_x_early/b_prior_x_early carry the cross-season prior weighted
    toward EARLY rounds (when within-season form/season-rate features are
    still thin or empty); a_season_x_late/b_season_x_late are the mirror,
    weighting the season-to-date rate toward LATE rounds once it's had
    enough games to mean something. Plain linear regression on the two raw
    columns separately can't express this "which one to trust depends on
    how far into the season we are" relationship -- these interaction terms
    make it explicit.

    Backtested (5-fold CV, verified robust across 5 independently-seeded
    fold splits) to meaningfully help the 1X2/BTTS classifiers; the goal-
    based Poisson regressors (OU2.5/CorrectScore/HT_*) saw no consistent
    change either way, so this is purely additive rather than a feature-set
    swap -- see predict/ml_model.py and predict/backtest.py for where C got
    retuned accordingly (classifiers only, regressors unchanged).

    round_number defaults to 0 when absent (e.g. predict/build.py's orphan-
    batch path, a brand-new season's Round 1 predicted before it has a
    features row at all) -- season_progress=0 there, which correctly puts
    full weight on the prior and zero on the (nonexistent) season rate.
    """
    progress = (df["round_number"].fillna(0).clip(upper=MAX_ROUND) / MAX_ROUND)
    df["a_prior_x_early"] = df["a_prior_pts_rate"] * (1 - progress)
    df["a_season_x_late"] = df["a_season_pts_rate"] * progress
    df["b_prior_x_early"] = df["b_prior_pts_rate"] * (1 - progress)
    df["b_season_x_late"] = df["b_season_pts_rate"] * progress
    return df


def load_training_frame(conn):
    """Every played match with computed features, joined to its actual
    result (FT and HT). One row per historical match."""
    df = pd.read_sql_query(
        """SELECT f.*, m.ft_a, m.ft_b, m.ht_a, m.ht_b
           FROM features f JOIN matches m ON m.match_id = f.match_ref
           WHERE f.match_ref IS NOT NULL""",
        conn,
    )
    df[_RAW_STAT_COLS] = df[_RAW_STAT_COLS].fillna(0)
    df = _add_shrinkage_features(df)
    df["result_1x2"] = np.select(
        [df["ft_a"] > df["ft_b"], df["ft_a"] < df["ft_b"]], ["Home", "Away"], default="Draw"
    )
    df["btts"] = np.where((df["ft_a"] > 0) & (df["ft_b"] > 0), "Yes", "No")
    has_ht = df["ht_a"].notna() & df["ht_b"].notna()
    df["ht_result"] = np.where(
        has_ht,
        np.select([df["ht_a"] > df["ht_b"], df["ht_a"] < df["ht_b"]], ["Home", "Away"], default="Draw"),
        None,
    )
    return df


def row_to_feature_vector(row):
    """row: a dict (e.g. a `features` table row for the live fixture, or
    predict/build.py's orphan-batch synthetic row). Returns a 1-row array
    in STAT_FEATURE_COLS order, missing values -> 0, matching how
    load_training_frame() fills historical rows -- including computing the
    same shrinkage interaction terms from whatever round_number/prior/
    season values are present.
    """
    round_number = row.get("round_number") or 0
    progress = min(round_number, MAX_ROUND) / MAX_ROUND
    a_prior, a_season = row.get("a_prior_pts_rate") or 0, row.get("a_season_pts_rate") or 0
    b_prior, b_season = row.get("b_prior_pts_rate") or 0, row.get("b_season_pts_rate") or 0

    full_row = dict(row)
    full_row["a_prior_x_early"] = a_prior * (1 - progress)
    full_row["a_season_x_late"] = a_season * progress
    full_row["b_prior_x_early"] = b_prior * (1 - progress)
    full_row["b_season_x_late"] = b_season * progress

    return np.array([[full_row.get(c) or 0 for c in STAT_FEATURE_COLS]])
