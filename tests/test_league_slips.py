"""Other-league tickets, merged tickets and their combined Telegram messages."""
from datetime import datetime, timezone

import pytest

import track.league_notify as ln
import track.league_slips as ls
from db.connection import get_connection

NOW = datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc)
ODDS = {"1X2": {"Home": 1.5, "Draw": 4.0, "Away": 6.0}, "HT_OU0.5": {"Over": 1.25, "Under": 3.8},
        "BTTS": {"Yes": 1.9, "No": 1.8}}


def _add_season(conn, cid, first_id, rounds, gpr=10, with_odds=True, played=True):
    for r in range(1, rounds + 1):
        for k in range(gpr):
            ext = first_id + (r - 1) * gpr + k
            a, b = f"L{cid}A{k}", f"L{cid}B{k}"
            if played:
                conn.execute("""INSERT INTO lg_matches VALUES (?, ?, 'x', ?, ?, ?, ?, ?, 2, 1, 1, 0, 't', 's')""",
                             (ext, cid, first_id, r, k + 1, a, b))
            conn.execute("INSERT OR IGNORE INTO lg_events VALUES (?, ?, 'x', ?, ?, ?, ?, 'f')",
                         (ext, cid, ext, a, b, "2026-10-23T21:00:00Z"))
            if with_odds:
                for m, book in ODDS.items():
                    for sel, p in book.items():
                        conn.execute("INSERT OR IGNORE INTO lg_odds VALUES (?, ?, ?, ?, 'c')", (ext, m, sel, p))


def _upcoming(conn, cid, first_id, round_no, gpr=10):
    for k in range(gpr):
        ext = first_id + (round_no - 1) * gpr + k
        conn.execute("INSERT INTO lg_events VALUES (?, ?, 'x', ?, ?, ?, ?, 'f')",
                     (ext, cid, ext, f"L{cid}A{k}", f"L{cid}B{k}", "2026-10-23T21:00:00Z"))
        for m, book in ODDS.items():
            for sel, p in book.items():
                conn.execute("INSERT INTO lg_odds VALUES (?, ?, ?, ?, 'c')", (ext, m, sel, p))


def test_money_starts_at_the_round_upcoming_at_switch_on(env):
    with get_connection(env[0]) as conn:
        _add_season(conn, 2, 1000, rounds=3)
        _upcoming(conn, 2, 1000, 4)
        built = ls.build_league(conn, 2, now=NOW)                # records start = season 1000, round 4
        settled = [e for e in built if e["round"]["status"] == "settled"]
        assert settled and not any(s["played"] for e in settled for s in e["slips"])   # history: tracking only
        up = [e for e in built if e["round"]["status"] == "upcoming"][-1]
        assert sorted(s["name"] for s in ls.played(up["slips"])) == ["1X2 top 5", "BTTS top 5", "HT_OU0.5 top 5"]
        for k in range(10):                                      # round 4 finishes
            conn.execute("INSERT INTO lg_matches VALUES (?, 2, 'x', 1000, 4, ?, 'a', 'b', 2, 1, 1, 0, 't', 's')",
                         (1030 + k, k + 1))
        totals = ls.league_totals(ls.build_league(conn, 2, now=NOW))
        assert totals[1000]["rounds"] == 1 and totals[1000]["slips"] == 3 and totals[1000]["staked"] == 1500


def test_merged_tickets_once_per_round_and_settled_from_results(env):
    with get_connection(env[0]) as conn:
        for cid in (1, 2, 3, 4):
            _add_season(conn, cid, cid * 10000, rounds=1)
            _upcoming(conn, cid, cid * 10000, 2)
        built = {cid: ls.build_slips(ls.load_league_rounds(conn, cid, now=NOW), plan_fn=lambda s: ls.LEAGUE_PLAN)
                 for cid in (1, 2, 3, 4)}
        keys = ls.create_merged(conn, built, now=NOW)
        assert len(keys) == 6 and ls.create_merged(conn, built, now=NOW) == []      # once per Premier round
        tickets = {ls.merged_name(t): t for t in ls.merged_tickets(conn)}
        assert len(tickets["1X2 merged top 1"]["legs"]) == 4 and len(tickets["1X2 merged top 2"]["legs"]) == 8
        assert {l["league"] for l in tickets["BTTS merged top 1"]["legs"]} == {1, 2, 3, 4}
        assert not tickets["1X2 merged top 1"]["settled"]
        for cid in (1, 2, 3, 4):                                 # round 2 finishes: every home side wins 2-1
            for k in range(10):
                ext = cid * 10000 + 10 + k
                conn.execute("INSERT INTO lg_matches VALUES (?, ?, 'x', ?, 2, ?, 'a', 'b', 2, 1, 1, 0, 't', 's')",
                             (ext, cid, cid * 10000, k + 1))
        t = {ls.merged_name(t): t for t in ls.merged_tickets(conn)}["1X2 merged top 1"]
        assert t["settled"] and t["won"] and t["returned"] == pytest.approx(500 * 1.5 ** 4)


