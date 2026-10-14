from conftest import add_played_fixture

import track.notify as notify_mod
from db.connection import get_connection
from track.betslips import build_slips
from track.notify import _chunks, format_result, format_upcoming, notify


def _slip(name, legs_won, n, won, price=1.3):
    legs = [{"teams": (f"A{i}", f"B{i}"), "market": "OU1.5", "selection": "Over", "price": price,
             "won": (i < legs_won) if won is not None else None} for i in range(n)]
    odds = price ** n
    return {"name": name, "legs": legs, "odds": odds, "stake": 500, "settled": won is not None,
            "won": won, "returned": 500 * odds if won else 0.0, "legs_won": legs_won}


def _entry(status, slips, key=(29, 6), label="S29 R6", number=6):
    return {"round": {"status": status, "key": key, "label": label, "number": number}, "slips": slips}


def test_formats_contain_the_essentials():
    up = format_upcoming(_entry("upcoming", [_slip("OU1.5 top 5", 0, 5, None)], label="Round 8 (upcoming)"))
    assert "Round 8" in up and "Over/Under 1.5 top 5" in up and "A0 v B0" in up
    assert "Over 1.5</b> @1.30" in up
    e = _entry("settled", [_slip("OU1.5 top 5", 5, 5, True), _slip("BTTS top 5", 3, 5, False)])
    e["round"]["season"] = 29
    res = format_result(e, {"rounds": 6, "profit": -1234})
    assert "1/2 slips won" in res and "won ₦1,856" in res and "(3/5 picks)" in res
    assert "Season 29 so far (6 rounds, from ₦0)" in res and "−₦1,234" in res


def test_each_message_sent_once_and_failures_retried(env, monkeypatch):
    db_path, _ = env
    built = [_entry("settled", [_slip("OU1.5 top 5", 5, 5, True)]),
             _entry("upcoming", [_slip("OU1.5 top 5", 0, 5, None)], key=None, label="Round 7 (upcoming)")]
    monkeypatch.setattr(notify_mod, "build_slips", lambda rounds: built)
    monkeypatch.setattr(notify_mod, "load_rounds", lambda conn: [])

    outbox = []
    with get_connection(db_path) as conn:
        r = notify(conn, sender=outbox.append)
        assert r["sent"][0] == "hello" and len(outbox) == len(r["sent"])
        assert {"hello", "result:29-6"} <= set(r["sent"])
        assert notify(conn, sender=outbox.append)["sent"] == []     # nothing new -> nothing sent

        built.append(_entry("upcoming", [_slip("OU1.5 top 5", 0, 5, None)], key=None, label="Round 8 (upcoming)", number=8))
        def broken(text):
            raise OSError("network down")
        r = notify(conn, sender=broken)
        assert r["sent"] == [] and len(r["failed"]) == 1             # not recorded...
        assert len(notify(conn, sender=outbox.append)["sent"]) == 1  # ...so it's retried next run


