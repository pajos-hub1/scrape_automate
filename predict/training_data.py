"""Historical match feature/outcome pairs for training and backtesting --
shared between predict/ml_model.py and predict/backtest.py so both draw on
the exact same data and feature set.

Deliberately excludes odds_* columns: none of the 540 historical rows have
them (odds are only ever scraped for the live upcoming fixture, never
backfilled), and training on odds would make "beats odds" a circular
comparison anyway -- see predict/ml_model.py's module docstring.
"""
import numpy as np
import pandas as pd

STAT_FEATURE_COLS = [
    "a_form_games", "a_form_pts", "a_form_wins", "a_form_draws", "a_form_losses", "a_form_gf", "a_form_ga",
    "b_form_games", "b_form_pts", "b_form_wins", "b_form_draws", "b_form_losses", "b_form_gf", "b_form_ga",
    "h2h_games", "h2h_a_wins", "h2h_b_wins", "h2h_draws", "h2h_avg_goals",
    "a_home_played", "a_home_wins", "a_home_draws", "a_home_losses", "a_home_gf", "a_home_ga",
    "b_away_played", "b_away_wins", "b_away_draws", "b_away_losses", "b_away_gf", "b_away_ga",
    "a_season_pts_rate", "a_season_gf_avg", "a_season_ga_avg",
    "b_season_pts_rate", "b_season_gf_avg", "b_season_ga_avg",
]


def load_training_frame(conn):
    """Every played match with computed features, joined to its actual
    result (FT and HT). One row per historical match (540 as of writing,
    across both preserved seasons)."""
    df = pd.read_sql_query(
        """SELECT f.*, m.ft_a, m.ft_b, m.ht_a, m.ht_b
           FROM features f JOIN matches m ON m.match_id = f.match_ref
           WHERE f.match_ref IS NOT NULL""",
        conn,
    )
    df[STAT_FEATURE_COLS] = df[STAT_FEATURE_COLS].fillna(0)
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
    """row: a dict (e.g. a `features` table row for the live fixture).
    Returns a 1-row array in STAT_FEATURE_COLS order, missing values -> 0,
    matching how load_training_frame() fills historical rows."""
    return np.array([[row.get(c) or 0 for c in STAT_FEATURE_COLS]])
