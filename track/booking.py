"""Bet9ja booking codes + kickoff times for the tickets sent on Telegram
(requested 2026-10-24). One code per ticket (the whole accumulator), booked
once and cached in booking_codes, so every chat gets the same code and a
re-run never books again. A failed booking never blocks a message: it just
goes out saying the code is unavailable.
"""
import json
from datetime import datetime, timedelta, timezone

from db.upsert import now_iso
from scraper.zoom_api import ZoomApiError, book_bet, raw_odds_key

BOOKING = True                      # tests switch this off (no network)
KEEP_DAYS = 2
WAT = timezone(timedelta(hours=1))


def _events(conn, ext_ids):
    ids = sorted({i for i in ext_ids if i is not None})
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    return {r["ext_match_id"]: r for r in conn.execute(
        f"SELECT ext_match_id, event_id, kickoff_time FROM all_lg_events WHERE ext_match_id IN ({marks})", ids)}


def _premier_ext_ids(conn, legs):
    """Premier picks carry fixture_id; the booking needs bet9ja's matchId."""
    ids = sorted({l["fixture_id"] for l in legs if l.get("fixture_id") is not None})
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    return {r["fixture_id"]: r["ext_match_id"] for r in conn.execute(
        f"SELECT fixture_id, ext_match_id FROM fixtures WHERE fixture_id IN ({marks})", ids)}


def _ext_ids(conn, legs):
    fx = _premier_ext_ids(conn, legs)
    return [l.get("ext_match_id") if l.get("ext_match_id") is not None else fx.get(l.get("fixture_id")) for l in legs]


def kickoff(conn, legs):
    """The first kickoff among these picks, in WAT, or None."""
    times = []
    for r in _events(conn, _ext_ids(conn, legs)).values():
        try:
            times.append(datetime.fromisoformat((r["kickoff_time"] or "").replace("Z", "+00:00")))
        except ValueError:
            pass
    return min(times).astimezone(WAT) if times else None


def code_for(conn, legs, stake=500):
    """Booking code for one ticket; None if booking is off, "" if it failed."""
    if not BOOKING:
        return None
    ext = _ext_ids(conn, legs)
    events = _events(conn, ext)
    picks = []
    for leg, x in zip(legs, ext):
        key = raw_odds_key(leg["market"], leg["selection"])
        ev = events.get(x)
        if key is None or ev is None:
            return ""
        picks.append((ev["event_id"], key, leg["price"]))
    sig = json.dumps(sorted(f"{e}${k}" for e, k, _ in picks))
    row = conn.execute("SELECT code FROM booking_codes WHERE picks = ?", (sig,)).fetchone()
    if row:
        return row["code"]
    try:
        code = book_bet(picks, stake=stake)
    except ZoomApiError as e:
        print(f"   (booking code failed: {e})")
        return ""
    conn.execute("DELETE FROM booking_codes WHERE created_at < ?",
                 ((datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds"),))
    conn.execute("INSERT OR REPLACE INTO booking_codes (picks, code, created_at) VALUES (?, ?, ?)", (sig, code, now_iso()))
    conn.commit()
    return code


def code_line(code):
    """The ticket's booking-code line ("" when booking is off)."""
    if code is None:
        return ""
    if not code:
        return "📲 Booking code unavailable — add the picks by hand"
    return f"📲 Booking code: <code>{code}</code>"


def slip_extras(conn, entry):
    """For a Premier/league upcoming message: {"kickoff": WAT datetime or None,
    "codes": {slip name: code}}."""
    from track.betslips import played
    slips = played(entry["slips"])
    return {"kickoff": kickoff(conn, [l for s in slips for l in s["legs"]]),
            "codes": {s["name"]: code_for(conn, s["legs"], stake=s["stake"]) for s in slips}}
