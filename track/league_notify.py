"""Telegram for the other leagues and merged tickets: at most ONE combined
"new slips" message and ONE combined "results" message per run, however
many leagues moved on (track/notify.py sends them alongside Premier's).

Each league round / merged hour inside a combined message is recorded under
its own key too, so nothing is ever repeated and nothing is skipped if a
run is missed.
"""
import hashlib
import html
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from track.betslips import played, summarize
from track.booking import code_for, code_line, slip_extras
from track.league_slips import (FLAGS, FULL_NAMES, LEAGUES, MERGED_BARS, build_league, league_totals, merged_name,
                                merged_season_bounds, merged_tickets)

TICKETS = ["1X2 top 5", "HT_OU0.5 top 5", "BTTS top 5"]
MERGED_ORDER = ["1X2", "HT_OU0.5", "BTTS"]
# Other leagues whose slips/results go to Telegram (user's choice 2026-10-24:
# the three most profitable so far, alongside Premier). The rest are still
# played on paper and shown on the dashboard; their messages are recorded
# as sent without sending, so switching one on later doesn't replay a backlog.
TELEGRAM_LEAGUES = {3, 2}              # Bundesliga, Liga (Serie A dropped 2026-10-25: −₦4,142 by then)


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


def _ticket_block(title, odds, stake, legs, market, with_flag=False, code=None):
    """One ticket: a heading, odds/payout, booking code, then one pick per line."""
    lines = [f"<b>━━ {html.escape(title)} ━━</b>", f"Odds {odds:,.2f} → pays <b>{_naira(stake * odds)}</b>"]
    if code_line(code):
        lines.append(code_line(code))
    for leg in legs:
        a, b = (_t(x) for x in leg["teams"])
        flag = (FLAGS.get(leg.get("league"), "") + " ") if with_flag else ""
        chance = f" · {leg['odds_p'] * 100:.0f}%" if with_flag and leg.get("odds_p") else ""
        lines.append(f"{flag}{html.escape(a)} v {html.escape(b)} — {html.escape(PICK_TEXT[market](a, b, leg['selection']))}"
                     f"{chance}")
    return "\n".join(lines)


def _era_line(name, x, bold=False):
    if not x["n"]:
        return f"{name}: no set finished yet"
    money = _naira(x["profit"], sign=True)
    money = f"<b>{money}</b>" if bold else money
    return f"{name}: {money} · {x['won']}/{x['n']} tickets won"


def _result_line(title, won, returned, legs_won, n, stake):
    return (f"✅ {html.escape(title)} — won <b>{_naira(returned)}</b> ({_naira(returned - stake, sign=True)})" if won
            else f"❌ {html.escape(title)} — {legs_won}/{n} picks ({_naira(-stake, sign=True)})")


def merged_season_totals(merged, n):
    """Settled tickets of merged season n."""
    ts = [t for t in merged if t["settled"] and t.get("mseason") == n]
    return {"n": len(ts), "won": sum(bool(t["won"]) for t in ts),
            "profit": sum(t["returned"] - t["stake"] for t in ts), "staked": sum(t["stake"] for t in ts),
            "sets": len({t["hour"] for t in ts}), "tickets": ts}


def format_merged_season(merged, n):
    """End-of-season summary for merged season n (3 days of sets)."""
    x = merged_season_totals(merged, n)
    start, end = (d.astimezone(WAT) for d in merged_season_bounds(n))
    lines = [f"🏁 <b>MERGED · S{n} finished</b>",
             f"{start:%d %b %H:%M} – {end:%d %b %H:%M} WAT · {x['sets']} sets",
             f"Tickets won: <b>{x['won']}/{x['n']}</b>",
             f"Staked {_naira(x['staked'])} → profit <b>{_naira(x['profit'], sign=True)}</b>"
             + (f" ({x['profit'] / x['staked'] * 100:+.1f}%)" if x["staked"] else ""), "", "<b>By ticket</b>"]
    by = {}
    for t in x["tickets"]:
        b = by.setdefault((t["k"], MERGED_ORDER.index(t["market"])), [merged_name(t), 0, 0, 0.0])
        b[1] += 1
        b[2] += bool(t["won"])
        b[3] += t["returned"] - t["stake"]
    for _, (name, n_, won, profit) in sorted(by.items()):
        lines.append(f"{'✅' if profit > 0 else '❌'} {html.escape(name)}: {won}/{n_} won · {_naira(profit, sign=True)}")
    wins = [t for t in x["tickets"] if t["won"]]
    if wins:
        best = max(wins, key=lambda t: t["returned"])
        lines += ["", f"🏆 Best ticket: {html.escape(best['label'].split(' · ')[0].replace('Premier ', 'Merged ', 1))} · "
                      f"{html.escape(merged_name(best))} — odds {best['odds']:,.2f}, paid {_naira(best['returned'])}"]
    lines += ["", f"S{n + 1} starts from ₦0."]
    return "\n".join(lines)


