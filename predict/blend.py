"""Blends bookmaker odds with ml_v1's own team-stats classifier for 1X2 --
the one place this measurably helps. Verified with a FIXED blend weight
(not re-tuned per test, which would just be overfitting the weight search)
across 7 independently-seeded 5-fold CV splits on 1,914 historical matches
with real 1X2 odds: the blend ties-or-beats odds alone in every single
seed (odds alone is a constant 53.3%; the blend ranged 53.3-53.7%), only
possible now that ml_v1 itself got good enough (this session's cross-
season prior + shrinkage + L1 changes) to add anything on top of the
market. The same test on BTTS did NOT show this pattern -- the "best"
blend weight bounced around wildly per seed (0.70-0.95) with marginal-to-
negative gains over odds alone, a sign of noise, not signal -- so BTTS
(and every other market) is left exactly as baseline_v0 already computes
it: odds when available, Poisson fallback otherwise.

Deliberately a NEW model rather than a change to ml_v1 or baseline_v0:
ml_v1 must stay odds-free to remain an honest, non-circular test of
whether team-stats alone beat the market; baseline_v0 is the placeholder
"just use whatever's available" reference. This is the first model that's
actually trying to be the best live predictor, not a diagnostic baseline.
"""
from predict.baseline import BaselinePredictor
from predict.common import market_result as _market
from predict.common import normalize as _normalize
from predict.interface import Predictor
from predict.ml_model import MLPredictor

ODDS_WEIGHT_1X2 = 0.6  # toward odds; (1 - this) toward ml_v1's team-stats classifier


class BlendPredictor(Predictor):
    model_version = "blend_v1"

    def __init__(self, conn):
        self._baseline = BaselinePredictor(conn)
        self._ml = MLPredictor(conn)

    def predict_markets(self, row):
        markets = dict(self._baseline.predict_markets(row))
        markets["1X2"] = self._predict_1x2_blend(row)
        return markets

    def _predict_1x2_blend(self, row):
        oh, od, oa = row.get("odds_home_prob"), row.get("odds_draw_prob"), row.get("odds_away_prob")
        ml_probs = self._ml.predict_markets(row)["1X2"]["probabilities"]

        if oh is None or od is None or oa is None:
            return _market(_normalize(ml_probs))

        odds_probs = _normalize({"Home": oh, "Draw": od, "Away": oa})
        blended = {
            k: ODDS_WEIGHT_1X2 * odds_probs[k] + (1 - ODDS_WEIGHT_1X2) * ml_probs[k]
            for k in odds_probs
        }
        return _market(_normalize(blended))
