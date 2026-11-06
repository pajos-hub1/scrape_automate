"""Strategy tester: play a betting plan over thousands of simulated months.

Each simulated game works like Zoom's (sim/ratings.py): expected goals from
the teams' fixed ratings and home advantage, times a small random per-game
adjustment; goals are Poisson; each half gets half of each team's goals.
Prices: Bet9ja takes ~5% of every selection, measured 2026-11-06 against
each game's true chance -- price = (1 - cut) / chance, the same from long
shots to favourites (1X2 5.5%, HT O/U 0.5 5.0%, BTTS 5.1%). Those chances
matched real results within ~1 point in every band over 7,500 games, except
that the strongest favourites win a little more often (see BIAS).

Tickets are built like the live pipeline (track/betslips.py and
track/league_slips.py), ranking picks by chance:
  premier      3 tickets a Premier round: 1X2 / HT O/U 0.5 / BTTS, top 5 each
  merged       one set per Premier round: every league's next round, each
               league's top 1 / top 2 per market that clear MERGED_BARS
  singles      single bets on every Premier pick at or above a chance
All arrays are (runs, ...): every run is an independent simulated history.
"""
import math
from dataclasses import dataclass

import numpy as np

from sim import ratings as R
from track.league_slips import MERGED_BARS, MERGED_MIN_LEAGUES

from pathlib import Path

RESULTS_PATH = Path(__file__).with_name("results.json")    # `run.py simulate --save`; read by the dashboard
MARKETS = ["1X2", "HT_OU0.5", "BTTS"]
CUT = {"1X2": 0.055, "HT_OU0.5": 0.050, "BTTS": 0.051}
# Zoom slightly underrates its strongest favourites (measured 2026-11-06):
# each Premier game's strongest pick won ~1 point more often than its true
# chance (7,545 games; 1X2 +0.9, HT O/U 0.5 +1.2, BTTS +0.6), and the real
# merged picks ~2 points more (2,490 picks, z 2.3). Zoom prices from its own
# chance, so favourites cost ~3% a pick instead of ~5%. Checked against our
# real tracked tickets (706 Premier, 939 merged): at 2 points they land at
# the 87th / 95th percentile of the simulation (ordinary luck); at 1 point
# 97th / 99.5th; at 0, 99.4th / 100th. So BIAS = 2 points; 1 is the cautious end.
BIAS = 0.02
BIAS_CAUTIOUS = 0.01
ROUNDS_PER_DAY = 15            # every league plays a round about every 96 minutes
MERGED_SETS_PER_DAY = 13.3     # measured: 40 sets per 3-day merged season (rounds already used are skipped)
MERGED_OTHER_LEAGUE_SHARE = 0.90   # measured: a set has 6.4 of 7 leagues -- Premier always, each other ~90%
MAX_GOALS = 12


