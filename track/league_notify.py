"""Telegram for the other leagues and merged tickets: at most ONE combined
"new slips" message and ONE combined "results" message per run, however
many leagues moved on (track/notify.py sends them alongside Premier's).

Each league round / merged hour inside a combined message is recorded under
its own key too, so nothing is ever repeated and nothing is skipped if a
run is missed.
"""
import hashlib
import html
from collections import defaultdict

from track.betslips import played, summarize
from track.league_slips import FLAGS, FULL_NAMES, LEAGUES, build_league, league_totals, merged_tickets

TICKETS = ["1X2 top 5", "HT_OU0.5 top 5", "BTTS top 5"]
MERGED_ORDER = ["1X2", "HT_OU0.5", "BTTS"]


def _naira(v, sign=False):
    s = f"₦{abs(v):,.0f}"
    return (("+" if v >= 0 else "−") + s) if sign else s


def _t(name):
    return name[2:] if name.startswith("Z.") else name


PICK_TEXT = {
    "1X2": lambda a, b, sel: {"Home": f"{a} win", "Away": f"{b} win"}.get(sel, "Draw"),
    "HT_OU0.5": lambda a, b, sel: f"{sel} 0.5 (1st half)",
    "BTTS": lambda a, b, sel: "Both teams score" if sel == "Yes" else "Not both teams score",
}
TICKET_TITLE = {"1X2": "1X2", "HT_OU0.5": "HT Over/Under 0.5", "BTTS": "BTTS"}


def _ticket_block(title, odds, stake, legs, market, with_flag=False):
    """One ticket: a heading, odds/payout, then one pick per line."""
    lines = [f"<b>━━ {html.escape(title)} ━━</b>", f"Odds {odds:,.2f} → pays <b>{_naira(stake * odds)}</b>"]
    for leg in legs:
        a, b = (_t(x) for x in leg["teams"])
        flag = (FLAGS.get(leg.get("league"), "") + " ") if with_flag else ""
        lines.append(f"{flag}{html.escape(a)} v {html.escape(b)} — {html.escape(PICK_TEXT[market](a, b, leg['selection']))}")
    return "\n".join(lines)


def _result_line(title, won, returned, legs_won, n):
    return (f"✅ {html.escape(title)} — won <b>{_naira(returned)}</b>" if won
            else f"❌ {html.escape(title)} — {legs_won}/{n} picks")


def league_messages(sent, connect=None):
    """[(key, text)] for the combined messages, plus (item_key, None) entries
    marking every league round / merged hour they cover as sent."""
    from db.connection import get_connection
    with (connect or get_connection)() as conn:
        built = {cid: build_league(conn, cid) for cid in LEAGUES}
        merged = merged_tickets(conn)

    from track.notify import format_result, format_season_summary, format_upcoming   # lazy: notify imports this module
    out = []
    for cid, b in built.items():
        name, totals = FULL_NAMES[cid], league_totals(b)
        parts, keys = [], []
        for e in b:
            if not played(e["slips"]):
                continue                                    # before the league's start point: tracking only
            r = e["round"]
            if r["status"] == "settled":
                key = f"lgres:{cid}:{r['season']}:{r['number']}"
                if key not in sent:
                    parts.append(format_result(e, totals.get(r["season"]), league=name,
                                               season_name=f"{LEAGUES[cid]} season {r['season_no']}"))
                    keys.append(key)
            elif r["status"] == "upcoming":
                key = f"lgslips:{cid}:{r['season'] or 'new'}:{r['number']}"
                if key not in sent:
                    parts.append(format_upcoming(e, league=name))
                    keys.append(key)
        # End of a league season: one summary, like Premier's.
        for season, t in totals.items():
            key = f"lgseason:{cid}:{season}"
            if key in sent or not _season_over(b, season):
                continue
            season_no = next(e["round"]["season_no"] for e in b if e["round"]["season"] == season)
            parts.append(format_season_summary(season, t, summarize(b, season=season), league=name,
                                               season_name=f"Season {season_no}"))
            keys.append(key)
        if parts:                                           # one message per league, like Premier's
            out.append((f"lgmsg:{cid}:" + _digest(keys), "\n\n\n".join(parts)))
            out += [(k, None) for k in keys]

    new_blocks, new_keys, res_lines, res_keys = [], [], [], []
    by_hour = defaultdict(list)
    for t in merged:
        by_hour[t["hour"]].append(t)
    total_merged = sum(t["returned"] - t["stake"] for t in merged if t["settled"])
    for hour, ts in by_hour.items():                      # in creation order
        key_new, key_res = f"lgm:new:{hour}", f"lgm:res:{hour}"
        if key_new not in sent:
            n_leagues = len(ts[0]["legs"]) // ts[0]["k"]
            parts = [f"🔗 <b>Merged tickets · {html.escape(ts[0]['label'])}</b>\n"
                     f"{len(ts)} tickets · ₦{ts[0]['stake']:,} each · best picks from {n_leagues} leagues"]
            for t in sorted(ts, key=lambda t: (t["k"], MERGED_ORDER.index(t["market"]))):
                parts.append(_ticket_block(f"{TICKET_TITLE[t['market']]} · top {t['k']}", t["odds"], t["stake"],
                                           t["legs"], t["market"], with_flag=True))
            new_blocks.append("\n\n".join(parts))
            new_keys.append(key_new)
        if key_res not in sent and all(t["settled"] for t in ts):
            net = sum(t["returned"] - t["stake"] for t in ts)
            lines = [f"🔗 <b>Merged tickets · {html.escape(ts[0]['label'])}</b>"]
            lines += [_result_line(f"{TICKET_TITLE[t['market']]} · top {t['k']}", t["won"], t["returned"],
                                   t["legs_won"], len(t["legs"]))
                      for t in sorted(ts, key=lambda t: (t["k"], MERGED_ORDER.index(t["market"])))]
            lines.append(f"This set {_naira(net, sign=True)} · all merged so far <b>{_naira(total_merged, sign=True)}</b>")
            res_lines.append("\n".join(lines))
            res_keys.append(key_res)

    if new_blocks:
        out.append(("lgbatch:new:" + _digest(new_keys),
                    "📥 <b>New merged tickets</b> (paper)\n\n" + "\n\n\n".join(new_blocks)))
        out += [(k, None) for k in new_keys]
    if res_lines:
        out.append(("lgbatch:res:" + _digest(res_keys), "📊 <b>Merged ticket results</b>\n\n" + "\n\n".join(res_lines)))
        out += [(k, None) for k in res_keys]
    return out


def _season_over(built, season):
    """A league season is over once its last round has settled (34 or 38
    rounds, by games per round) or a later season has appeared."""
    rounds = [e["round"] for e in built if e["round"]["season"] == season]
    # Only a FINISHED later round counts: the new season's Round 1 is listed
    # while this season's last round is still being played.
    later = any(e["round"]["status"] == "settled" and e["round"]["season"] > season for e in built)
    gpr = max((len(r["games"]) for r in rounds), default=10)
    last = max((r["number"] for r in rounds if r["status"] == "settled"), default=0)
    return later or last >= (38 if gpr >= 10 else 34)


def _digest(keys):
    return hashlib.sha1("|".join(keys).encode()).hexdigest()[:12]
