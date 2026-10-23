"""Step 3: compare every Zoom league on its own odds and results.

For each league, over every finished round with captured odds (staked or
not, so the comparison doesn't wait for staking to start):
  - picks: how often the 15 ticket picks per round (1X2, HT O/U 0.5, BTTS
    top 5) won, against what the odds said -- with a z-score; |z| >= 2 is
    more than ordinary luck
  - cut: bet9ja's average margin per pick (1 - fair chance x price)
  - tickets: the 3 top-5 tickets at ₦500 -- won vs expected, and return
Plus the merged tickets. Sent on Telegram each time a Premier season ends
(track/notify.py) and shown live on the dashboard.
"""
import html
import math

from track.league_slips import FLAGS, FULL_NAMES, MERGED_LEAGUES, build_league, merged_name, merged_tickets

TICKETS = ["1X2 top 5", "HT_OU0.5 top 5", "BTTS top 5"]
MIN_ROUNDS = 10            # fewer finished rounds with odds than this: flagged as too early to judge


def compare_leagues(conn):
    rows = []
    for cid in MERGED_LEAGUES:
        built = build_league(conn, cid)
        rounds = [e for e in built if e["round"]["status"] == "settled" and e["slips"]]
        picks_n = picks_w = picks_e = picks_v = 0.0
        cut_sum = 0.0
        t_n = t_w = t_e = 0
        staked = returned = 0.0
        for e in rounds:
            for s in e["slips"]:
                if s["name"] not in TICKETS or not s["settled"]:
                    continue
                for leg in s["legs"]:
                    p = leg["odds_p"]
                    picks_n += 1
                    picks_w += bool(leg["won"])
                    picks_e += p
                    picks_v += p * (1 - p)
                    cut_sum += 1 - p * leg["price"]
                t_n += 1
                t_w += bool(s["won"])
                t_e += math.prod(leg["odds_p"] for leg in s["legs"])
                staked += 500
                returned += 500 * s["odds"] if s["won"] else 0.0
        if not picks_n:
            rows.append({"cid": cid, "rounds": 0})
            continue
        rows.append({
            "cid": cid, "rounds": len(rounds), "picks": int(picks_n),
            "pick_rate": picks_w / picks_n, "pick_expected": picks_e / picks_n,
            "z": (picks_w - picks_e) / math.sqrt(picks_v) if picks_v else 0.0,
            "cut": cut_sum / picks_n, "tickets": t_n, "tickets_won": t_w, "tickets_expected": t_e,
            "roi": (returned - staked) / staked if staked else 0.0, "profit": returned - staked,
        })
    rows.sort(key=lambda r: (r["rounds"] == 0, -r.get("roi", -9)))

    merged = {}
    for t in merged_tickets(conn):
        if not t["settled"]:
            continue
        m = merged.setdefault(merged_name(t), {"n": 0, "won": 0, "staked": 0.0, "returned": 0.0})
        m["n"] += 1
        m["won"] += bool(t["won"])
        m["staked"] += t["stake"]
        m["returned"] += t["returned"]
    return rows, merged


def _pct(x, sign=False):
    return f"{x * 100:+.1f}%" if sign else f"{x * 100:.1f}%"


def format_comparison(conn, title):
    rows, merged = compare_leagues(conn)
    lines = [f"📊 <b>{html.escape(title)}</b>",
             "Each league judged on its own odds and results (₦500 per ticket, 3 top-5 tickets a round)", ""]
    for i, r in enumerate(rows, start=1):
        name = f"{FLAGS[r['cid']]} {FULL_NAMES[r['cid']]}"
        if not r["rounds"]:
            lines.append(f"{name}: no finished rounds with odds yet")
            continue
        flag = "⚠️" if abs(r["z"]) >= 2 else "✅"
        early = " (early — few rounds)" if r["rounds"] < MIN_ROUNDS else ""
        lines.append(f"{i}. <b>{html.escape(name)}</b>{early}\n"
                     f"   Tickets: {r['tickets_won']}/{r['tickets']} won (odds expected {r['tickets_expected']:.1f}) · "
                     f"return <b>{_pct(r['roi'], sign=True)}</b>\n"
                     f"   Picks: {_pct(r['pick_rate'])} won vs {_pct(r['pick_expected'])} expected {flag} · "
                     f"cut {_pct(r['cut'])} · {r['rounds']} rounds")
    if merged:
        lines += ["", "<b>Merged tickets</b>"]
        for n, m in sorted(merged.items()):
            roi = (m["returned"] - m["staked"]) / m["staked"]
            lines.append(f"   {html.escape(n)}: {m['won']}/{m['n']} won · return {_pct(roi, sign=True)}")
    judged = [r for r in rows if r["rounds"] >= MIN_ROUNDS]
    lines.append("")
    if not judged:
        lines.append("Too early to judge any league — this gets clearer every season.")
    elif any(abs(r["z"]) >= 2 for r in judged):
        lines.append("⚠️ Some league's picks are drifting from its odds by more than luck usually explains — "
                     "worth watching if it repeats next season.")
    elif all(r["roi"] < 0 for r in judged):
        lines.append("✅ Every league's picks win about as often as its odds say, and every league's tickets lose "
                     "— no league beats bet9ja's cut so far.")
    else:
        best = max(judged, key=lambda r: r["roi"])
        lines.append(f"{FULL_NAMES[best['cid']]} is ahead so far — one good stretch isn't proof; it needs to hold "
                     f"for several seasons.")
    return "\n".join(lines)
