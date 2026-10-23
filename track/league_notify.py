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

from track.betslips import played
from track.league_slips import FLAGS, LEAGUES, build_league, league_totals, merged_tickets

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

    new_blocks, new_keys, res_lines, res_keys = [], [], [], []
    for cid, b in built.items():
        flag, totals = FLAGS[cid], league_totals(b)
        for e in b:
            slips = {s["name"]: s for s in played(e["slips"]) if s["name"] in TICKETS}
            if not slips:
                continue                                    # tracking only (before the league's next season)
            r = e["round"]
            if r["status"] == "upcoming":
                key = f"lgslips:{cid}:{r['season'] or 'new'}:{r['number']}"
                if key not in sent:
                    kick = (r.get("kickoff") or "")[11:16]
                    parts = [f"{flag} <b>{html.escape(r['label'])}</b>" + (f" · kick-off {kick} UTC" if kick else "")]
                    for n in TICKETS:
                        if n in slips:
                            s, market = slips[n], n.replace(" top 5", "")
                            parts.append(_ticket_block(f"{TICKET_TITLE[market]} · top 5", s["odds"], s["stake"],
                                                       s["legs"], market))
                    new_blocks.append("\n\n".join(parts))
                    new_keys.append(key)
            elif r["status"] == "settled":
                key = f"lgres:{cid}:{r['season']}:{r['number']}"
                if key not in sent:
                    net = sum(s["returned"] - s["stake"] for s in slips.values())
                    season = totals.get(r["season"], {}).get("profit", 0.0)
                    lines = [f"{flag} <b>{html.escape(r['label'])}</b>"]
                    lines += [_result_line(TICKET_TITLE[n.replace(" top 5", "")], s["won"], s["returned"],
                                           s["legs_won"], len(s["legs"])) for n, s in slips.items()]
                    lines.append(f"Round {_naira(net, sign=True)} · season so far <b>{_naira(season, sign=True)}</b>")
                    res_lines.append("\n".join(lines))
                    res_keys.append(key)

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

    out = []
    if new_blocks:
        out.append(("lgbatch:new:" + _digest(new_keys),
                    "📥 <b>New slips — other leagues</b> (paper)\n\n" + "\n\n\n".join(new_blocks)))
        out += [(k, None) for k in new_keys]
    if res_lines:
        out.append(("lgbatch:res:" + _digest(res_keys), "📊 <b>Results — other leagues</b>\n\n" + "\n\n".join(res_lines)))
        out += [(k, None) for k in res_keys]
    return out


def _digest(keys):
    return hashlib.sha1("|".join(keys).encode()).hexdigest()[:12]
