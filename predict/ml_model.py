"""First real (non-placeholder) model: logistic regression for the
classification markets (1X2, BTTS), Poisson regression for expected goals
feeding the same scoring-grid machinery baseline.py already uses for the
goal-based markets (OU2.5, CorrectScore, HT_1X2, HT_OU1.5).

Two deliberate choices, both explained to the user before building this:

- Trained ONLY on team form/H2H/season-rate/cross-season-prior features --
  never odds. None of the historical rows have odds (only ever scraped for
  the live upcoming fixture), so there's nothing to train that relationship
  on; and feeding odds in would make "beats odds" a circular comparison,
  which is exactly the trivial "ties odds" result baseline_v0 already gets
  on 1X2/BTTS. This model is meant to be an honest, independent test of
  whether team-stats alone carry signal beyond what the bookmaker prices in.

- Linear/GLM models (logistic + Poisson regression), not gradient-boosted
  trees. Re-tested with 6 seasons (2330+ matches, up from the original 2
  seasons/~540 rows) -- trees still lost on every single market, so this
  isn't a data-starvation artifact, it's a real property of this feature
  set. Also re-tested: a persistent cross-match Elo rating (12-point
  hyperparameter sweep) and recency-weighting the cross-season prior --
  neither beat the simpler flat-average approach already in
  predict/training_data.py's a_/b_prior_* columns. What DID help,
  confirmed robust across 5 independently-seeded CV splits: retuning C
  down to 0.01 (see below) and the dynamic prior/current-season shrinkage
  interaction features (also in training_data.py).

Retrains from scratch on every construction (see predict/training_data.py)
rather than persisting a serialized model -- fitting takes well under a
second even at thousands of rows, so there's no benefit to caching it, and
this guarantees predictions always reflect the latest data.
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

        # L1 (l1_ratio=1.0, saga solver), C=0.05, for the classifiers -- not
        # plain L2 any more. With 47 features now (cross-season prior +
        # shrinkage interactions pushed it up from 36), L2's "shrink
        # everything a little" was leaving real redundancy on the table;
        # L1's sparsity (it actually zeroes out weak coefficients rather
        # than just shrinking them) fits a sparser, more effective model --
        # verified as a robust win over the best L2 config across 5
        # independently-seeded CV fold splits (wins 4/5 outright on both
        # 1X2 and BTTS, ties the 5th on 1X2). `penalty=` is deprecated in
        # this sklearn version in favor of `l1_ratio` directly -- l1_ratio=1
        # is pure L1, 0 would be pure L2.
        # The goal regressors' alpha=1.0 is untouched -- neither the C/
        # penalty retuning nor the shrinkage features were a consistent win
        # for them (won some seeds, lost others), so there was nothing safe
        # to change there. See predict/backtest.py for the same config.
        self.clf_1x2 = LogisticRegression(max_iter=5000, C=0.05, l1_ratio=1.0, solver="saga").fit(Xs, df["result_1x2"])
        self.clf_btts = LogisticRegression(max_iter=5000, C=0.05, l1_ratio=1.0, solver="saga").fit(Xs, df["btts"])
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
        p_over15 = poisson_over_under(lambda_home + lambda_away, 1.5)
        p_over_ht15 = poisson_over_under((lambda_home + lambda_away) * HT_FT_GOAL_RATIO, 1.5)
        p_over_ht05 = poisson_over_under((lambda_home + lambda_away) * HT_FT_GOAL_RATIO, 0.5)

        return {
            "1X2": _market(probs_1x2),
            "BTTS": _market(probs_btts),
            "OU2.5": _market({"Over": p_over25, "Under": 1 - p_over25}),
            "OU1.5": _market({"Over": p_over15, "Under": 1 - p_over15}),
            "CorrectScore": _market(top_scorelines(grid)),
            "HT_1X2": _market(result_probs_from_grid(ht_grid)),
            "HT_OU1.5": _market({"Over": p_over_ht15, "Under": 1 - p_over_ht15}),
            "HT_OU0.5": _market({"Over": p_over_ht05, "Under": 1 - p_over_ht05}),
        }
