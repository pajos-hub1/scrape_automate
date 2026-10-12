"""Telegram notifications for the paper betslips (track/betslips.py).

Each cycle (every ~15 min) this sends, at most once each:
  - the upcoming round's slips, as soon as that round's fixtures are known
  - the most recently settled round's result: every slip WON/LOST, the
    round's ₦ result and the running ₦ total
  - a one-time "connected" message the first time it runs

Which messages have gone out is recorded in the `notifications` table, so
a re-polled round never gets re-sent. Pipeline-failure alerts are sent by
the workflow itself (cycle.yml), since a failed cycle may never get here.

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment (GitHub
repo secrets). Without them it does nothing; send errors are logged and
never fail the cycle.
"""
import hashlib
import html
import json
import os
import urllib.request

from db.upsert import now_iso
from track.betslips import build_slips, load_rounds

MAX_LEN = 4000   # Telegram's hard limit is 4096 chars per message

MARKET_NAMES = {"1X2": "1X2", "OU1.5": "Over/Under 1.5", "HT_OU0.5": "HT Over/Under 0.5",
                "BTTS": "BTTS", "HT_OU1.5": "HT Over/Under 1.5", "OU2.5": "Over/Under 2.5"}


def _naira(v, sign=False):
    s = f"₦{abs(v):,.0f}"
    if sign:
        return ("+" if v >= 0 else "−") + s
    return s


def _slip_title(slip):
    name = slip["name"]
    for code, label in MARKET_NAMES.items():
        if name.startswith(code + " "):
            return label + name[len(code):]
    return name.replace("-game", "-game slip")


def format_upcoming(entry):
    slips = entry["slips"]
    lines = [f"🎟 <b>{html.escape(entry['round']['label'])}: {len(slips)} paper slips</b>",
             f"₦{slips[0]['stake']:,} each · {_naira(sum(s['stake'] for s in slips))} total", ""]
    for slip in slips:
        lines.append(f"<b>{html.escape(_slip_title(slip))}</b>: odds {slip['odds']:,.2f} → pays "
                     f"{_naira(slip['stake'] * slip['odds'])}")
        for leg in slip["legs"]:
            a, b = leg["teams"]
            lines.append(f"  • {html.escape(a)} v {html.escape(b)}: "
                         f"{MARKET_NAMES[leg['market']]} <b>{html.escape(leg['selection'])}</b> @{leg['price']:.2f}")
        lines.append("")
    return "\n".join(lines).strip()


def format_result(entry, running_total):
    slips = entry["slips"]
    won = [s for s in slips if s["won"]]
    result = sum(s["returned"] - s["stake"] for s in slips)
    icon = "✅" if result > 0 else "❌"
    lines = [f"{icon} <b>{html.escape(entry['round']['label'])} result: {len(won)}/{len(slips)} slips won, "
             f"{_naira(result, sign=True)}</b>", ""]
    for slip in slips:
        if slip["won"]:
            lines.append(f"✅ {html.escape(_slip_title(slip))} @{slip['odds']:,.2f} → won {_naira(slip['returned'])}")
        else:
            lines.append(f"❌ {html.escape(_slip_title(slip))} ({slip['legs_won']}/{len(slip['legs'])} picks) "
                         f"−{_naira(slip['stake'])}")
    lines += ["", f"Running total, all rounds: <b>{_naira(running_total, sign=True)}</b>"]
    return "\n".join(lines)


def _upcoming_key(entry):
    # The live batch is re-polled every cycle (its scraped_at moves), so key
    # on the round label + the actual pairings instead.
    pairings = sorted(leg["teams"] for slip in entry["slips"] for leg in slip["legs"])
    digest = hashlib.sha1(json.dumps([entry["round"]["label"], pairings]).encode()).hexdigest()[:16]
    return f"slips:{digest}"


def pending_messages(conn, built=None):
    """[(key, text)] not yet sent, oldest first."""
    built = built if built is not None else build_slips(load_rounds(conn))
    sent = {r["key"] for r in conn.execute("SELECT key FROM notifications")}
    out = []
    if "hello" not in sent:
        out.append(("hello", "👋 <b>NewFiti connected.</b> You'll get each round's paper betslips "
                             "(₦500 per slip, simulated) and their results here, plus an alert if "
                             "the pipeline fails."))

    settled = [e for e in built if e["round"]["status"] == "settled" and e["slips"]]
    if settled:
        last = settled[-1]
        key = f"result:{last['round']['key'][0]}-{last['round']['key'][1]}"
        if key not in sent:
            running = sum(s["returned"] - s["stake"] for e in settled for s in e["slips"])
            out.append((key, format_result(last, running)))

    upcoming = [e for e in built if e["round"]["status"] == "upcoming" and e["slips"]]
    if upcoming:
        key = _upcoming_key(upcoming[-1])
        if key not in sent:
            out.append((key, format_upcoming(upcoming[-1])))
    return out


def send_telegram(text, token=None, chat_id=None):
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    for chunk in _chunks(text):
        body = json.dumps({"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                           "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            if not json.load(resp).get("ok"):
                raise RuntimeError("Telegram returned ok=false")


def _chunks(text):
    """Split on blank lines so no message exceeds MAX_LEN."""
    parts, cur = [], ""
    for block in text.split("\n\n"):
        candidate = f"{cur}\n\n{block}" if cur else block
        if len(candidate) > MAX_LEN and cur:
            parts.append(cur)
            cur = block
        else:
            cur = candidate
    if cur:
        parts.append(cur[:MAX_LEN])
    return parts


def _describe_error(e):
    detail = type(e).__name__
    code = getattr(e, "code", None)
    if code is not None:
        detail += f" {code}"
    try:
        detail += f" ({json.load(e)['description']})"   # HTTPError body from Telegram
    except Exception:  # noqa: BLE001
        pass
    return detail


def notify(conn, sender=send_telegram):
    """Send every pending message; record each one only after it's sent."""
    if sender is send_telegram and not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        return {"skipped": "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"}
    sent, failed = [], []
    for key, text in pending_messages(conn):
        try:
            sender(text)
        except Exception as e:  # noqa: BLE001 -- must never fail the cycle
            # Never echo the request URL: it contains the bot token. Telegram's
            # own error description (e.g. "Bad Request: chat not found") is safe.
            failed.append(f"{key}: {_describe_error(e)}")
            continue
        conn.execute("INSERT OR REPLACE INTO notifications (key, sent_at) VALUES (?, ?)", (key, now_iso()))
        conn.commit()
        sent.append(key)
    return {"sent": sent, "failed": failed}
