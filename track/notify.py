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
from datetime import datetime, timezone
import html
import json
import os
import urllib.request

from db.upsert import now_iso
from config import ROUNDS_PER_SEASON
from track.cuts import cut_stats, verdict as cut_verdict
from track.league_notify import league_messages
from track.betslips import (_all_kinds, build_slips, load_rounds, played, played_kinds, round_stake, stake_for,
                            season_totals, summarize)

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


PREMIER_NAME = "PREMIER (ENGLAND)"


def _title(entry, league):
    """League name in capitals first, so every message says which league."""
    return f"{league} · {entry['round']['label']}"


def format_upcoming(entry, updated=False, league=PREMIER_NAME):
    slips = played(entry["slips"])
    title = html.escape(_title(entry, league))
    head = (f"🔄 <b>Updated: {title} slips changed</b> "
            f"(these replace the earlier ones)\n" if updated else "")
    lines = [head + f"🎟 <b>{title}: {len(slips)} paper slips (upcoming)</b>",
             f"{_naira(sum(s['stake'] for s in slips))} total", ""]
    for slip in slips:
        lines.append(f"<b>{html.escape(_slip_title(slip))}</b> (stake {_naira(slip['stake'])}): odds "
                     f"{slip['odds']:,.2f} → pays {_naira(slip['stake'] * slip['odds'])}")
        for leg in slip["legs"]:
            a, b = leg["teams"]
            lines.append(f"  • {html.escape(a)} v {html.escape(b)}: "
                         f"<b>{html.escape(pick_label(leg['market'], leg['selection']))}</b> @{leg['price']:.2f}")
        lines.append("")
    return "\n".join(lines).strip()


def format_result(entry, season_total=None, league=PREMIER_NAME, season_name=None):
    slips = played(entry["slips"])
    won = [s for s in slips if s["won"]]
    result = sum(s["returned"] - s["stake"] for s in slips)
    icon = "✅" if result > 0 else "❌"
    lines = [f"{icon} <b>{html.escape(_title(entry, league))} result: {len(won)}/{len(slips)} slips won, "
             f"{_naira(result, sign=True)}</b>", ""]
    for slip in slips:
        if slip["won"]:
            lines.append(f"✅ {html.escape(_slip_title(slip))} @{slip['odds']:,.2f} → won {_naira(slip['returned'])}")
        else:
            lines.append(f"❌ {html.escape(_slip_title(slip))} ({slip['legs_won']}/{len(slip['legs'])} picks) "
                         f"−{_naira(slip['stake'])}")
    if season_total is not None:
        season = season_name or f"Season {entry['round'].get('season')}"
        lines += ["", f"{season} so far ({season_total['rounds']} rounds, from ₦0): "
                      f"<b>{_naira(season_total['profit'], sign=True)}</b>"]
        if season_total.get("by_legs"):
            parts = [f"top {n}: {_naira(p, sign=True)}" for n, p in sorted(season_total["by_legs"].items(), reverse=True)]
            lines.append("This season, " + " · ".join(parts))
    return "\n".join(lines)


# Key of the last mid-season correction (S31, Best 6 added). Frozen: since
# 2026-10-19 slip changes take effect from a NEW season (track/betslips.py
# PLANS), which starts from ₦0 and needs no recalculation message. Change
# this string only if a mid-season recalculation is ever wanted again.
RECALC_KEY = "recalc:drop-all-games-ht_ou1.5-ou1.5-ou2.5:btts=1000:best6from31"
RECALC_BEFORE = "2026-10-16T21:00:00+00:00"   # chats connected before the latest change


