"""First real (non-placeholder) model: logistic regression for the
classification markets (1X2, BTTS), Poisson regression for expected goals
feeding the same scoring-grid machinery baseline.py already uses for the
goal-based markets (OU2.5, CorrectScore, HT_1X2, HT_OU1.5).

Two deliberate choices, both explained to the user before building this:

- Trained ONLY on team form/H2H/season-rate features -- never odds. None of
  the 540 historical rows have odds (only ever scraped for the live
  upcoming fixture), so there's nothing to train that relationship on; and
  feeding odds in would make "beats odds" a circular comparison, which is
  exactly the trivial "ties odds" result baseline_v0 already gets on
  1X2/BTTS. This model is meant to be an honest, independent test of
  whether team-stats alone carry signal beyond what the bookmaker prices in.

- Linear/GLM models (logistic + Poisson regression), not gradient-boosted
  trees. On 540 rows of RNG-driven outcomes, a tree ensemble is far more
  likely to fit noise and look good in-sample while carrying no real
  signal. Revisit with a tree-based model once there's dramatically more
  data (the ballpark discussed: dozens of preserved seasons, not 2).

Retrains from scratch on every construction (see predict/training_data.py)
rather than persisting a serialized model -- fitting on ~540x36 floats
takes well under a second, so there's no benefit to caching it, and this
guarantees predictions always reflect the latest data.
"""
import numpy as np
from sklearn.linear_model import LogisticRegression, PoissonRegressor
from sklearn.preprocessing import StandardScaler

from predict.common import market_result as _market
from predict.interface import Predictor
from predict.poisson import poisson_over_under, result_probs_from_grid, score_grid, top_scorelines
from predict.training_data import STAT_FEATURE_COLS, load_training_frame, row_to_feature_vector

HT_FT_GOAL_RATIO = 0.5  # measured from this project's scraped data -- see predict/baseline.py


class MLPredictor(Predictor):
    model_version = "ml_v1"

    def __init__(self, conn):
        df = load_training_frame(conn)
        self.n_train = len(df)

        X = df[STAT_FEATURE_COLS].values
        self.scaler = StandardScaler().fit(X)
        Xs = self.scaler.transform(X)

        self.clf_1x2 = LogisticRegression(max_iter=2000, C=0.5).fit(Xs, df["result_1x2"])
        self.clf_btts = LogisticRegression(max_iter=2000, C=0.5).fit(Xs, df["btts"])
        self.reg_home_goals = PoissonRegressor(alpha=1.0, max_iter=2000).fit(Xs, df["ft_a"])
        self.reg_away_goals = PoissonRegressor(alpha=1.0, max_iter=2000).fit(Xs, df["ft_b"])

    def predict_markets(self, row):
        xs = self.scaler.transform(row_to_feature_vector(row))

        probs_1x2 = dict(zip(self.clf_1x2.classes_, self.clf_1x2.predict_proba(xs)[0]))
        probs_btts = dict(zip(self.clf_btts.classes_, self.clf_btts.predict_proba(xs)[0]))

        lambda_home = max(float(self.reg_home_goals.predict(xs)[0]), 0.05)
        lambda_away = max(float(self.reg_away_goals.predict(xs)[0]), 0.05)

        grid = score_grid(lambda_home, lambda_away)
        ht_grid = score_grid(lambda_home * HT_FT_GOAL_RATIO, lambda_away * HT_FT_GOAL_RATIO)

        p_over25 = poisson_over_under(lambda_home + lambda_away, 2.5)
        p_over_ht15 = poisson_over_under((lambda_home + lambda_away) * HT_FT_GOAL_RATIO, 1.5)

        return {
            "1X2": _market(probs_1x2),
            "BTTS": _market(probs_btts),
            "OU2.5": _market({"Over": p_over25, "Under": 1 - p_over25}),
            "CorrectScore": _market(top_scorelines(grid)),
            "HT_1X2": _market(result_probs_from_grid(ht_grid)),
            "HT_OU1.5": _market({"Over": p_over_ht15, "Under": 1 - p_over_ht15}),
        }
