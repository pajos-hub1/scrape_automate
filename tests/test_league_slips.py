"""Other-league tickets, merged tickets and their combined Telegram messages."""
from datetime import datetime, timezone

import pytest

import track.league_notify as ln
import track.league_slips as ls
from db.connection import get_connection

NOW = datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_bars(monkeypatch):
    """Tests use made-up odds; switch the chance bars off unless a test sets them."""
    off = {1: {"1X2": 0.0, "HT_OU0.5": 0.0, "BTTS": 0.0}, 2: {"1X2": 0.0, "HT_OU0.5": 0.0, "BTTS": 0.0}}
    monkeypatch.setattr(ls, "MERGED_BARS", off)
    monkeypatch.setattr(ln, "MERGED_BARS", off)
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


def _upcoming(conn, cid, first_id, round_no, gpr=10, kickoff="2099-01-01T21:00:00Z"):
    for k in range(gpr):
        ext = first_id + (round_no - 1) * gpr + k
        conn.execute("INSERT INTO lg_events VALUES (?, ?, 'x', ?, ?, ?, ?, 'f')",
                     (ext, cid, ext, f"L{cid}A{k}", f"L{cid}B{k}", kickoff))
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
    texts = [t for _, t in msgs if t and "Merged tickets" in t]
    assert len(texts) == 1 and "1X2 · top 1" in texts[0]
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


def test_league_season_summary_only_after_the_last_round(env):
    connect = lambda: get_connection(env[0])
    with connect() as conn:
        _add_season(conn, 3, 3000, rounds=33, gpr=9)              # 18-team league: 34 rounds
        _upcoming(conn, 3, 3000, 34, gpr=9)
        ls.build_league(conn, 3, now=NOW)                          # staked from R34
        _upcoming(conn, 3, 8000, 1, gpr=9)                         # next season's R1 already listed
    summaries = lambda msgs: [t for _, t in msgs if t and "finished" in t]
    assert summaries(ln.league_messages({"hello"}, connect=connect)) == []   # R34 not finished yet
    with connect() as conn:
        for k in range(9):                                         # R34 finishes
            conn.execute("INSERT INTO lg_matches VALUES (?, 3, 'x', 3000, 34, ?, 'a', 'b', 2, 1, 1, 0, 't', 's')",
                         (3000 + 33 * 9 + k, k + 1))
    msgs = ln.league_messages({"hello"}, connect=connect)
    s = summaries(msgs)
    assert len(s) == 1 and "BUNDESLIGA (GERMANY) · Season 1 finished" in s[0]
    assert summaries(ln.league_messages({"hello"} | {k for k, _ in msgs}, connect=connect)) == []   # once


def test_only_chosen_leagues_go_to_telegram(env):
    connect = lambda: get_connection(env[0])
    with connect() as conn:
        for cid in (3, 5):                                         # Bundesliga is sent, Ligue 1 isn't
            _add_season(conn, cid, cid * 1000, rounds=1)
            _upcoming(conn, cid, cid * 1000, 2)
            ls.build_league(conn, cid, now=NOW)
    msgs = ln.league_messages({"hello"}, connect=connect, part="leagues")
    texts = [k for k, t in msgs if t]
    assert len(texts) == 1 and texts[0].startswith("lgmsg:3:")
    assert ("lgslips:5:5000:2", None) in msgs                      # recorded silently, no backlog later


def test_league_comparison(env):
    import track.league_report as lr
    with get_connection(env[0]) as conn:
        for cid in (1, 2):
            _add_season(conn, cid, cid * 10000, rounds=12)
        rows, merged = lr.compare_leagues(conn)
        by = {r["cid"]: r for r in rows}
        assert by[1]["rounds"] == 12 and by[1]["tickets"] == 36 and by[1]["picks"] == 180
        assert by[2]["cut"] == pytest.approx(1 - (1 / 1.5) / (1 / 1.5 + 1 / 4.0 + 1 / 6.0) * 1.5, abs=0.02)
        assert by[5]["rounds"] == 0
        text = lr.format_comparison(conn, "League comparison · after PREMIER season 33")
        assert "PREMIER (ENGLAND)" in text and "LIGA (SPAIN)" in text and "no finished rounds with odds yet" in text



def test_merged_set_is_never_sent_after_kickoff(env):
    connect = lambda: get_connection(env[0])
    with connect() as conn:
        for cid in (1, 2, 3, 4):
            _add_season(conn, cid, cid * 10000, rounds=1)
            _upcoming(conn, cid, cid * 10000, 2, kickoff="2026-01-01T07:40:00Z")      # long since played
        built = {cid: ls.build_slips(ls.load_league_rounds(conn, cid, now=NOW.replace(year=2025)),
                                     plan_fn=lambda r: ls.LEAGUE_PLAN) for cid in (1, 2, 3, 4)}
        assert ls.create_merged(conn, built, now=NOW.replace(year=2025))
    msgs = ln.league_messages({"hello"}, connect=connect, part="merged")
    assert not any(t and "Odds" in t for _, t in msgs)                               # no stale slips
    assert any(k.startswith("lgm:new:") and t is None for k, t in msgs)             # recorded silently


def test_merged_header_shows_place_by_rounds_and_result_time():
    legs = [{"league": 4, "round": 3, "kickoff": "2026-10-24T11:35:00Z"},
            {"league": 1, "round": 4, "kickoff": "2026-10-24T12:25:00Z"}]
    head = ln._set_header([{"label": "Premier R4 · from 12:25 UTC", "legs": legs, "stake": 500}])
    assert "Merged tickets · Premier R4" in head and "from 12:25" not in head
    assert "Place by 12:35 WAT" in head                          # 11:35 UTC = 12:35 in Nigeria
    assert "🇮🇹 Serie A R3 12:35 · 🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier R4 13:25" in head
    assert "Results expected about 15:05 WAT" in head           # last kickoff 13:25 WAT + 1h40