def format_recalc(season, entries, totals):
    title = lambda k: "10-game slip" if k == "All-games slip" else _slip_title({"name": k})
    staked = ", ".join(f"{title(k)} {_naira(stake_for(k, season))}" for k in played_kinds(season))
    dropped = ", ".join(sorted(title(k) for k in _all_kinds()
                               if k not in played_kinds(season) and not k.endswith("top 3")))
    lines = [f"🔁 <b>Slips changed: {len(played_kinds(season))} per round now "
             f"({_naira(round_stake(season))})</b>",
             f"Staked: {html.escape(staked)}.",
             f"Dropped -- no longer sent or counted: {html.escape(dropped)}.",
             "", f"<b>Season {season} recalculated from round 1</b>:"]
    for e in entries:
        slips = played(e["slips"])
        won = sum(1 for s in slips if s["won"])
        result = sum(s["returned"] - s["stake"] for s in slips)
        lines.append(f"{'✅' if result > 0 else '❌'} {html.escape(e['round']['label'])}: {won}/{len(slips)} won, "
                     f"{_naira(result, sign=True)}")
    lines += ["", f"Season {season} so far ({totals['rounds']} rounds, from ₦0): "
                  f"<b>{_naira(totals['profit'], sign=True)}</b>"]
    return "\n".join(lines)


WEEKLY_CUTS = True
LEAGUE_MESSAGES = True


def _cut_line(r):
    pct = lambda x: f"{x / r['n'] * 100:.1f}%"
    if r["unit"] in ("games", "picks"):
        body = f"{r['actual']:,.0f} of {r['n']:,} {r['unit']} ({pct(r['actual'])}) -- odds said {pct(r['expected'])}"
    else:
        body = f"{r['actual']:,.0f} of {r['n']:,} -- odds said {r['expected']:,.0f}"
    return f"{'⚠️' if r['unusual'] else '✅'} {html.escape(r['label'])}: {body}"


def format_cut_check(season, rounds, season_rows, all_rows):
    lines = [f"📊 <b>Weekly cut check</b> -- are \"sure\" picks losing more than the odds say?", "",
             f"<b>Season {season}</b> ({rounds} rounds): {html.escape(cut_verdict(season_rows))}"]
    lines += [_cut_line(r) for r in season_rows]
    lines += ["", f"<b>All seasons</b>: {html.escape(cut_verdict(all_rows))}"]
    lines += [_cut_line(r) for r in all_rows]
    lines += ["", "✅ = within normal luck. ⚠️ would mean results are drifting from the odds -- "
                  "worth watching only if it repeats week after week."]
    return "\n".join(lines)


def format_catchup(entries):
    first, last = entries[0]["round"]["label"], entries[-1]["round"]["label"]
    lines = [f"📋 <b>Missed results: {html.escape(first)} – {html.escape(last)}</b>",
             "These rounds settled late (a season-tracking fault, now fixed):", ""]
    for e in entries:
        slips = played(e["slips"])
        won = [s for s in slips if s["won"]]
        result = sum(s["returned"] - s["stake"] for s in slips)
        detail = ", ".join(_slip_title(s) for s in won) if won else "none"
        lines.append(f"{'✅' if result > 0 else '❌'} {html.escape(e['round']['label'])}: {len(won)}/{len(slips)} won, "
                     f"{_naira(result, sign=True)} (won: {html.escape(detail)})")
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
                 for s in played(entry["slips"])], 10)


def _upcoming_key(entry):
    return f"slips:{_round_id(entry)}:{_content_id(entry)}"


def _legacy_upcoming_key(entry):
    # Key format used before 2026-10-12 15:00 UTC. Only consulted so a round
    # already sent under it isn't re-sent after the switch.
    pairings = sorted(leg["teams"] for slip in entry["slips"] for leg in slip["legs"])
    legacy_label = f"Round {entry['round']['number']} (upcoming)"
    return "slips:" + hashlib.sha1(json.dumps([legacy_label, pairings]).encode()).hexdigest()[:16]


def chat_ids():
    """TELEGRAM_CHAT_ID may hold several chats, comma-separated. The first
    is the owner's: it also gets the workflow's failure alerts."""
    return [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]