def _booking(conn, built, merged, sent, part):
    """Booking codes (+ kickoff) for the messages about to be sent: league
    slips going to Telegram, and merged sets not yet sent or started."""
    extras, codes = {}, {}
    try:
        for cid, b in (built.items() if part in ("all", "leagues") else []):
            if cid not in TELEGRAM_LEAGUES:
                continue
            for e in b:
                r = e["round"]
                key = f"lgslips:{cid}:{r['season'] or 'new'}:{r['number']}"
                if r["status"] == "upcoming" and played(e["slips"]) and key not in sent:
                    extras[key] = slip_extras(conn, e)
        if part in ("all", "merged"):
            now = datetime.now(timezone.utc)
            by_hour = defaultdict(list)
            for t in merged:
                by_hour[t["hour"]].append(t)
            for hour, ts in by_hour.items():
                if f"lgm:new:{hour}" not in sent and not _kicked_off(ts, now):
                    for t in ts:
                        codes[t["key"]] = code_for(conn, t["legs"], stake=t["stake"])
    except Exception as e:  # noqa: BLE001 -- codes are a bonus; never block the messages
        print(f"   (booking codes skipped: {type(e).__name__}: {e})")
    return extras, codes


def league_messages(sent, connect=None, part="all"):
    """[(key, text)] for the combined messages, plus (item_key, None) entries
    marking every league round / merged hour they cover as sent.
    part: "all", "leagues" (per-league messages only) or "merged" (merged
    tickets only -- for the separate merged-tickets bot)."""
    from db.connection import get_connection
    with (connect or get_connection)() as conn:
        built = {cid: build_league(conn, cid) for cid in LEAGUES}
        merged = merged_tickets(conn)
        extras, merged_codes = _booking(conn, built, merged, sent, part)

    from track.notify import format_result, format_season_summary, format_upcoming   # lazy: notify imports this module
    out = []
    for cid, b in (built.items() if part in ("all", "leagues") else []):
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
                    parts.append(format_upcoming(e, league=name, extras=extras.get(key)))
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
        if parts and cid not in TELEGRAM_LEAGUES:           # dashboard only
            out += [(k, None) for k in keys]
        elif parts:                                         # one message per league, like Premier's
            out.append((f"lgmsg:{cid}:" + _digest(keys), "\n\n\n".join(parts)))
            out += [(k, None) for k in keys]

    new_blocks, new_keys, res_lines, res_keys = [], [], [], []
    by_hour = defaultdict(list)
    for t in (merged if part in ("all", "merged") else []):
        by_hour[t["hour"]].append(t)
    if part == "merged" and MERGED_HELLO not in sent and by_hour:
        # A newly connected merged-tickets bot: say hello, then only the
        # latest set onwards -- don't replay every earlier set.
        out.append((MERGED_HELLO, "👋 <b>NewFiti Merged connected.</b> Merged cross-league tickets "
                                  "(paper, ₦500 each) and their results will come here."))
        for hour in list(by_hour)[:-1]:
            out += [(f"lgm:new:{hour}", None), (f"lgm:res:{hour}", None)]
            sent = sent | {f"lgm:new:{hour}", f"lgm:res:{hour}"}
    # Running totals are per merged season (3 days, from the first set made
    # with the chance bars); earlier sets are left out of messages.
    now = datetime.now(timezone.utc)
    for hour, ts in by_hour.items():                      # in creation order
        key_new, key_res = f"lgm:new:{hour}", f"lgm:res:{hour}"
        if key_new not in sent and _kicked_off(ts, now):
            # Never send slips for games already under way (a late run, or a
            # newly connected bot catching up): record it silently instead.
            out.append((key_new, None))
            sent = sent | {key_new}
        if key_new not in sent:
            parts = [_set_header(ts)]
            for t in sorted(ts, key=lambda t: (t["k"], MERGED_ORDER.index(t["market"]))):
                bar = MERGED_BARS[t["k"]][t["market"]]
                parts.append(_ticket_block(f"{TICKET_TITLE[t['market']]} · top {t['k']} · picks {bar * 100:.0f}%+",
                                           t["odds"], t["stake"], t["legs"], t["market"], with_flag=True,
                                           code=merged_codes.get(t["key"])))
            made = {(t["market"], t["k"]) for t in ts}
            skipped = [f"{TICKET_TITLE[m]} · top {k} (none reached {MERGED_BARS[k][m] * 100:.0f}%)"
                       for k in (1, 2) for m in MERGED_ORDER if (m, k) not in made]
            if skipped:
                parts.append("<i>No ticket this time: " + html.escape("; ".join(skipped)) + "</i>")
            new_blocks.append("\n\n".join(parts))
            new_keys.append(key_new)
        if key_res not in sent and all(t["settled"] for t in ts):
            net = sum(t["returned"] - t["stake"] for t in ts)
            lines = [f"🔗 <b>Merged tickets · {html.escape(ts[0]['label'])}</b>"]
            lines += [_result_line(f"{TICKET_TITLE[t['market']]} · top {t['k']}", t["won"], t["returned"],
                                   t["legs_won"], len(t["legs"]), t["stake"])
                      for t in sorted(ts, key=lambda t: (t["k"], MERGED_ORDER.index(t["market"])))]
            lines.append(f"This set {_naira(net, sign=True)}")
            if ts[0].get("mseason"):
                n = ts[0]["mseason"]
                lines.append(_era_line(f"Total Merged S{n}", merged_season_totals(merged, n), bold=True))
            res_lines.append("\n".join(lines))
            res_keys.append(key_res)

    if new_blocks:
        out.append(("lgbatch:new:" + _digest(new_keys),
                    "📥 <b>New merged tickets</b> (paper)\n\n" + "\n\n\n".join(new_blocks)))
        out += [(k, None) for k in new_keys]
    if res_lines:
        out.append(("lgbatch:res:" + _digest(res_keys), "📊 <b>Merged ticket results</b>\n\n" + "\n\n".join(res_lines)))
        out += [(k, None) for k in res_keys]
    # A merged season over (its 3 days passed, every ticket settled): one summary.
    for n in sorted({t["mseason"] for t in merged if t.get("mseason")}) if part in ("all", "merged") else []:
        key = f"mseason:{n}"
        if key in sent or now < merged_season_bounds(n)[1]:
            continue
        if all(t["settled"] for t in merged if t.get("mseason") == n):
            out.append((key, format_merged_season(merged, n)))
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


