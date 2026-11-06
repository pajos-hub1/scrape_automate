"""Strategy tester (sim/): the simulated league behaves like Zoom's."""
import numpy as np

from sim import engine as E
from sim import ratings as R


def test_schedule_is_a_double_round_robin():
    for n in (18, 20):
        rounds = E.schedule(n)
        assert len(rounds) == 2 * (n - 1)
        meetings = [(int(h), int(a)) for hs, as_ in rounds for h, a in zip(hs, as_)]
        assert len(meetings) == len(set(meetings)) == n * (n - 1)          # every pairing, home and away, once
        for hs, as_ in rounds:
            assert len(set(hs) | set(as_)) == n                              # everyone plays every round


def test_ratings_cover_all_seven_leagues():
    r = R.load()
    assert sorted(r) == [1, 2, 3, 4, 5, 6, 7]
    assert 1.15 < np.exp(r[1]["home"]) < 1.25                               # Premier home advantage ~x1.20
    best = max(range(len(r[1]["teams"])), key=lambda i: r[1]["attack"][i] + r[1]["defence"][i])
    assert r[1]["teams"][best] == "Z.Arsenal"


def test_prices_and_outcomes_follow_the_true_chance():
    rng = np.random.default_rng(0)
    lg = E.League.from_ratings(1, R.load()[1])
    ps, wins = [], []
    for i in range(40):
        rnd = E.play_round(lg, i, 500, rng, bias=0.0)
        for m in E.MARKETS:
            p, price, won = rnd[m]
            assert np.allclose(price, (1 - E.CUT[m]) / p, atol=0.006)       # ~5% cut on every price
            assert (p >= 0.5).all() or m == "1X2"                           # the strongest pick per game
            ps.append(p.ravel()); wins.append(won.ravel())
    p, w = np.concatenate(ps), np.concatenate(wins)
    assert abs(w.mean() - p.mean()) < 0.005                                  # calibrated with no bias


def test_strategies_place_the_expected_tickets():
    book = E.run("premier", days=1, runs=50)
    assert np.all(book.tickets == 3 * E.ROUNDS_PER_DAY)                     # 3 tickets every round
    book = E.run("merged", days=1, runs=50)
    assert (book.tickets <= 6 * round(E.MERGED_SETS_PER_DAY)).all() and book.tickets.mean() > 3 * E.MERGED_SETS_PER_DAY
    s = E.summary(E.run("singles", days=1, runs=50, threshold=0.75), 1)
    assert s["win_rate"] > 0.75
