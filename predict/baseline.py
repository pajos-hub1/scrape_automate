"""Placeholder predictor -- NOT the final model. The actual algorithm is a
deliberately deferred decision (per the project brief); this exists so the
prediction pipeline is testable end-to-end, and it doubles as one of the
"dumb baselines to beat" Stage 4 needs anyway.

1X2 / HT_1X2:  bookmaker odds-implied probabilities when available
               (de-margined), else the historical base rates (42/34/24
               home/draw/away for FT; HT falls back to the same shape
               scaled through the HT Poisson grid) nudged by season-to-date
               points-rate difference between the two sides.
BTTS:          bookmaker odds-implied probabilities when available, else a
               rough per-side scoring likelihood from attack/defense season
               averages.
OU2.5 / OU1.5 /
HT_OU1.5 / HT_OU0.5:
               bookmaker odds-implied probabilities when available, else an
               independent-Poisson model over each side's expected goals
               (HT lambdas are the FT lambdas scaled by HT_FT_GOAL_RATIO).
               All four of these are real bet9ja markets -- HT_OU used to
               be assumed Poisson-only until the "1st Half Markets" tab
               (a separate page load, see scraper/fixtures_scraper.py) was
               found to price it directly, same as HT_1X2.
CorrectScore:  independent-Poisson model, reporting the top scorelines --
               not odds-informed yet, even though CorrectScore odds are now
               scraped (see track/reconcile.py's odds_implied_label for
               where those are actually used: the dashboard's market-implied
               comparison column, not this model).
"""
from predict.common import market_result as _market
from predict.common import normalize as _normalize
from predict.interface import Predictor
from predict.poisson import poisson_over_under, result_probs_from_grid, score_grid, top_scorelines

HOME_BASE, DRAW_BASE, AWAY_BASE = 0.42, 0.34, 0.24  # historical base rates, from the project brief
DEFAULT_GOALS_AVG = 1.3  # fallback when a side has no played matches yet this season
HT_FT_GOAL_RATIO = 0.5  # measured from this project's scraped data, see module docstring


class BaselinePredictor(Predictor):
    model_version = "baseline_v0"

    def predict_markets(self, row):
        lambda_home, lambda_away = self._expected_goals(row)
        grid = score_grid(lambda_home, lambda_away)
        ht_grid = score_grid(lambda_home * HT_FT_GOAL_RATIO, lambda_away * HT_FT_GOAL_RATIO)

        return {
            "1X2": self._predict_1x2(row),
            "BTTS": self._predict_btts(row),
            "OU2.5": self._predict_ou(row, lambda_home, lambda_away, 2.5, "odds_over25_prob", "odds_under25_prob"),
            "OU1.5": self._predict_ou(row, lambda_home, lambda_away, 1.5, "odds_over15_prob", "odds_under15_prob"),
            "CorrectScore": _market(top_scorelines(grid)),
            "HT_1X2": self._predict_ht_1x2(row, ht_grid),
            "HT_OU1.5": self._predict_ht_ou(row, lambda_home, lambda_away, 1.5,
                                             "odds_ht_over15_prob", "odds_ht_under15_prob"),
            "HT_OU0.5": self._predict_ht_ou(row, lambda_home, lambda_away, 0.5,
                                             "odds_ht_over05_prob", "odds_ht_under05_prob"),
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

    def _predict_ht_1x2(self, row, ht_grid):
        oh, od, oa = row.get("odds_ht_home_prob"), row.get("odds_ht_draw_prob"), row.get("odds_ht_away_prob")
        if oh is not None and od is not None and oa is not None:
            probs = _normalize({"Home": oh, "Draw": od, "Away": oa})
        else:
            probs = _normalize(result_probs_from_grid(ht_grid))
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

    def _predict_ou(self, row, lambda_home, lambda_away, line, over_key, under_key):
        oo, ou = row.get(over_key), row.get(under_key)
        if oo is not None and ou is not None:
            probs = _normalize({"Over": oo, "Under": ou})
        else:
            p_over = poisson_over_under(lambda_home + lambda_away, line)
            probs = _normalize({"Over": p_over, "Under": 1 - p_over})
        return _market(probs)

    def _predict_ht_ou(self, row, lambda_home, lambda_away, line, over_key, under_key):
        oo, ou = row.get(over_key), row.get(under_key)
        if oo is not None and ou is not None:
            probs = _normalize({"Over": oo, "Under": ou})
        else:
            lam_ht_total = (lambda_home + lambda_away) * HT_FT_GOAL_RATIO
            p_over = poisson_over_under(lam_ht_total, line)
            probs = _normalize({"Over": p_over, "Under": 1 - p_over})
        return _market(probs)
