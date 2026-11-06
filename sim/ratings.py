"""Zoom's fixed team ratings, measured from its own correct-score odds.

What the data showed (2026-11-06, ~6,250 Premier games over 19 seasons):
  - each game's expected goals follow
        log(home goals) = base + home + attack[home] - defence[away]
        log(away goals) = base        + attack[away] - defence[home]
  - the ratings NEVER change: identical every season and all through a
    season (no form, no table effects); home advantage is x1.20
  - on top, every game gets a small random adjustment (~5% on each side's
    goals) that Zoom's odds already include
  - goals are Poisson; half-time gets exactly half the expected goals

Expected goals per game come from the correct-score odds: P(home scores 0)
= exp(-home goals), so home goals = -ln P(home 0). Fitted per league by
least squares on log goals, with attack and defence each summing to zero.
`python run.py simulate --refit` refreshes sim/ratings.json from the DB.
"""
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

RATINGS_PATH = Path(__file__).with_name("ratings.json")
LEAGUE_NAMES = {1: "Premier", 2: "Liga", 3: "Bundesliga", 4: "Serie A", 5: "Ligue 1", 6: "Primeira", 7: "Eredivisie"}


def _fair(book):
    t = sum(1 / p for p in book.values())
    return {s: (1 / p) / t for s, p in book.items()}


def expected_goals(cs_book):
    """(home, away) expected goals from one game's correct-score prices, or None."""
    p = _fair(cs_book)
    grid = {tuple(map(int, s.split("-"))): v for s, v in p.items() if s != "Other"}
    h0 = sum(v for (h, a), v in grid.items() if h == 0)
    a0 = sum(v for (h, a), v in grid.items() if a == 0)
    if not (0 < h0 < 1 and 0 < a0 < 1):
        return None
    return -math.log(h0), -math.log(a0)


def fit_league(games):
    """games: [(home team, away team, home goals, away goals)] -> ratings dict."""
    teams = sorted({g[0] for g in games} | {g[1] for g in games})
    T = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    rows, y = [], []
    for home, away, lh, la in games:
        h, a = T[home], T[away]
        r = np.zeros(2 + 2 * n); r[0] = 1; r[1] = 1; r[2 + h] = 1; r[2 + n + a] = -1
        rows.append(r); y.append(math.log(lh))
        r = np.zeros(2 + 2 * n); r[0] = 1; r[2 + a] = 1; r[2 + n + h] = -1
        rows.append(r); y.append(math.log(la))
    for block in (range(2, 2 + n), range(2 + n, 2 + 2 * n)):     # sum-to-zero (weighted)
        r = np.zeros(2 + 2 * n); r[list(block)] = 100.0
        rows.append(r); y.append(0.0)
    X, Y = np.array(rows), np.array(y)
    beta, *_ = np.linalg.lstsq(X, Y, rcond=None)
    leftover = Y[:-2] - X[:-2] @ beta
    return {"teams": teams, "base": float(beta[0]), "home": float(beta[1]),
            "attack": [float(x) for x in beta[2:2 + n]], "defence": [float(x) for x in beta[2 + n:]],
            "game_noise": float(leftover.std()), "games": len(games)}


def fit_all(conn):
    """Ratings for every league from the database (live + archives)."""
    books = defaultdict(dict)
    for r in conn.execute("SELECT ext_match_id, selection, price FROM all_lg_odds WHERE market = 'CorrectScore'"):
        books[r[0]][r[1]] = r[2]
    by_league = defaultdict(list)
    for r in conn.execute("SELECT ext_match_id, competition_id, team_a, team_b FROM all_lg_events "
                          "WHERE competition_id BETWEEN 1 AND 7"):
        if r[0] in books:
            eg = expected_goals(books[r[0]])
            if eg:
                by_league[r[1]].append((r[2], r[3], *eg))
    # Premier also has months of correct-score odds from the old scraper.
    seen = set()
    pbooks = defaultdict(dict)
    for r in conn.execute("SELECT fixture_id, selection, price FROM all_odds WHERE market = 'CorrectScore'"):
        pbooks[r[0]][r[1]] = r[2]
    for r in conn.execute("SELECT fixture_id, season_id, round_number, match_number, team_a, team_b FROM fixtures"):
        key = (r[1], r[2], r[3])
        if r[0] in pbooks and r[1] is not None and key not in seen:
            seen.add(key)
            eg = expected_goals(pbooks[r[0]])
            if eg:
                by_league[1].append((r[4], r[5], *eg))
    return {str(cid): dict(fit_league(gs), name=LEAGUE_NAMES[cid]) for cid, gs in sorted(by_league.items()) if len(gs) >= 200}


def load():
    return {int(k): v for k, v in json.loads(RATINGS_PATH.read_text()).items()}


def save(ratings):
    RATINGS_PATH.write_text(json.dumps(ratings, indent=1) + "\n")