MERGED_HELLO = "mhello"


RESULT_DELAY = timedelta(minutes=100)    # measured: bet9ja posts a round's results ~92-105 min after kickoff
SHORT_LEAGUE = {1: "Premier", 2: "Liga", 3: "Bundes", 4: "Serie A", 5: "Ligue 1", 6: "Primeira", 7: "Eredivisie"}


WAT = timezone(timedelta(hours=1), "WAT")   # Nigeria time: what the reader's Telegram shows


def _hhmm(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(WAT) if ts else None


def _set_header(ts):
    """'place by' (the set's first kickoff), every league's round and
    kickoff, and when the results should be in."""
    per_league = {}
    for t in ts:
        for l in t["legs"]:
            k = _hhmm(l.get("kickoff"))
            cur = per_league.get(l["league"])
            if cur is None or (k and (cur[1] is None or k < cur[1])):
                per_league[l["league"]] = (l.get("round"), k)
    order = sorted(per_league.items(), key=lambda kv: (kv[1][1] is None, kv[1][1] or datetime.max.replace(tzinfo=WAT)))
    kicks = [k for _, (_, k) in order if k]
    name = ts[0]["label"].split(" · ")[0]
    head = f"🔗 <b>Merged tickets · {html.escape(name)}</b>"
    if kicks:
        head += f"\n⏰ <b>Place by {min(kicks):%H:%M} WAT</b> (first kick-off)"
    rounds = " · ".join(f"{FLAGS.get(cid, '')} {SHORT_LEAGUE.get(cid, cid)}"
                        + (f" R{rno}" if rno else "") + (f" {k:%H:%M}" if k else "")
                        for cid, (rno, k) in order)
    lines = [head, rounds]
    if kicks:
        lines.append(f"🏁 Results expected about {max(kicks) + RESULT_DELAY:%H:%M} WAT")
    lines.append(f"{len(ts)} ticket{'s' if len(ts) != 1 else ''} · ₦{ts[0]['stake']:,} each · picks from {len(per_league)} leagues")
    return "\n".join(lines)


def _kicked_off(tickets, now):
    times = [t["first_kickoff"] for t in tickets if t.get("first_kickoff")]
    if not times:
        return False
    try:
        return datetime.fromisoformat(min(times).replace("Z", "+00:00")) <= now
    except ValueError:
        return False


def _digest(keys):
    return hashlib.sha1("|".join(keys).encode()).hexdigest()[:12]
