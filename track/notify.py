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
from config import ROUNDS_PER_SEASON
from track.betslips import build_slips, load_rounds, season_totals, summarize

MAX_LEN = 4000   # Telegram's hard limit is 4096 chars per message
NEW_SEASON_WINDOW = 3   # a new season this young means the previous one just ended

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


def format_upcoming(entry, updated=False):
    slips = entry["slips"]
    head = (f"🔄 <b>Updated: {html.escape(entry['round']['label'])} slips changed</b> (results since the "
            f"last message shifted the picks; these replace the earlier ones)\n" if updated else "")
    lines = [head + f"🎟 <b>{html.escape(entry['round']['label'])}: {len(slips)} paper slips (upcoming)</b>",
             f"₦{slips[0]['stake']:,} each · {_naira(sum(s['stake'] for s in slips))} total", ""]
    for slip in slips:
        lines.append(f"<b>{html.escape(_slip_title(slip))}</b>: odds {slip['odds']:,.2f} → pays "
                     f"{_naira(slip['stake'] * slip['odds'])}")
        for leg in slip["legs"]:
            a, b = leg["teams"]
            lines.append(f"  • {html.escape(a)} v {html.escape(b)}: "
                         f"<b>{html.escape(pick_label(leg['market'], leg['selection']))}</b> @{leg['price']:.2f}")
        lines.append("")
    return "\n".join(lines).strip()


def format_result(entry, season_total=None):
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
    if season_total is not None:
        season = entry["round"].get("season")
        lines += ["", f"Season {season} so far ({season_total['rounds']} rounds, from ₦0): "
                      f"<b>{_naira(season_total['profit'], sign=True)}</b>"]
    return "\n".join(lines)


def format_season_summary(season, totals, by_type):
    """End-of-season report: that season's own money, starting from ₦0."""
    icon = "🏆" if totals["profit"] > 0 else "📉"
    lines = [f"{icon} <b>Season {season} finished: {_naira(totals['profit'], sign=True)}</b>",
             f"{totals['rounds']} rounds · {totals['won']}/{totals['slips']} slips won",
             f"Staked {_naira(totals['staked'])} · returned {_naira(totals['returned'])}", "",
             "<b>By slip type</b>"]
    for kind, t in sorted(by_type.items(), key=lambda kv: kv[1]["profit"], reverse=True):
        title = _slip_title({"name": kind}) if kind != "All-games slip" else "10-game slip"
        lines.append(f"{'✅' if t['profit'] > 0 else '❌'} {html.escape(title)}: {t['won']}/{t['slips']} won, "
                     f"{_naira(t['profit'], sign=True)}")
    lines += ["", "The next season starts again from ₦0."]
    return "\n".join(lines)


def pick_label(market, selection):
    """Short, readable pick: "Over 1.5", "HT Under 0.5", "1X2 Home", "BTTS No"."""
    if market.startswith("HT_OU"):
        return f"HT {selection} {market[5:]}"
    if market.startswith("OU"):
        return f"{selection} {market[2:]}"
    return f"{market} {selection}"


def _sha(obj, n):
    return hashlib.sha1(json.dumps(obj).encode()).hexdigest()[:n]


def _round_id(entry):
    """Which round this is: its number + every game in it. Independent of
    the picks, so it stays the same when the slips' content changes."""
    games = entry["round"].get("games")
    teams = ([list(g["teams"]) for g in games.values()] if games
             else [list(leg["teams"]) for slip in entry["slips"] for leg in slip["legs"]])
    return _sha([entry["round"]["number"], sorted(set(map(tuple, teams)))], 10)


def _content_id(entry):
    """Which version of the slips: every pick and price. Changes when, e.g.,
    a round settling shifts the history scores and reorders the top 5."""
    return _sha([[s["name"], [[list(l["teams"]), l["market"], l["selection"], l["price"]] for l in s["legs"]]]
                 for s in entry["slips"]], 10)


def _upcoming_key(entry):
    return f"slips:{_round_id(entry)}:{_content_id(entry)}"


def _legacy_upcoming_key(entry):
    # Key format used before 2026-10-12 15:00 UTC. Only consulted so a round
    # already sent under it isn't re-sent after the switch.
    pairings = sorted(leg["teams"] for slip in entry["slips"] for leg in slip["legs"])
    legacy_label = f"Round {entry['round']['number']} (upcoming)"
    return "slips:" + hashlib.sha1(json.dumps([legacy_label, pairings]).encode()).hexdigest()[:16]


def pending_messages(conn, built=None):
    """[(key, text)] not yet sent, oldest first. text=None means "record the
    key without sending" (a round already sent under the legacy key).

    Upcoming slips are re-sent -- marked as updated -- only when their
    content differs from every version already sent for that round."""
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
            totals = season_totals(built).get(last["round"].get("season"))
            out.append((key, format_result(last, totals)))

    # Season summary, sent once, only when a season ends NOW: the current
    # season reaching its last round, or a new season's first rounds coming
    # in (then summarise the one just before it). Never an old season --
    # so switching this on doesn't announce seasons that ended long ago.
    seasons = season_totals(built)          # in the order played
    order = list(seasons)
    finished_now = None
    if order and seasons[order[-1]]["last_round"] >= ROUNDS_PER_SEASON:
        finished_now = order[-1]
    elif len(order) >= 2 and seasons[order[-1]]["rounds"] <= NEW_SEASON_WINDOW:
        finished_now = order[-2]
    if finished_now is not None:
        key = f"season:{finished_now}"
        if key not in sent:
            out.append((key, format_season_summary(finished_now, seasons[finished_now],
                                                   summarize(built, season=finished_now))))

    upcoming = [e for e in built if e["round"]["status"] == "upcoming" and e["slips"]]
    if upcoming:
        entry = upcoming[-1]
        key = _upcoming_key(entry)
        round_prefix = f"slips:{_round_id(entry)}:"
        if key in sent:
            pass                                                   # this exact version already went out
        elif any(k.startswith(round_prefix) for k in sent):
            out.append((key, format_upcoming(entry, updated=True)))  # same round, picks changed
        elif _legacy_upcoming_key(entry) in sent:
            out.append((key, None))                                # sent before the key format changed
        else:
            out.append((key, format_upcoming(entry)))
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
    """Send every pending message; record each one only after it's sent.
    The outcome is also saved to notify_status for diagnosis."""
    result = _notify(conn, sender)
    conn.execute("INSERT OR REPLACE INTO notify_status (id, at, summary) VALUES (1, ?, ?)",
                 (now_iso(), json.dumps(result)))
    conn.commit()
    return result


def _notify(conn, sender):
    if sender is send_telegram:
        missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.environ.get(k)]
        if missing:
            return {"skipped": f"not set: {', '.join(missing)}"}
    sent, failed = [], []
    for key, text in pending_messages(conn):
        try:
            if text is not None:
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
