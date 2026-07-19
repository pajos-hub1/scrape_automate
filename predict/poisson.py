"""Shared independent-Poisson scoring model. Both the correct-score and
half-time markets are just different views of the same "expected goals for
each side" model that OU2.5/BTTS already use in baseline.py -- this module
is the one place that turns (lambda_home, lambda_away) into market
probabilities, so all four goal-based markets stay consistent with each
other.
"""
import math

MAX_GOALS = 6  # grid cap per side; tail beyond this is negligible at these lambdas


def poisson_pmf(k, lam):
    lam = max(lam, 1e-6)
    return math.exp(-lam) * lam ** k / math.factorial(k)


def poisson_over_under(lam_total, line):
    """P(total > line) for a half-integer line (0.5, 1.5, 2.5, ...)."""
    threshold = math.floor(line)
    p_under_or_eq = sum(poisson_pmf(k, lam_total) for k in range(threshold + 1))
    return max(0.0, min(1.0, 1 - p_under_or_eq))


def score_grid(lambda_home, lambda_away, max_goals=MAX_GOALS):
    """Returns {(home_goals, away_goals): prob}, normalized to sum to 1
    (absorbs the negligible tail beyond max_goals)."""
    grid = {
        (i, j): poisson_pmf(i, lambda_home) * poisson_pmf(j, lambda_away)
        for i in range(max_goals + 1)
        for j in range(max_goals + 1)
    }
    total = sum(grid.values())
    return {k: v / total for k, v in grid.items()}


def result_probs_from_grid(grid):
    """Collapses a score grid into Home/Draw/Away win probabilities."""
    home = sum(p for (i, j), p in grid.items() if i > j)
    draw = sum(p for (i, j), p in grid.items() if i == j)
    away = sum(p for (i, j), p in grid.items() if i < j)
    return {"Home": home, "Draw": draw, "Away": away}


def top_scorelines(grid, top_n=8):
    """Top N scorelines by probability, plus an 'Other' bucket for the
    remainder -- reporting the full grid (49 cells) in a predictions row
    would be noise, not signal."""
    ranked = sorted(grid.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    probs = {f"{i}-{j}": p for (i, j), p in ranked}
    remainder = 1 - sum(probs.values())
    if remainder > 1e-9:
        probs["Other"] = remainder
    return probs