def schedule(n):
    """Double round robin (circle method): 2(n-1) rounds of n/2 games, as (home, away) index arrays."""
    idx = list(range(n))
    first = []
    for _ in range(n - 1):
        first.append([(idx[i], idx[n - 1 - i]) for i in range(n // 2)])
        idx = [idx[0]] + [idx[-1]] + idx[1:-1]
    second = [[(a, h) for h, a in rnd] for rnd in first]
    return [(np.array([g[0] for g in rnd]), np.array([g[1] for g in rnd])) for rnd in first + second]


@dataclass
class League:
    cid: int
    name: str
    base: float
    home: float
    attack: np.ndarray
    defence: np.ndarray
    noise: float
    rounds: list

    @classmethod
    def from_ratings(cls, cid, r):
        return cls(cid, r["name"], r["base"], r["home"], np.array(r["attack"]), np.array(r["defence"]),
                   r["game_noise"], schedule(len(r["teams"])))


def _poisson_pmf(lam):
    """(..., MAX_GOALS) Poisson probabilities."""
    k = np.arange(MAX_GOALS)
    return np.exp(-lam[..., None] + k * np.log(lam[..., None]) - np.array([math.lgamma(x + 1) for x in k]))


def play_round(league, rnd_index, runs, rng, bias=BIAS):
    """One round of `league` for every run. Returns per-market arrays (runs, games):
    chance of the round's best pick per game, its price, and whether it won."""
    h, a = league.rounds[rnd_index % len(league.rounds)]
    g = len(h)
    noise = rng.normal(0.0, league.noise, size=(2, runs, g))
    lh = np.exp(league.base + league.home + league.attack[h] - league.defence[a] + noise[0])
    la = np.exp(league.base + league.attack[a] - league.defence[h] + noise[1])
    ph, pa = _poisson_pmf(lh), _poisson_pmf(la)
    cdf_h = np.cumsum(ph, axis=-1)
    p_home = (pa * (1 - cdf_h)).sum(-1)                       # P(home > away)
    p_draw = (ph * pa).sum(-1)
    p_away = 1 - p_home - p_draw
    p_ht_over = 1 - np.exp(-(lh + la) / 2)
    p_btts = (1 - np.exp(-lh)) * (1 - np.exp(-la))

    gh, ga = rng.poisson(lh), rng.poisson(la)
    hh, ha = rng.binomial(gh, 0.5), rng.binomial(ga, 0.5)
    out = {}
    one = np.stack([p_home, p_draw, p_away])                   # (3, runs, games)
    best = one.argmax(0)
    res = np.where(gh > ga, 0, np.where(gh == ga, 1, 2))
    out["1X2"] = (one.max(0), best == res)
    over = p_ht_over >= 0.5
    out["HT_OU0.5"] = (np.where(over, p_ht_over, 1 - p_ht_over), np.where(over, hh + ha > 0, hh + ha == 0))
    yes = p_btts >= 0.5
    out["BTTS"] = (np.where(yes, p_btts, 1 - p_btts), np.where(yes, (gh > 0) & (ga > 0), (gh == 0) | (ga == 0)))
    # p = the true chance (outcomes are drawn from it); Zoom's price comes
    # from its own slightly lower chance for the favourite (FAVOURITE_BIAS).
    # p = the true chance; outcomes follow it. The pick's real chance is
    # `bias` higher (Zoom underrates favourites): draw the bonus wins on top.
    extra = rng.random(size=(3,) + p_home.shape)
    res = {}
    for j, (m, (pm, won)) in enumerate(out.items()):
        bonus = (~won) & (extra[j] < bias / np.maximum(1 - pm, 1e-9))
        res[m] = (pm, np.round((1 - CUT[m]) / pm, 2), won | bonus)
    return res


def _top(p, price, won, k):
    """The k highest-chance picks per run: (runs, k) arrays."""
    order = np.argsort(-p, axis=1)[:, :k]
    take = lambda x: np.take_along_axis(x, order, axis=1)
    return take(p), take(price), take(won)


class Book:
    """Running results of one strategy across runs."""

    def __init__(self, runs, budget):
        self.profit = np.zeros(runs)
        self.low = np.zeros(runs)               # lowest running balance
        self.staked = np.zeros(runs)
        self.tickets = np.zeros(runs)
        self.wins = np.zeros(runs)
        self.streak = np.zeros(runs)
        self.worst_streak = np.zeros(runs)
        self.budget = budget

    def add(self, stake, odds, won, active=None):
        """One ticket per run (active: which runs actually place it)."""
        active = np.ones_like(won, dtype=bool) if active is None else active
        st = np.where(active, stake, 0.0)
        self.staked += st
        self.tickets += active
        self.wins += active & won
        self.profit += np.where(active, np.where(won, stake * odds - stake, -stake), 0.0)
        self.low = np.minimum(self.low, self.profit)
        self.streak = np.where(active, np.where(won, 0, self.streak + 1), self.streak)
        self.worst_streak = np.maximum(self.worst_streak, self.streak)


def run(strategy, days=30, runs=2000, stake=500, budget=20000, seed=1, threshold=0.75, legs=5, bias=BIAS):
    rng = np.random.default_rng(seed)
    leagues = {cid: League.from_ratings(cid, r) for cid, r in R.load().items()}
    start = {cid: rng.integers(len(lg.rounds)) for cid, lg in leagues.items()}   # same for all runs: fine, ratings are fixed
    book = Book(runs, budget)
    steps = round(days * (MERGED_SETS_PER_DAY if strategy == "merged" else ROUNDS_PER_DAY))
    for i in range(steps):
        if strategy == "premier":
            rnd = play_round(leagues[1], start[1] + i, runs, rng, bias)
            for m in MARKETS:
                p, price, won = _top(*rnd[m], legs)
                book.add(stake, price.prod(1), won.all(1))
        elif strategy == "singles":
            rnd = play_round(leagues[1], start[1] + i, runs, rng, bias)
            for m in MARKETS:
                p, price, won = rnd[m]
                for j in range(p.shape[1]):
                    book.add(stake, price[:, j], won[:, j], active=p[:, j] >= threshold)
        elif strategy == "merged":
            top5, present = {}, {}
            for cid, lg in leagues.items():
                rnd = play_round(lg, start[cid] + i, runs, rng, bias)
                top5[cid] = {m: _top(*rnd[m], 5) for m in MARKETS}
                # a league whose next round was already used in the last set sits this one out
                present[cid] = np.ones(runs, bool) if cid == 1 else rng.random(runs) < MERGED_OTHER_LEAGUE_SHARE
            if len(top5) < MERGED_MIN_LEAGUES:
                continue
            for m in MARKETS:
                for k in (1, 2):
                    bar = MERGED_BARS[k][m]
                    p = np.concatenate([top5[c][m][0][:, :k] for c in top5], axis=1)
                    price = np.concatenate([top5[c][m][1][:, :k] for c in top5], axis=1)
                    won = np.concatenate([top5[c][m][2][:, :k] for c in top5], axis=1)
                    keep = (p >= bar) & np.concatenate([np.repeat(present[c][:, None], k, 1) for c in top5], axis=1)
                    any_leg = keep.any(1)
                    odds = np.where(keep, price, 1.0).prod(1)
                    all_won = np.where(keep, won, True).all(1)
                    book.add(stake, odds, all_won, active=any_leg)
        else:
            raise ValueError(f"unknown strategy {strategy!r}")
    return book


def summary(book, days):
    p = book.profit
    staked = book.staked.mean()
    return {
        "days": days, "runs": len(p), "staked": staked, "tickets": book.tickets.mean(),
        "win_rate": float((book.wins.sum() / book.tickets.sum()) if book.tickets.sum() else 0.0),
        "mean": float(p.mean()), "median": float(np.median(p)), "return": float(p.mean() / staked) if staked else 0.0,
        "p_profit": float((p > 0).mean()), "p05": float(np.percentile(p, 5)), "p95": float(np.percentile(p, 95)),
        "worst_streak": float(np.median(book.worst_streak)),
        "p_ruin": float((book.low <= -book.budget).mean()),
    }


def format_summary(name, s, budget):
    naira = lambda v: ("−" if v < 0 else "+") + f"₦{abs(v):,.0f}"
    return "\n".join([
        f"{name}: {s['days']} days, {s['runs']:,} simulated histories",
        f"  tickets {s['tickets']:,.0f} (win {s['win_rate'] * 100:.1f}%), staked ₦{s['staked']:,.0f}",
        f"  average result {naira(s['mean'])} ({s['return'] * 100:+.1f}%), typical (median) {naira(s['median'])}",
        f"  ended in profit: {s['p_profit'] * 100:.0f}% of histories; 1 in 20 worse than {naira(s['p05'])}, "
        f"1 in 20 better than {naira(s['p95'])}",
        f"  longest losing run (typical): {s['worst_streak']:.0f} tickets in a row",
        f"  a ₦{budget:,} budget ran out in {s['p_ruin'] * 100:.0f}% of histories",
    ])
