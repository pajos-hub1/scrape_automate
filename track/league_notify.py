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
from track.league_slips import (FLAGS, LEAGUES, MERGED_LEAGUES, SHORT, build_league, league_totals, merged_name,
                                merged_tickets)

TICKETS = ["1X2 top 5", "HT_OU0.5 top 5", "BTTS top 5"]


def _naira(v, sign=False):
    s = f"₦{abs(v):,.0f}"
    return (("+" if v >= 0 else "−") + s) if sign else s


def _t(name):
    return name[2:] if name.startswith("Z.") else name


def _pick(leg, market):
    a, b = (_t(x) for x in leg["teams"])
    sel = leg["selection"]
    if market == "1X2":
        return {"Home": f"{a} (H)", "Away": f"{b} (A)"}.get(sel, f"{a}–{b} (X)")
    return f"{a}–{b} {sel}"


def _ticket_line(name, odds, stake, legs, market, with_flag=False):
    picks = " · ".join((FLAGS.get(l.get("league"), "") + " " if with_flag else "") + _pick(l, market) for l in legs)
    return f"<b>{html.escape(name)}</b> @{odds:,.2f} → {_naira(stake * odds)}: {html.escape(picks)}"


def _result_bit(name, won, returned, stake, legs_won, n):
    return f"{name} ✅ {_naira(returned - stake, sign=True)}" if won else f"{name} ❌ {legs_won}/{n}"


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
                    lines = [f"{flag} <b>{html.escape(r['label'])}</b>" + (f" · kick-off {kick} UTC" if kick else "")]
                    for n in TICKETS:
                        if n in slips:
                            s = slips[n]
                            lines.append(_ticket_line(SHORT[n.replace(' top 5', '')], s["odds"], s["stake"], s["legs"],
                                                      n.replace(" top 5", "")))
                    new_blocks.append("\n".join(lines))
                    new_keys.append(key)
            elif r["status"] == "settled":
                key = f"lgres:{cid}:{r['season']}:{r['number']}"
                if key not in sent:
                    net = sum(s["returned"] - s["stake"] for s in slips.values())
                    bits = [_result_bit(SHORT[n.replace(' top 5', '')], s["won"], s["returned"], s["stake"],
                                        s["legs_won"], len(s["legs"])) for n, s in slips.items()]
                    season = totals.get(r["season"], {}).get("profit", 0.0)
                    res_lines.append(f"{flag} <b>{html.escape(r['label'])}</b>: {' · '.join(bits)} → "
                                     f"<b>{_naira(net, sign=True)}</b> · season {_naira(season, sign=True)}")
                    res_keys.append(key)

    by_hour = defaultdict(list)
    for t in merged:
        by_hour[t["hour"]].append(t)
    total_merged = sum(t["returned"] - t["stake"] for t in merged if t["settled"])
    for hour, ts in sorted(by_hour.items()):
        key_new, key_res = f"lgm:new:{hour}", f"lgm:res:{hour}"
        if key_new not in sent:
            lines = [f"🔗 <b>Merged tickets · {hour[11:13]}:00 UTC</b> (best picks from {len(ts[0]['legs']) // ts[0]['k']} leagues)"]
            lines += [_ticket_line(merged_name(t), t["odds"], t["stake"], t["legs"], t["market"], with_flag=True)
                      for t in ts]
            new_blocks.append("\n".join(lines))
            new_keys.append(key_new)
        if key_res not in sent and all(t["settled"] for t in ts):
            net = sum(t["returned"] - t["stake"] for t in ts)
            bits = [_result_bit(merged_name(t).replace(" merged", ""), t["won"], t["returned"], t["stake"],
                                t["legs_won"], len(t["legs"])) for t in ts]
            res_lines.append(f"🔗 <b>Merged {hour[11:13]}:00</b>: {' · '.join(bits)} → <b>{_naira(net, sign=True)}</b>"
                             f" · all merged so far {_naira(total_merged, sign=True)}")
            res_keys.append(key_res)

    out = []
    if new_blocks:
        out.append(("lgbatch:new:" + _digest(new_keys),
                    "📥 <b>New slips — other leagues</b> (₦500 per ticket, paper)\n\n" + "\n\n".join(new_blocks)))
        out += [(k, None) for k in new_keys]
    if res_lines:
        out.append(("lgbatch:res:" + _digest(res_keys), "📊 <b>Results — other leagues</b>\n\n" + "\n".join(res_lines)))
        out += [(k, None) for k in res_keys]
    return out


def _digest(keys):
    return hashlib.sha1("|".join(keys).encode()).hexdigest()[:12]