def _chat_suffix(index, chat):
    # The first chat keeps the original, unsuffixed keys (so nothing already
    # sent to it is re-sent). Others get "@<hash>": the DB is committed to a
    # public repo, so the chat id itself is never stored.
    return "" if index == 0 else "@" + hashlib.sha1(str(chat).encode()).hexdigest()[:8]


def pending_messages(conn, built=None, suffix=""):
    """[(key, text)] not yet sent, oldest first. text=None means "record the
    key without sending" (a round already sent under the legacy key).

    Upcoming slips are re-sent -- marked as updated -- only when their
    content differs from every version already sent for that round."""
    built = built if built is not None else build_slips(load_rounds(conn))
    all_rows = conn.execute("SELECT key, sent_at FROM notifications").fetchall()
    rows = [r["key"] for r in all_rows]
    hello_at = next((r["sent_at"] for r in all_rows if r["key"] == "hello" + suffix), None)
    if suffix:
        sent = {k[: -len(suffix)] for k in rows if k.endswith(suffix)}
    else:
        sent = {k for k in rows if "@" not in k}
    out = []
    if "hello" not in sent:
        out.append(("hello", "👋 <b>NewFiti connected.</b> You'll get each round's paper betslips "
                             "(₦500 per slip, simulated) and their results here, plus an alert if "
                             "the pipeline fails."))

    settled = [e for e in built if e["round"]["status"] == "settled" and e["slips"]]
    if settled:
        last = settled[-1]
        rkey = lambda e: f"result:{e['round']['key'][0]}-{e['round']['key'][1]}"
        key = rkey(last)
        if key not in sent:
            season = last["round"].get("season")
            totals = season_totals(built).get(season)
            # Several rounds settled since the last result message (e.g. a
            # backlog cleared after a fix): one catch-up message for the
            # missed rounds, then the full message for the latest one.
            same = [e for e in settled if e["round"].get("season") == season]
            last_sent = max((e["round"]["key"][1] for e in same if rkey(e) in sent), default=None)
            missed = [e for e in same[:-1] if rkey(e) not in sent
                      and (last_sent is None or e["round"]["key"][1] > last_sent)]
            if last_sent is not None and missed:
                out.append((f"catchup:{key[7:]}", format_catchup(missed)))
                out.extend((rkey(e), None) for e in missed)        # mark them as sent
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

    # One-time correction after dropping the two UNPLAYED slip types: the
    # current season's results so far, recalculated without them.
    # Only for chats that got the old 7-slip messages (connected before the change).
    if RECALC_KEY not in sent and hello_at and hello_at < RECALC_BEFORE and seasons:
        current = list(seasons)[-1]
        if not seasons[current]["complete"]:
            out.append((RECALC_KEY, format_recalc(current, [e for e in settled if e["round"].get("season") == current],
                                                  seasons[current])))

    # Weekly cut check (track/cuts.py), once per ISO week, to chats already
    # receiving messages.
    if WEEKLY_CUTS and "hello" in sent and seasons:
        year, week, _ = datetime.now(timezone.utc).isocalendar()
        key = f"cuts:{year}-W{week:02d}"
        if key not in sent:
            current = list(seasons)[-1]
            out.append((key, format_cut_check(current, seasons[current]["rounds"],
                                              cut_stats(built, season=current), cut_stats(built))))

    # Other leagues + merged tickets: combined messages (track/league_notify.py),
    # only for chats already receiving Premier's.
    if LEAGUE_MESSAGES and "hello" in sent:
        try:
            out.extend(league_messages(sent))
        except Exception as e:  # noqa: BLE001 -- never let them break Premier's messages
            print(f"   (other-league messages skipped: {type(e).__name__}: {e})")

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


def notify(conn, sender=send_telegram, chats=None):
    """Send every pending message; record each one only after it's sent.
    The outcome is also saved to notify_status for diagnosis."""
    result = _notify(conn, sender, chats)
    conn.execute("INSERT OR REPLACE INTO notify_status (id, at, summary) VALUES (1, ?, ?)",
                 (now_iso(), json.dumps(result)))
    conn.commit()
    return result