def test_skips_quietly_without_credentials(env, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with get_connection(env[0]) as conn:
        assert "skipped" in notify(conn)


def test_long_messages_split_under_limit():
    text = "\n\n".join("x" * 900 for _ in range(10))
    parts = _chunks(text)
    assert len(parts) > 1 and all(len(p) <= notify_mod.MAX_LEN for p in parts)
    assert "".join(parts).count("x") == 9000


def test_real_slips_format(env):
    db_path, _ = env
    with get_connection(db_path) as conn:
        for n in range(1, 11):
            add_played_fixture(conn, n, 1, 0)
        from track.betslips import load_rounds
        built = build_slips(load_rounds(conn))
    assert built and built[-1]["slips"]
    assert "slips won" in format_result(built[-1])


def test_pick_labels():
    from track.notify import pick_label
    assert pick_label("OU1.5", "Over") == "Over 1.5"
    assert pick_label("OU2.5", "Under") == "Under 2.5"
    assert pick_label("HT_OU0.5", "Over") == "HT Over 0.5"
    assert pick_label("HT_OU1.5", "Under") == "HT Under 1.5"
    assert pick_label("1X2", "Home") == "1X2 Home"
    assert pick_label("BTTS", "No") == "BTTS No"


def test_upcoming_key_ignores_display_label():
    from track.notify import _upcoming_key
    slips = [_slip("OU1.5 top 5", 0, 5, None)]
    old = _upcoming_key(_entry("upcoming", slips, key=None, label="Round 14 (upcoming)", number=14))
    new = _upcoming_key(_entry("upcoming", slips, key=None, label="S29 R14", number=14))
    assert old == new


def _upcoming(price, number=14):
    e = _entry("upcoming", [_slip("OU1.5 top 5", 0, 5, None, price=price)], key=None,
               label=f"S29 R{number}", number=number)
    e["round"]["games"] = {i: {"teams": (f"A{i}", f"B{i}")} for i in range(10)}
    return e


def _run(env, monkeypatch, built, outbox):
    monkeypatch.setattr(notify_mod, "build_slips", lambda rounds: built)
    monkeypatch.setattr(notify_mod, "load_rounds", lambda conn: [])
    with get_connection(env[0]) as conn:
        return notify(conn, sender=outbox.append)


def test_unchanged_slips_not_resent_changed_slips_sent_as_update(env, monkeypatch):
    outbox, built = [], [_upcoming(1.30)]
    assert len(_run(env, monkeypatch, built, outbox)["sent"]) == 2          # hello + slips
    assert _run(env, monkeypatch, built, outbox)["sent"] == []             # same content: silent
    built[0] = _upcoming(1.25)                                             # same round, picks changed
    assert len(_run(env, monkeypatch, built, outbox)["sent"]) == 1
    assert "Updated" in outbox[-1]
    assert _run(env, monkeypatch, built, outbox)["sent"] == []             # and only once
    built[0] = _upcoming(1.30, number=15)                                  # a new round: normal message
    _run(env, monkeypatch, built, outbox)
    assert "Updated" not in outbox[-1] and "S29 R15" in outbox[-1]


def test_round_sent_under_legacy_key_is_not_resent(env, monkeypatch):
    from track.notify import _legacy_upcoming_key
    entry = _upcoming(1.30)
    with get_connection(env[0]) as conn:
        for k in ("hello", _legacy_upcoming_key(entry)):
            conn.execute("INSERT INTO notifications (key, sent_at) VALUES (?, 'x')", (k,))
    outbox = []
    r = _run(env, monkeypatch, [entry], outbox)
    assert outbox == [] and len(r["sent"]) == 1      # recorded silently under the new key


_clock = iter(range(10_000))


def _settled(season, rnd, profit_won):
    e = _entry("settled", [_slip("OU1.5 top 5", 5 if profit_won else 4, 5, profit_won)],
               key=(season, rnd), label=f"S{season} R{rnd}", number=rnd)
    e["round"]["season"] = season
    e["round"]["sort"] = f"{next(_clock):06d}"       # chronological, like real rounds
    return e


def test_season_totals_restart_from_zero():
    from track.betslips import season_totals
    built = [_settled(28, 37, False), _settled(28, 38, True), _settled(29, 1, False), _settled(29, 2, False)]
    t = season_totals(built)
    assert t[28]["rounds"] == 2 and t[28]["complete"]
    assert t[29]["rounds"] == 2 and not t[29]["complete"]
    assert t[29]["profit"] == -1000                      # S29 counts only its own slips
    assert t[28]["profit"] == -500 + (500 * 1.3 ** 5 - 500)


def test_season_summary_sent_once_for_season_that_just_ended(env, monkeypatch):
    # Ids out of time order, like the real S28 (older) / S27 (newer).
    built = [_settled(28, 38, False), _settled(27, 38, True), _settled(29, 1, False)]
    outbox = []
    r = _run(env, monkeypatch, built, outbox)
    assert "season:27" in r["sent"] and "season:28" not in r["sent"]   # the one just played, by time
    summary = next(t for t in outbox if "Season 27 finished" in t)
    assert "next season starts again from ₦0" in summary
    assert "season:27" not in _run(env, monkeypatch, built, outbox)["sent"]


def test_no_summary_for_seasons_that_ended_long_ago(env, monkeypatch):
    built = [_settled(27, 38, True)] + [_settled(29, r, False) for r in range(1, 10)]
    r = _run(env, monkeypatch, built, [])
    assert not any(k.startswith("season:") for k in r["sent"])


def test_season_order_follows_time_not_id():
    from track.betslips import season_totals
    t = season_totals([_settled(28, 38, False), _settled(27, 37, True), _settled(29, 1, False)])
    assert list(t) == [28, 27, 29]
    assert t[27]["complete"] and not t[29]["complete"]


def test_two_chats_each_get_every_message_once(env, monkeypatch):
    built = [_settled(29, 5, True), _upcoming(1.30, number=6)]
    sent_to = []
    sender = lambda text, chat_id: sent_to.append(chat_id)
    r = _run_chats(env, monkeypatch, built, sender, ["111", "222"])
    assert sent_to.count("111") == sent_to.count("222") == 3          # hello, result, slips each
    assert _run_chats(env, monkeypatch, built, sender, ["111", "222"])["sent"] == []
    with get_connection(env[0]) as conn:
        keys = [k for (k,) in conn.execute("SELECT key FROM notifications")]
    assert not any("222" in k or "111" in k for k in keys)              # ids never stored


def test_adding_second_chat_does_not_resend_to_first(env, monkeypatch):
    built = [_settled(29, 5, True), _upcoming(1.30, number=6)]
    sent_to = []
    sender = lambda text, chat_id: sent_to.append(chat_id)
    _run_chats(env, monkeypatch, built, sender, ["111"])
    sent_to.clear()
    _run_chats(env, monkeypatch, built, sender, ["111", "222"])
    assert sent_to == ["222", "222", "222"]                             # only the new chat catches up


def test_one_chat_failing_does_not_block_the_other(env, monkeypatch):
    built = [_settled(29, 5, True)]
    got = []
    def sender(text, chat_id):
        if chat_id == "bad":
            raise OSError("down")
        got.append(chat_id)
    r = _run_chats(env, monkeypatch, built, sender, ["111", "bad"])
    assert got == ["111", "111", "111"] and len(r["failed"]) == 2      # hello, result + delivery alert
    r = _run_chats(env, monkeypatch, built, lambda text, chat_id: got.append(chat_id), ["111", "bad"])
    assert got[-3:] == ["bad", "bad", "111"]       # retried for the failed chat, then "fixed" note to owner


def _run_chats(env, monkeypatch, built, sender, chats):
    monkeypatch.setattr(notify_mod, "build_slips", lambda rounds: built)
    monkeypatch.setattr(notify_mod, "load_rounds", lambda conn: [])
    with get_connection(env[0]) as conn:
        return notify(conn, sender=sender, chats=chats)


def test_owner_reminded_every_run_until_fixed(env, monkeypatch):
    built = [_settled(29, 5, True)]
    got, broken = [], {"friend"}
    def sender(text, chat_id):
        if chat_id in broken:
            raise OSError("blocked")
        got.append((chat_id, text))
    alerts = lambda: [t for c, t in got if c == "me" and "couldn't be delivered" in t]
    for _ in range(3):                                                  # three runs, still broken
        r = _run_chats(env, monkeypatch, built, sender, ["me", "friend"])
        assert r.get("alerted_owner") and not r.get("owner_failed")
    assert len(alerts()) == 3 and "chat 2" in alerts()[0] and "failing since" in alerts()[-1]
    broken.clear()                                                      # fixed
    r = _run_chats(env, monkeypatch, built, sender, ["me", "friend"])
    assert r.get("recovered") and any("delivery fixed" in t for c, t in got if c == "me")
    r = _run_chats(env, monkeypatch, built, sender, ["me", "friend"])  # quiet afterwards
    assert not r.get("alerted_owner") and not r.get("recovered") and len(alerts()) == 3


def test_owner_chat_failing_is_flagged(env, monkeypatch):
    def sender(text, chat_id):
        raise OSError("down")
    r = _run_chats(env, monkeypatch, [_settled(29, 5, True)], sender, ["me", "friend"])
    assert r["owner_failed"] is True and not r.get("alerted_owner")


def test_health_tile_shows_telegram_status(env):
    from dashboard.build import _render_health_section
    import json as _json
    with get_connection(env[0]) as conn:
        conn.execute("INSERT INTO notify_status (id, at, summary) VALUES (1, '2026-10-14T10:00:00+00:00', ?)",
                     (_json.dumps({"sent": [], "failed": ["chat2 hello: OSError"]}),))
        from dashboard.health import missed_rounds_last_24h  # noqa: F401
        import dashboard.health as h
        h.fetch_recent_runs = lambda hours=24: None
        html_out = _render_health_section(h.get_health(conn))
    assert "Telegram" in html_out and "Partly failing" in html_out
