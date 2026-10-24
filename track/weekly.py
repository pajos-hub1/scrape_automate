"""Running profit per league (dashboard charts) and the weekly Telegram
summary (requested 2026-10-24): one message every Sunday evening with each
league's week, merged tickets since/before the chance bars, and the best
ticket of the week.
"""
import html
from datetime import datetime, timedelta, timezone

from track.betslips import build_slips, load_rounds, played
from track.league_notify import TELEGRAM_LEAGUES, merged_era
from track.league_slips import FLAGS, FULL_NAMES, LEAGUES, build_league, merged_name, merged_tickets

MERGED_SEASON = "S1"        # merged tickets as they are now (with the chance bars), counted from set R6
PREMIER_FROM_SEASON = 33     # Premier's current plan (3 top-5 tickets at ₦500) started at S33
SEND_WEEKDAY = 6             # Sunday
SEND_HOUR_UTC = 19           # 20:00 WAT
WAT = timezone(timedelta(hours=1))
SHORT = {1: "Premier", **LEAGUES}
MARKETS = {"1X2": "1X2", "HT_OU0.5": "HT O/U 0.5", "BTTS": "BTTS"}


def _round_row(e, at, cid):
    slips = [s for s in played(e["slips"]) if s.get("settled", True)]
    return {"at": at, "label": e["round"]["label"], "cid": cid, "slips": slips,
            "won": sum(bool(s["won"]) for s in slips),
            "profit": sum(s["returned"] - s["stake"] for s in slips)}


def league_rounds(conn, premier_built=None):
    """{cid: [round rows, oldest first]}: every staked, finished round with
    its profit, from when each league's money started (Premier: S33)."""
    pb = premier_built if premier_built is not None else build_slips(load_rounds(conn))
    out = {1: [_round_row(e, e["round"]["sort"], 1) for e in pb
               if e["round"]["status"] == "settled" and played(e["slips"])
               and (e["round"].get("season") or 0) >= PREMIER_FROM_SEASON]}
    for cid in LEAGUES:
        out[cid] = [_round_row(e, e["round"].get("settled_at") or "", cid) for e in build_league(conn, cid)
                    if e["round"]["status"] == "settled" and played(e["slips"])]
    return {cid: [r for r in rows if r["slips"]] for cid, rows in out.items()}


def merged_sets(merged):
    """Settled merged sets, oldest first: [{label, profit, tickets, barred}]."""
    sets = {}
    for t in merged:
        if t["settled"]:
            x = sets.setdefault(t["hour"], {"label": t["label"], "profit": 0.0, "tickets": 0, "won": 0,
                                            "barred": t["barred"], "at": t["first_kickoff"] or t["created_at"]})
            x["profit"] += t["returned"] - t["stake"]
            x["tickets"] += 1
            x["won"] += bool(t["won"])
    return list(sets.values())


def _naira(v, sign=False):
    s = f"₦{abs(v):,.0f}"
    return (("+" if v >= 0 else "−") + s) if sign else s


def _ticket_name(cid, row, slip):
    market, _, rest = slip["name"].partition(" ")
    return f"{SHORT[cid].upper()} {row['label']} · {MARKETS.get(market, market)} {rest}"


def format_weekly(conn, now=None):
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=7)).isoformat(timespec="seconds")
    rows = league_rounds(conn)
    merged = merged_tickets(conn)
    first = (now - timedelta(days=7)).astimezone(WAT)
    last = now.astimezone(WAT)
    lines = [f"📅 <b>Weekly summary</b> · {first:%d %b} – {last:%d %b}",
             "Paper tickets, ₦500 each. This week, then total so far.", ""]

    week = {cid: [r for r in rs if r["at"] >= since] for cid, rs in rows.items()}
    wk_profit = {cid: sum(r["profit"] for r in rs) for cid, rs in week.items()}
    shown = [1] + [cid for cid in LEAGUES if cid in TELEGRAM_LEAGUES]
    for cid in shown:
        n = sum(len(r["slips"]) for r in week[cid])
        won = sum(r["won"] for r in week[cid])
        total = sum(r["profit"] for r in rows[cid])
        if n:
            lines.append(f"{FLAGS[cid]} <b>{html.escape(FULL_NAMES[cid])}</b>: <b>{_naira(wk_profit[cid], sign=True)}</b> "
                         f"({won}/{n} tickets won) · total {_naira(total, sign=True)}")
        else:
            lines.append(f"{FLAGS[cid]} <b>{html.escape(FULL_NAMES[cid])}</b>: no finished rounds this week")
    others = [cid for cid in LEAGUES if cid not in TELEGRAM_LEAGUES and week[cid]]
    if others:
        lines.append("Dashboard only: " + " · ".join(f"{FLAGS[cid]} {SHORT[cid]} {_naira(wk_profit[cid], sign=True)}"
                                                    for cid in others))

    # Merged count from the first set made with the chance bars (earlier sets ignored).
    wk_merged = [t for t in merged if t["settled"] and t["barred"] and (t["first_kickoff"] or t["created_at"]) >= since]
    if wk_merged:
        x = merged_era(merged, True)
        lines.append("")
        lines.append(f"🔗 <b>MERGED · {MERGED_SEASON}</b>: <b>{_naira(sum(t['returned'] - t['stake'] for t in wk_merged), sign=True)}</b> "
                     f"({sum(bool(t['won']) for t in wk_merged)}/{len(wk_merged)} tickets won) · "
                     f"total {_naira(x['profit'], sign=True)}")

    telegram_week = sum(wk_profit[cid] for cid in shown) + sum(t["returned"] - t["stake"] for t in wk_merged)
    lines += ["", f"💰 This week, everything sent on Telegram: <b>{_naira(telegram_week, sign=True)}</b>"]

    wins = [(s["returned"], _ticket_name(cid, r, s), s["odds"]) for cid in shown for r in week[cid]
            for s in r["slips"] if s["won"]]
    wins += [(t["returned"], f"MERGED {t['label']} · {merged_name(t)}", t["odds"]) for t in wk_merged if t["won"]]
    if wins:
        ret, name, odds = max(wins)
        lines.append(f"🏆 Best ticket: {html.escape(name)} — odds {odds:,.2f}, paid {_naira(ret)}")
    else:
        lines.append("🏆 No ticket won this week.")
    played_leagues = [cid for cid in rows if week[cid]]
    if played_leagues:
        worst = min(played_leagues, key=lambda c: wk_profit[c])
        if wk_profit[worst] < 0:
            lines.append(f"📉 Worst league: {FLAGS[worst]} {SHORT[worst]} {_naira(wk_profit[worst], sign=True)}")
    return "\n".join(lines)


def weekly_key(now):
    """The key to send this week's summary under, or None if it isn't time yet
    (Sunday from 20:00 WAT; a Sunday missed entirely is skipped)."""
    if now.weekday() != SEND_WEEKDAY or now.hour < SEND_HOUR_UTC:
        return None
    year, week, _ = now.isocalendar()
    return f"weekly:{year}-W{week:02d}"
