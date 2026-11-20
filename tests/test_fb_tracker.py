"""fb_tracker: parsing, matching across bookmakers, arb maths, episode tracking. No network."""
import json

import pytest

from fb_tracker import collect
from fb_tracker.arbs import closest, find_arbs
from fb_tracker.matching import match_books, normalise, similarity
from fb_tracker.sources import Event, parse_bet9ja, parse_sportybet


def ev(book, home, away, odds, ko="2026-11-21T12:30:00Z"):
    return Event(book, "England", "Championship", home, away, ko, odds)


def test_parse_bet9ja_and_sportybet_to_the_same_shape():
    b9 = {"D": {"E": [{"DS": "Millwall - QPR", "STARTDATEUTC": "2026-11-21T12:30:00Z",
                       "O": {"S_1X2_1": "2.54", "S_1X2_X": "3.4", "S_1X2_2": "2.7", "S_OU@2.5_O": "1.72", "S_OU@2.5_U": "2.06"}}]}}
    e = parse_bet9ja(b9, "England", "Championship")[0]
    assert (e.home, e.away, e.kickoff) == ("Millwall", "QPR", "2026-11-21T12:30:00Z")
    assert e.odds[("1X2", "1")] == 2.54 and e.odds[("OU2.5", "U")] == 2.06
    sp = {"data": {"tournaments": [{"name": "Championship", "events": [{
        "homeTeamName": "Millwall", "awayTeamName": "Queens Park Rangers", "estimateStartTime": 1795264200000,
        "sport": {"category": {"name": "England", "tournament": {"name": "Championship"}}},
        "markets": [{"id": "1", "outcomes": [{"desc": "Home", "odds": "2.60"}, {"desc": "Draw", "odds": "3.30"},
                                            {"desc": "Away", "odds": "2.80"}]},
                    {"id": "18", "specifier": "total=2.5", "outcomes": [{"desc": "Over 2.5", "odds": "1.70"},
                                                                        {"desc": "Under 2.5", "odds": "2.10"}]},
                    {"id": "18", "specifier": "total=3", "outcomes": [{"desc": "Over 3", "odds": "2.0"}]},
                    {"id": "10", "outcomes": [{"desc": "Draw or Away", "odds": "1.50"}]}]}]}]}}
    s = parse_sportybet(sp)[0]
    assert s.odds == {("1X2", "1"): 2.6, ("1X2", "X"): 3.3, ("1X2", "2"): 2.8, ("OU2.5", "O"): 1.7,
                      ("OU2.5", "U"): 2.1, ("DC", "X2"): 1.5}                 # whole-number lines ignored


def test_names_match_across_bookmakers_but_not_youth_sides():
    assert normalise("Viborg FF") == normalise("Viborg")
    assert similarity("QPR", "Queens Park Rangers") == 1.0
    assert similarity("Man City", "Manchester City") == 1.0
    assert similarity("Brighton", "Brighton & Hove Albion") > 0.9
    assert similarity("Arsenal U21", "Arsenal") < 0.5
    assert similarity("Chelsea Women", "Chelsea") < 0.5


def test_matching_pairs_games_by_kickoff_and_names():
    b9 = [ev("bet9ja", "Millwall", "QPR", {}), ev("bet9ja", "Leeds", "Hull", {}, ko="2026-11-21T15:00:00Z")]
    sp = [ev("sportybet", "Hull City", "Leeds", {}, ko="2026-11-21T15:00:00Z"),        # reversed: not the same game
          ev("sportybet", "Millwall FC", "Queens Park Rangers", {}, ko="2026-11-21T12:35:00Z"),
          ev("sportybet", "Leeds United", "Hull City", {}, ko="2026-11-21T18:00:00Z")]  # 3 hours later: not it
    m = match_books({"bet9ja": b9, "sportybet": sp})
    assert len(m) == 1 and m[0]["sportybet"].home == "Millwall FC"


def test_arb_maths_and_stakes():
    match = {"bet9ja": ev("bet9ja", "A", "B", {("1X2", "1"): 1.98, ("1X2", "X"): 3.4, ("1X2", "2"): 4.0}),
             "sportybet": ev("sportybet", "A", "B", {("1X2", "1"): 1.90, ("1X2", "X"): 3.8, ("1X2", "2"): 4.5})}
    arbs = find_arbs(match)
    assert [a["cover"] for a in arbs] == ["1X2"]
    a = arbs[0]
    assert abs(a["margin"] - (1 / (1 / 1.98 + 1 / 3.8 + 1 / 4.5) - 1)) < 1e-9
    returns = [stake * price for _, _, _, price, stake in a["legs"]]
    assert max(returns) - min(returns) < 10 and min(returns) > 10_000            # same payout whatever happens
    assert {book for _, _, book, _, _ in a["legs"]} == {"bet9ja", "sportybet"}
    assert closest(match) < 1
    same = {"bet9ja": ev("bet9ja", "A", "B", {("1X2", "1"): 2.0, ("1X2", "X"): 3.3, ("1X2", "2"): 3.6}),
            "sportybet": ev("sportybet", "A", "B", {("1X2", "1"): 1.95, ("1X2", "X"): 3.2, ("1X2", "2"): 3.5})}
    assert find_arbs(same) == [] and closest(same) is None     # one book best on every leg: no cross-book cover


def test_an_arb_seen_in_consecutive_snapshots_is_one_episode(tmp_path):
    conn = collect.connect(tmp_path / "fb.db")
    arb = {"bet9ja": [ev("bet9ja", "A", "B", {("OU2.5", "O"): 2.2, ("OU2.5", "U"): 1.8})],
           "sportybet": [ev("sportybet", "A", "B", {("OU2.5", "O"): 1.8, ("OU2.5", "U"): 2.1})]}
    gone = {"bet9ja": [ev("bet9ja", "A", "B", {("OU2.5", "O"): 1.9, ("OU2.5", "U"): 1.8})],
            "sportybet": [ev("sportybet", "A", "B", {("OU2.5", "O"): 1.8, ("OU2.5", "U"): 1.9})]}
    src = lambda data: {b: (lambda b=b: data[b]) for b in data}
    collect.snapshot(conn, src(arb), ts="2026-11-20T10:00:00+00:00")
    collect.snapshot(conn, src(arb), ts="2026-11-20T10:10:00+00:00")
    collect.snapshot(conn, src(gone), ts="2026-11-20T10:20:00+00:00")
    collect.snapshot(conn, src(arb), ts="2026-11-20T10:30:00+00:00")             # back again: a new episode
    rows = conn.execute("SELECT first_seen, last_seen, snapshots FROM arbs ORDER BY first_seen").fetchall()
    assert rows == [("2026-11-20T10:00:00+00:00", "2026-11-20T10:10:00+00:00", 2),
                    ("2026-11-20T10:30:00+00:00", "2026-11-20T10:30:00+00:00", 1)]


def test_one_bookmaker_failing_does_not_stop_the_snapshot(tmp_path):
    conn = collect.connect(tmp_path / "fb.db")
    def broken():
        raise TimeoutError("down")
    out = collect.snapshot(conn, {"bet9ja": broken, "sportybet": lambda: [ev("sportybet", "A", "B", {})]},
                           ts="2026-11-20T10:00:00+00:00")
    assert "bet9ja" in out["errors"] and out["counts"]["sportybet"] == 1
    assert json.loads(conn.execute("SELECT errors FROM runs").fetchone()[0])["bet9ja"].startswith("TimeoutError")