def test_merged_needs_enough_leagues(env):
    with get_connection(env[0]) as conn:
        for cid in (1, 2):
            _add_season(conn, cid, cid * 10000, rounds=1)
            _upcoming(conn, cid, cid * 10000, 2)
        built = {cid: ls.build_slips(ls.load_league_rounds(conn, cid, now=NOW), plan_fn=lambda s: ls.LEAGUE_PLAN)
                 for cid in (1, 2)}
        assert ls.create_merged(conn, built, now=NOW) == []


def test_combined_message_sent_once_and_marks_every_item(env, monkeypatch):
    with get_connection(env[0]) as conn:
        for cid in (1, 2, 3, 4):
            _add_season(conn, cid, cid * 10000, rounds=1)
            _upcoming(conn, cid, cid * 10000, 2)
        built = {cid: ls.build_slips(ls.load_league_rounds(conn, cid, now=NOW), plan_fn=lambda s: ls.LEAGUE_PLAN)
                 for cid in (1, 2, 3, 4)}
        ls.create_merged(conn, built, now=NOW)
    connect = lambda: get_connection(env[0])
    msgs = ln.league_messages({"hello"}, connect=connect)
    texts = [t for _, t in msgs if t]
    assert len(texts) == 1 and "Merged tickets" in texts[0] and "1X2 · top 1" in texts[0]
    sent = {"hello"} | {k for k, _ in msgs}
    assert [t for _, t in ln.league_messages(sent, connect=connect) if t] == []                 # nothing repeated


def test_merged_sets_follow_premier_rounds_and_never_reuse_a_round(env):
    plan = lambda s: ls.LEAGUE_PLAN
    with get_connection(env[0]) as conn:
        for cid in (1, 2, 3, 4):
            _add_season(conn, cid, cid * 10000, rounds=1)
            _upcoming(conn, cid, cid * 10000, 2)
        build = lambda: {cid: ls.build_slips(ls.load_league_rounds(conn, cid, now=NOW), plan_fn=plan) for cid in (1, 2, 3, 4)}
        assert len(ls.create_merged(conn, build(), now=NOW)) == 6           # set for Premier R2
        assert {t["label"] for t in ls.merged_tickets(conn)} == {"Premier R2 · from 21:00 UTC"}
        # Premier moves to R3 but the others still show their R2 (already used): wait, don't reuse.
        conn.execute("DELETE FROM lg_events WHERE competition_id = 1 AND ext_match_id >= 10010")
        _upcoming(conn, 1, 10000, 3)
        for k in range(10):
            conn.execute("INSERT INTO lg_matches VALUES (?, 1, 'x', 10000, 2, ?, 'a', 'b', 1, 0, 0, 0, 't', 's')", (10010 + k, k + 1))
        assert ls.create_merged(conn, build(), now=NOW) == []
        # The other leagues move on to R3 too: now the Premier R3 set is made, with every league.
        for cid in (2, 3, 4):
            for k in range(10):
                conn.execute("INSERT INTO lg_matches VALUES (?, ?, 'x', ?, 2, ?, 'a', 'b', 1, 0, 0, 0, 't', 's')",
                             (cid * 10000 + 10 + k, cid, cid * 10000, k + 1))
            _upcoming(conn, cid, cid * 10000, 3)
        assert len(ls.create_merged(conn, build(), now=NOW)) == 6
        r3 = [t for t in ls.merged_tickets(conn) if t["hour"] == "P10000R3"]
        assert {l["league"] for l in r3[0]["legs"]} == {1, 2, 3, 4}
        all_ids = [l["ext_match_id"] for t in ls.merged_tickets(conn) if t["k"] == 2 and t["market"] == "1X2" for l in t["legs"]]
        assert len(all_ids) == len(set(all_ids))                                   # no game bet twice
