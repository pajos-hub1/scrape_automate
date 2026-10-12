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
    res = format_result(_entry("settled", [_slip("OU1.5 top 5", 5, 5, True), _slip("BTTS top 5", 3, 5, False)]), -1234)
    assert "1/2 slips won" in res and "won ₦1,856" in res and "(3/5 picks)" in res and "−₦1,234" in res


def test_each_message_sent_once_and_failures_retried(env, monkeypatch):
    db_path, _ = env
    built = [_entry("settled", [_slip("OU1.5 top 5", 5, 5, True)]),
             _entry("upcoming", [_slip("OU1.5 top 5", 0, 5, None)], key=None, label="Round 7 (upcoming)")]
    monkeypatch.setattr(notify_mod, "build_slips", lambda rounds: built)
    monkeypatch.setattr(notify_mod, "load_rounds", lambda conn: [])

    outbox = []
    with get_connection(db_path) as conn:
        r = notify(conn, sender=outbox.append)
        assert r["sent"][0] == "hello" and len(r["sent"]) == 3 and len(outbox) == 3
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
    assert "slips won" in format_result(built[-1], 0)


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