def test_merged_bars_filter_picks_and_skip_empty_tickets(env, monkeypatch):
    monkeypatch.setattr(ls, "MERGED_BARS", {1: {"1X2": 0.60, "HT_OU0.5": 0.99, "BTTS": 0.0},
                                            2: {"1X2": 0.0, "HT_OU0.5": 0.0, "BTTS": 0.0}})
    with get_connection(env[0]) as conn:
        for cid in (1, 2, 3, 4):
            _add_season(conn, cid, cid * 10000, rounds=1)
            _upcoming(conn, cid, cid * 10000, 2)
        built = {cid: ls.build_slips(ls.load_league_rounds(conn, cid, now=NOW), plan_fn=lambda r: ls.LEAGUE_PLAN)
                 for cid in (1, 2, 3, 4)}
        keys = ls.create_merged(conn, built, now=NOW)
        assert not any("|HT_OU0.5|1" in k for k in keys)              # nobody reaches 99%: no ticket
        t = {ls.merged_name(t): t for t in ls.merged_tickets(conn)}
        assert all(l["odds_p"] >= 0.60 for l in t["1X2 merged top 1"]["legs"])
    monkeypatch.setattr(ln, "MERGED_BARS", ls.MERGED_BARS)
    msg = next(x for _, x in ln.league_messages({"hello"}, connect=lambda: get_connection(env[0]), part="merged")
               if x and "Odds" in x)
    assert "No ticket this time" in msg and "HT Over/Under 0.5 · top 1 (none reached 99%)" in msg
    assert "%" in msg.split("1X2 · top 1")[1].split("\n")[2]                   # each pick shows its chance


def test_weekly_summary_sent_sunday_evening_once_a_week():
    from track.weekly import weekly_key
    assert weekly_key(datetime(2026, 10, 25, 19, 0, tzinfo=timezone.utc)) == "weekly:2026-W43"   # Sun 20:00 WAT
    assert weekly_key(datetime(2026, 10, 25, 18, 59, tzinfo=timezone.utc)) is None
    assert weekly_key(datetime(2026, 10, 24, 21, 0, tzinfo=timezone.utc)) is None                # Saturday


def test_weekly_summary_and_bars_split(env, monkeypatch):
    from track.weekly import format_weekly
    connect = lambda: get_connection(env[0])
    with connect() as conn:
        for cid in (3, 5):
            _add_season(conn, cid, cid * 1000, rounds=1)
            _upcoming(conn, cid, cid * 1000, 2)
            ls.build_league(conn, cid, now=NOW)                    # staked from R2
            for k in range(10):                                    # R2 finishes: home sides win 2-1
                conn.execute("INSERT INTO lg_matches VALUES (?, ?, 'x', ?, 2, ?, 'a', 'b', 2, 1, 1, 0, 't', ?)",
                             (cid * 1000 + 10 + k, cid, cid * 1000, k + 1, "2026-10-24T10:00:00+00:00"))
        text = format_weekly(conn, now=datetime(2026, 10, 25, 19, 0, tzinfo=timezone.utc))
    assert "Weekly summary" in text and "BUNDESLIGA (GERMANY)</b>: <b>" in text
    assert "Dashboard only: 🇫🇷 Ligue 1" in text                   # not sent per round, still summarised
    assert "Best ticket" in text

    t = {"settled": True, "won": False, "returned": 0.0, "stake": 500}
    merged = [dict(t, barred=True), dict(t, barred=True, won=True, returned=1500.0), dict(t, barred=False)]
    assert ln.merged_era(merged, True) == {"n": 2, "won": 1, "profit": 500.0}
    assert ln.merged_era(merged, False) == {"n": 1, "won": 0, "profit": -500.0}


def test_booking_codes_and_kickoff_on_league_slips(env, monkeypatch):
    import track.booking as booking
    calls = []
    monkeypatch.setattr(booking, "BOOKING", True)
    monkeypatch.setattr(booking, "book_bet", lambda picks, stake=500: calls.append(picks) or f"CODE{len(calls)}")
    connect = lambda: get_connection(env[0])
    with connect() as conn:
        _add_season(conn, 3, 3000, rounds=1)
        _upcoming(conn, 3, 3000, 2, kickoff="2099-01-01T14:30:00Z")
        ls.build_league(conn, 3, now=NOW)
    text = next(t for k, t in ln.league_messages({"hello"}, connect=connect, part="leagues") if t)
    assert "Starts 15:30 WAT" in text                                  # kickoff in WAT
    assert text.count("📲 Booking code: <code>CODE") == 3              # one per ticket
    assert all(3010 <= e < 3020 and k.startswith("VS_") for p in calls for e, k, _ in p)   # R2's event ids
    n = len(calls)
    ln.league_messages({"hello"}, connect=connect, part="leagues")     # same picks: cached, no new booking
    assert len(calls) == n

    monkeypatch.setattr(booking, "book_bet", lambda picks, stake=500: (_ for _ in ()).throw(booking.ZoomApiError("down")))
    with connect() as conn:
        conn.execute("DELETE FROM booking_codes")
    text = next(t for k, t in ln.league_messages({"hello"}, connect=connect, part="leagues") if t)
    assert "Booking code unavailable" in text                          # message still goes out