def _notify(conn, sender, chats):
    if sender is send_telegram:
        missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.environ.get(k)]
        if missing:
            return {"skipped": f"not set: {', '.join(missing)}"}
    if chats is None:
        chats = chat_ids() if sender is send_telegram else [None]
    built = build_slips(load_rounds(conn))
    sent, failed, failures = [], [], []
    for index, chat in enumerate(chats):
        suffix = _chat_suffix(index, chat)
        label = f"chat{index + 1}"
        for key, text in pending_messages(conn, built, suffix=suffix):
            try:
                if text is not None:
                    sender(text) if chat is None else sender(text, chat_id=chat)
            except Exception as e:  # noqa: BLE001 -- must never fail the cycle
                # Never echo the request URL: it contains the bot token. Telegram's
                # own error description (e.g. "Bad Request: chat not found") is safe.
                reason = _describe_error(e)
                failed.append(f"{label} {key}: {reason}")
                failures.append((index, label, key, reason))
                continue
            conn.execute("INSERT OR REPLACE INTO notifications (key, sent_at) VALUES (?, ?)",
                         (key + suffix, now_iso()))
            conn.commit()
            sent.append(key if len(chats) == 1 else f"{label} {key}")

    result = {"sent": sent, "failed": failed}
    owner = (lambda text: sender(text)) if chats[0] is None else (lambda text: sender(text, chat_id=chats[0]))
    since = conn.execute("SELECT sent_at FROM notifications WHERE key = ?", (FAIL_SINCE_KEY,)).fetchone()
    other_failures = [f for f in failures if f[0] != 0]
    if failures and any(index == 0 for index, *_ in failures):
        result["owner_failed"] = True
    elif other_failures:
        # Remind the owner on EVERY run while another chat is still not
        # getting its messages (requested: keep seeing it until it's fixed).
        if since is None:
            conn.execute("INSERT INTO notifications (key, sent_at) VALUES (?, ?)", (FAIL_SINCE_KEY, now_iso()))
            conn.commit()
            since = (now_iso(),)
        try:
            owner(format_delivery_alert(other_failures, since[0]))
            result["alerted_owner"] = True
        except Exception as e:  # noqa: BLE001
            result["owner_failed"] = True
            failed.append(f"owner alert: {_describe_error(e)}")
    elif since is not None:
        # Everything delivered again: say so once, then stop reminding.
        try:
            owner(f"✅ <b>Telegram delivery fixed</b>: every chat is receiving messages again "
                  f"(had been failing since {since[0][11:16]} UTC).")
            conn.execute("DELETE FROM notifications WHERE key = ?", (FAIL_SINCE_KEY,))
            conn.commit()
            result["recovered"] = True
        except Exception as e:  # noqa: BLE001
            failed.append(f"owner recovery note: {_describe_error(e)}")
    return result


FAIL_SINCE_KEY = "deliveryfail:since"


def format_delivery_alert(failures, since=None):
    by_chat = {}
    for _, label, key, reason in failures:
        by_chat.setdefault(label.replace("chat", "chat "), []).append((key, reason))
    when = f" (failing since {since[11:16]} UTC)" if since else ""
    lines = [f"⚠️ <b>Telegram: some messages couldn't be delivered</b>{when}", ""]
    for chat, items in by_chat.items():
        reasons = sorted({r for _, r in items})
        lines.append(f"<b>{html.escape(chat)}</b>: {len(items)} message(s) not delivered "
                     f"({html.escape('; '.join(reasons))})")
    lines += ["", "Retried every run -- you'll get this reminder each run (~15 min) until it's fixed. "
                  "Common causes: that person blocked or never started the bot, or their chat ID in "
                  "TELEGRAM_CHAT_ID is wrong."]
    return "\n".join(lines)
