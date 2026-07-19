"""Placeholder predictor -- NOT the final model. The actual algorithm is a
deliberately deferred decision (per the project brief); this exists so the
prediction pipeline is testable end-to-end, and it doubles as one of the
"dumb baselines to beat" Stage 4 needs anyway.

1X2:          bookmaker odds-implied probabilities when available
              (de-margined), else the historical base rates (42/34/24
              home/draw/away) nudged by the season-to-date points-rate
              difference between the two sides.
BTTS:         bookmaker odds-implied probabilities when available, else a
              rough per-side scoring likelihood from attack/defense season
              averages.
OU2.5:        no O/U odds scraped yet (Stage 1 gap, still open) -- always
              uses an independent-Poisson model over each side's expected
              goals.
CorrectScore: same Poisson model, reporting the top scorelines.
HT_1X2 /
HT_OU1.5:     half-time goals aren't separately feature-engineered (Stage 2
              only computed FT-based features), so HT expected goals are
              approximated by scaling the FT Poisson lambdas by
              HT_FT_GOAL_RATIO -- measured directly from this project's own
              scraped data (420 matches: avg 1.23 HT goals vs 2.46 FT
              goals, ratio exactly 0.5). 1.5 is used as the HT goals line
              since it's the most balanced split in that same data
              (65% under / 35% over), unlike the lopsided 0.5 line.
"""
from predict.interface import Predictor
from predict.poisson import poisson_over_under, result_probs_from_grid, score_grid, top_scorelines

HOME_BASE, DRAW_BASE, AWAY_BASE = 0.42, 0.34, 0.24  # historical base rates, from the project brief
DEFAULT_GOALS_AVG = 1.3  # fallback when a side has no played matches yet this season
HT_FT_GOAL_RATIO = 0.5  # measured from this project's scraped data, see module docstring


def _normalize(probs: dict) -> dict:
    total = sum(probs.values())
    if total <= 0:
        n = len(probs)
        return {k: 1 / n for k in probs}
    return {k: v / total for k, v in probs.items()}


def _label_and_confidence(probs: dict):
    label = max(probs, key=probs.get)
    return label, probs[label]


def _market(probs: dict) -> dict:
    label, confidence = _label_and_confidence(probs)
    return {"label": label, "probabilities": probs, "confidence": confidence}


class BaselinePredictor(Predictor):
    model_version = "baseline_v0"

    def predict_markets(self, row):
        lambda_home, lambda_away = self._expected_goals(row)
        grid = score_grid(lambda_home, lambda_away)
        ht_grid = score_grid(lambda_home * HT_FT_GOAL_RATIO, lambda_away * HT_FT_GOAL_RATIO)

        return {
            "1X2": self._predict_1x2(row),
            "BTTS": self._predict_btts(row),
            "OU2.5": self._predict_ou25(row, lambda_home, lambda_away),
            "CorrectScore": _market(top_scorelines(grid)),
            "HT_1X2": _market(_normalize(result_probs_from_grid(ht_grid))),
            "HT_OU1.5": self._predict_ht_ou15(lambda_home, lambda_away),
        }

    def _expected_goals(self, row):
        """(lambda_home, lambda_away): attack-strength-vs-opponent-defense
        blend from season-to-date average goals for/against, matching what
        OU2.5/BTTS already use."""
        a_gf = row.get("a_season_gf_avg") or DEFAULT_GOALS_AVG
        a_ga = row.get("a_season_ga_avg") or DEFAULT_GOALS_AVG
        b_gf = row.get("b_season_gf_avg") or DEFAULT_GOALS_AVG
        b_ga = row.get("b_season_ga_avg") or DEFAULT_GOALS_AVG
        lambda_home = (a_gf + b_ga) / 2
        lambda_away = (b_gf + a_ga) / 2
        return lambda_home, lambda_away

    def _predict_1x2(self, row):
        oh, od, oa = row.get("odds_home_prob"), row.get("odds_draw_prob"), row.get("odds_away_prob")
        if oh is not None and od is not None and oa is not None:
            probs = _normalize({"Home": oh, "Draw": od, "Away": oa})
        else:
            a_rate = row.get("a_season_pts_rate") or 0
            b_rate = row.get("b_season_pts_rate") or 0
            diff = max(-0.15, min(0.15, (a_rate - b_rate) / 10))
            probs = _normalize({"Home": HOME_BASE + diff, "Draw": DRAW_BASE, "Away": AWAY_BASE - diff})
        return _market(probs)

    def _predict_btts(self, row):
        oy, on = row.get("odds_btts_yes_prob"), row.get("odds_btts_no_prob")
        if oy is not None and on is not None:
            probs = _normalize({"Yes": oy, "No": on})
        else:
            a_gf = row.get("a_season_gf_avg") or DEFAULT_GOALS_AVG
            a_ga = row.get("a_season_ga_avg") or DEFAULT_GOALS_AVG
            b_gf = row.get("b_season_gf_avg") or DEFAULT_GOALS_AVG
            b_ga = row.get("b_season_ga_avg") or DEFAULT_GOALS_AVG
            p_a_scores = min(0.95, max(0.05, (a_gf + b_ga) / 2 / 2.5))
            p_b_scores = min(0.95, max(0.05, (b_gf + a_ga) / 2 / 2.5))
            p_yes = p_a_scores * p_b_scores
            probs = _normalize({"Yes": p_yes, "No": 1 - p_yes})
        return _market(probs)

    def _predict_ou25(self, row, lambda_home, lambda_away):
        oo, ou = row.get("odds_over25_prob"), row.get("odds_under25_prob")
        if oo is not None and ou is not None:
            probs = _normalize({"Over": oo, "Under": ou})
        else:
            p_over = poisson_over_under(lambda_home + lambda_away, 2.5)
            probs = _normalize({"Over": p_over, "Under": 1 - p_over})
        return _market(probs)

    def _predict_ht_ou15(self, lambda_home, lambda_away):
        lam_ht_total = (lambda_home + lambda_away) * HT_FT_GOAL_RATIO
        p_over = poisson_over_under(lam_ht_total, 1.5)
        probs = _normalize({"Over": p_over, "Under": 1 - p_over})
        return _market(probs)
