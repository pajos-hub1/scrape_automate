"""Generates the static dashboard HTML (docs/index.html) from the current
DB state. Re-run every cycle (see run.py cmd_dashboard / cmd_cycle) so the
GitHub Pages copy always reflects the latest scrape/predict/track pass.
"""
import html
from pathlib import Path

from config import BASE_DIR, DASHBOARD_MODELS, DASHBOARD_SEASONS, ROUNDS_PER_SEASON
from dashboard.charts import accuracy_trend_chart, cumulative_profit_chart, fmt_naira, market_accuracy_chart
from dashboard.data import (get_accuracy_trend, get_meta, get_models_and_stats, get_profit_trend,
                            get_recent_season_ids, get_round_history)
from dashboard.health import get_health
from track.betslips import (STAKE_NAIRA, build_slips, load_rounds, played, played_kinds, round_stake,
                            season_totals, stake_for, summarize)
from track.cuts import cut_stats, verdict as cut_verdict
from track.report import PROB_MARKETS, accuracy_stats, probability_stats, profit_stats, roi, verdict

OUT_PATH = BASE_DIR / "docs" / "index.html"

MARKET_ORDER = ["1X2", "BTTS", "OU2.5", "OU1.5", "CorrectScore", "HT_1X2", "HT_OU1.5", "HT_OU0.5"]
MODEL_SHORT = {"baseline_v0": "Base", "ml_v1": "ML", "blend_v1": "Bln"}


def _model_tag(model_version):
    return MODEL_SHORT.get(model_version, model_version)


def _stat_tile(label, value, sub=None):
    sub_html = f'<div class="tile-sub">{html.escape(sub)}</div>' if sub else ""
    return (f'<div class="tile"><div class="tile-value">{html.escape(str(value))}</div>'
            f'<div class="tile-label">{html.escape(label)}</div>{sub_html}</div>')


def _render_meta_section(meta):
    if meta["current_round"] is not None:
        round_str, round_sub = f"{meta['current_round']} / {ROUNDS_PER_SEASON}", None
    elif meta.get("pending_round"):
        round_str = "New season starting"
        round_sub = f"Round {meta['pending_round']} predicted, not yet confirmed"
    else:
        round_str, round_sub = "n/a", None

    tiles = [
        _stat_tile("Current season round", round_str, round_sub),
        _stat_tile("Seasons preserved", meta["seasons_tracked"], f"{meta['seasons_archived']} archived"),
        _stat_tile("Last scraped", (meta["last_scraped"] or "n/a").replace("T", " ").split("+")[0] + " UTC"),
    ]
    return f'<div class="tiles">{"".join(tiles)}</div>'


def _fmt_ts(ts):
    return ts.strftime("%m-%d %H:%M UTC") if ts else "never"


def _render_health_section(health):
    """Top-of-page status: is the pipeline actually running and saving?"""
    tiles = []
    runs = health["runs"]
    if runs is not None:
        status = "OK" if runs["total"] and runs["failed"] == 0 else (
            "Degraded" if runs["total"] and runs["failed"] < runs["total"] else "Failing")
        tiles.append(_stat_tile("Pipeline (last 24h)", status,
                                f"{runs['total'] - runs['failed']} ok / {runs['failed']} failed runs"))
        tiles.append(_stat_tile("Last successful run", _fmt_ts(runs["last_success"])))
        tiles.append(_stat_tile("Last failed run", _fmt_ts(runs["last_failure"])))
    tg = health.get("telegram")
    if tg:
        if "skipped" in tg:
            value, sub = "Off", tg["skipped"]
        elif tg.get("failed"):
            value = "Failing" if tg.get("owner_failed") else "Partly failing"
            sub = f'{len(tg["failed"])} undelivered, retrying: ' + "; ".join(tg["failed"])[:140]
        else:
            value, sub = "OK", f'all delivered · last check {tg["at"][11:16]} UTC'
        tiles.append(_stat_tile("Telegram", value, sub))
    missed = health["missed_rounds"]
    tiles.append(_stat_tile("Rounds missed (24h)", len(missed),
                            ", ".join(missed[:6]) if missed else "every round got a prediction"))
    return f'<div class="tiles">{"".join(tiles)}</div>'


def _render_probability_section(conn, models):
    """Log-loss per market, per model, next to the de-margined odds' own
    log-loss on the same predictions (see track/report.probability_stats)."""
    if not models:
        return '<p class="empty">No reconciled predictions yet.</p>'
    per_model = {m: probability_stats(conn, m) for m in models}
    header = "".join(f"<th>{html.escape(_model_tag(m))}</th><th>Odds</th>" for m in models)
    rows = []
    for market in PROB_MARKETS:
        cells = ""
        for m in models:
            s = per_model[m].get(market)
            if not s or not s["n"]:
                cells += "<td>n/a</td><td>n/a</td>"
                continue
            model_ll, odds_ll = s["model_ll"] / s["n"], s["odds_ll"] / s["n"]
            cls = "hit" if model_ll < odds_ll else "miss" if model_ll > odds_ll + 0.0005 else ""
            cells += f'<td class="{cls}">{model_ll:.4f}</td><td>{odds_ll:.4f}</td>'
        rows.append(f"<tr><td>{html.escape(MARKET_LABELS[market])}</td>{cells}</tr>")
    return (f'<div class="table-scroll"><table><thead><tr><th>Market</th>{header}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


SLIP_BROWSER_ROUNDS = 30

SLIP_MARKET_LABELS = {"1X2": "1X2", "OU1.5": "O/U 1.5", "HT_OU0.5": "HT O/U 0.5", "BTTS": "BTTS",
                      "HT_OU1.5": "HT O/U 1.5", "OU2.5": "O/U 2.5"}


def _naira(v):
    return f"₦{v:,.0f}"


def _render_slip(slip):
    rows = []
    for leg in slip["legs"]:
        if leg["won"] is None:
            res, cls = "pending", ""
        else:
            res, cls = ("&#10003;", "hit") if leg["won"] else ("&#10007;", "miss")
        rows.append(
            f'<tr><td class="teams">{html.escape(leg["teams"][0])} <span class="vs">vs</span> '
            f'{html.escape(leg["teams"][1])}</td><td>{SLIP_MARKET_LABELS[leg["market"]]}</td>'
            f'<td>{html.escape(leg["selection"])}</td><td>{leg["price"]:.2f}</td>'
            f'<td>{leg["score"] * 100:.0f}%</td><td class="{cls}">{res}</td></tr>'
        )
    if slip["won"] is None:
        outcome = f'pending &middot; pays {_naira(slip["stake"] * slip["odds"])} if all {len(slip["legs"])} win'
    elif slip["won"]:
        outcome = f'<span class="hit">WON {_naira(slip["returned"])}</span>'
    else:
        outcome = (f'<span class="miss">LOST {_naira(slip["stake"])}</span> '
                   f'({slip["legs_won"]}/{len(slip["legs"])} picks won)')
    name = slip["name"].replace(" top ", " &middot; top ")
    for code, label in SLIP_MARKET_LABELS.items():
        if name.startswith(code + " "):
            name = label + name[len(code):]
    if not slip.get("played", True):
        name += ' <span class="conf">&middot; TRACKING ONLY, not staked or counted</span>'
    return (f'<div class="slip"><div class="slip-head"><strong>{name}</strong> '
            f'<span class="conf">odds {slip["odds"]:,.2f} &middot; stake {_naira(slip["stake"])} &middot; </span>'
            f'{outcome}</div><div class="table-scroll"><table><thead><tr><th>Game</th><th>Market</th>'
            f'<th>Pick</th><th>Odds</th><th>Chance</th><th></th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></div>')


def _render_betslips_section(built):
    """Paper accumulators -- see track/betslips.py."""
    if not built:
        return '<p class="empty">No rounds yet.</p>'

    # Money is tracked per season, each starting from ₦0.
    seasons = season_totals(built)
    # Current = the season played most recently (ids aren't chronological --
    # see season_totals), or a brand-new season with no results yet.
    upcoming_season = next((e["round"].get("season") for e in reversed(built)
                            if e["round"]["status"] == "upcoming"), None)
    current = next(reversed(seasons), None)
    if upcoming_season is not None and upcoming_season not in seasons:
        current = upcoming_season

    summary = summarize(built, season=current, include_unplayed=True)
    order = [k for k in summary if k != "All-games slip"] + (["All-games slip"] if "All-games slip" in summary else [])
    rows, tot = [], {"slips": 0, "won": 0, "staked": 0.0, "returned": 0.0}
    for kind in order:
        t = summary[kind]
        staked_kinds = played_kinds(current)
        if kind in staked_kinds:                 # other types: shown as tracking only, never counted
            for k in tot:
                tot[k] += t[k]
        label = kind
        for code, lab in SLIP_MARKET_LABELS.items():
            if kind.startswith(code + " "):
                label = lab + kind[len(code):]
        if kind not in staked_kinds:
            label += " (tracking only, not staked)"
        rows.append((label, t))
    rows.append(("Total", dict(tot, profit=tot["returned"] - tot["staked"], biggest_win=None)))
    def _type_row(label, t):
        if "(tracking only" in label:            # dropped types: what they WOULD have done
            money = (f'<td>&mdash;</td><td>&mdash;</td>'
                     f'<td class="conf">would be {fmt_naira(t["profit"])}</td>')
        else:
            money = (f'<td>{_naira(t["staked"])}</td><td>{_naira(t["returned"])}</td>'
                     f'<td class="{"hit" if t["profit"] > 0 else "miss"}">{fmt_naira(t["profit"])}</td>')
        return (f'<tr><td>{"<strong>Total</strong>" if label == "Total" else html.escape(label)}</td>'
                f'<td>{t["slips"]}</td><td>{t["won"]} ({t["won"] / t["slips"] * 100:.1f}%)</td>{money}'
                f'<td>{_naira(t["biggest_win"]) if t.get("biggest_win") else "&mdash;"}</td></tr>')

    body = "".join(_type_row(label, t) for label, t in rows if t["slips"])
    if not tot["slips"]:
        table = (f'<p class="empty">Season {current} has just started: ₦0 so far. Its first results '
                 f'appear here once round 1 finishes.</p>')
    else:
        table = ('<div class="table-scroll"><table><thead><tr><th>Slip</th><th>Slips</th><th>Won</th>'
                 '<th>Staked</th><th>Returned</th><th>Profit</th><th>Biggest win</th></tr></thead>'
                 f'<tbody>{body}</tbody></table></div>')

    season_rows = []
    for season in reversed(seasons):           # most recently played first
        t = seasons[season]
        status = "finished" if t["complete"] else f"in progress (round {t['last_round']})"
        season_rows.append(
            f'<tr><td>Season {season}</td><td>{status}</td><td>{t["rounds"]}</td>'
            f'<td>{t["won"]}/{t["slips"]}</td><td>{_naira(t["staked"])}</td><td>{_naira(t["returned"])}</td>'
            f'<td class="{"hit" if t["profit"] > 0 else "miss"}">{fmt_naira(t["profit"])}</td></tr>')
    if seasons:
        all_staked = sum(t["staked"] for t in seasons.values())
        all_ret = sum(t["returned"] for t in seasons.values())
        won_seasons = sum(1 for t in seasons.values() if t["complete"] and t["profit"] > 0)
        done = sum(1 for t in seasons.values() if t["complete"])
        season_rows.append(
            f'<tr class="round-total"><td><strong>All seasons</strong></td>'
            f'<td>{won_seasons}/{done} finished seasons in profit</td>'
            f'<td>{sum(t["rounds"] for t in seasons.values())}</td>'
            f'<td>{sum(t["won"] for t in seasons.values())}/{sum(t["slips"] for t in seasons.values())}</td>'
            f'<td>{_naira(all_staked)}</td><td>{_naira(all_ret)}</td>'
            f'<td class="{"hit" if all_ret > all_staked else "miss"}">{fmt_naira(all_ret - all_staked)}</td></tr>')
    season_table = ('<div class="table-scroll"><table><thead><tr><th>Season</th><th>Status</th><th>Rounds</th>'
                    '<th>Slips won</th><th>Staked</th><th>Returned</th><th>Profit</th></tr></thead>'
                    f'<tbody>{"".join(season_rows)}</tbody></table></div>')

    trend, running = [], 0.0
    for entry in built:
        if entry["round"].get("season") != current:
            continue
        settled = [s for s in played(entry["slips"]) if s["settled"]]
        if not settled:
            continue
        profit = sum(s["returned"] - s["stake"] for s in settled)
        running += profit
        trend.append({"index": len(trend), "label": entry["round"]["label"], "bets": len(settled),
                      "profit": profit, "cumulative": running})
    chart = cumulative_profit_chart(trend, fmt=fmt_naira, unit="", bet_word="slips")

    # Slip browser: the last SLIP_BROWSER_ROUNDS settled rounds + the
    # upcoming one (default view), each with every slip, stepped with </>.
    shown = [e for e in built if e["slips"] and e["round"]["status"] == "settled"][-SLIP_BROWSER_ROUNDS:]
    shown += [e for e in built if e["slips"] and e["round"]["status"] == "playing"]
    shown += [e for e in built if e["slips"] and e["round"]["status"] == "upcoming"]
    panels = []
    for i, e in enumerate(shown):
        if e["round"]["status"] in ("upcoming", "playing"):
            staked_slips = played(e["slips"])
            stake = sum(s["stake"] for s in staked_slips)
            state = "in play, results pending" if e["round"]["status"] == "playing" else "upcoming"
            label = f'{e["round"]["label"]} ({state}) · {len(staked_slips)} slips, {_naira(stake)} staked'
        else:
            staked_slips = played(e["slips"])
            result = sum(s["returned"] - s["stake"] for s in staked_slips)
            won = sum(1 for s in staked_slips if s["won"])
            label = (f'{e["round"]["label"]} · {won}/{len(staked_slips)} slips won · '
                     f'round result {fmt_naira(result)}')
        active = " active" if i == len(shown) - 1 else ""
        panels.append(f'<div class="slip-panel{active}" data-label="{html.escape(label)}">'
                      + "".join(_render_slip(s) for s in e["slips"]) + "</div>")
    nav = ""
    if panels:
        nav = ('<div class="round-nav">'
               '<button id="slip-prev" type="button" aria-label="Previous round">&larr;</button>'
               f'<span id="slip-nav-label" class="section-note"></span>'
               '<button id="slip-next" type="button" aria-label="Next round">&rarr;</button></div>')
    return "".join([
        f"<h3>Season {current}: by slip type (started from ₦0)</h3>", table,
        f"<h3>Season {current}: running total, round by round</h3>", chart,
        "<h3>Season by season (each starts from ₦0)</h3>", season_table,
        f"<h3>Every slip, round by round (last {SLIP_BROWSER_ROUNDS} rounds + this round)</h3>",
        nav, *panels,
    ])


def _render_market_tiles(stats):
    """One accuracy tile per market (still blended across models -- that
    part's fine, model comparison lives in its own section below) instead
    of a single 'Overall' number. A blended-across-MARKETS number is
    actively misleading: CorrectScore's ~13% ceiling (it's an exact-match
    36-way pick) drags a combined figure down next to 1X2's ~50%, so the
    single tile was never really answering "is this working," just
    averaging two different scales together."""
    tiles = []
    for market in MARKET_ORDER:
        s = stats.get(market)
        if not s or not s["n"]:
            tiles.append(_stat_tile(MARKET_LABELS[market], "n/a", "no reconciled predictions yet"))
            continue
        acc = s["model_correct"] / s["n"]
        tiles.append(_stat_tile(MARKET_LABELS[market], f"{acc * 100:.1f}%",
                                 f"{s['model_correct']}/{s['n']} predictions"))
    return "".join(tiles)


MARKET_LABELS = {
    "1X2": "1X2", "BTTS": "BTTS", "OU2.5": "O/U 2.5", "OU1.5": "O/U 1.5",
    "CorrectScore": "Correct Score", "HT_1X2": "HT 1X2", "HT_OU1.5": "HT O/U 1.5", "HT_OU0.5": "HT O/U 0.5",
}


PENDING_NOTES = {
    "upcoming": "Upcoming -- best-guess round numbering, see note below",
    "pending": ("Pending -- superseded by a newer batch before this round was confirmed played; "
                "outcome not yet known, will fill in once reconciled"),
}

NO_PREDICTION_NOTE = (
    "No prediction was ever made for this round -- its fixture preview window was missed entirely "
    "(the fixtures page only ever shows the single current upcoming round, with no archive, so a "
    "scheduling gap long enough can skip a round's preview before any cycle catches it). The result "
    "below is real; there's just nothing to compare it against."
)


def _render_round_panel(entry, index, total):
    """One round's table -- an unreconciled batch (upcoming or pending) or
    a reconciled round's predicted-vs-actual detail -- wrapped in a hidden-
    by-default div the round-browser JS toggles visibility on."""
    header_cells = "".join(f"<th>{MARKET_LABELS[m]}</th>" for m in MARKET_ORDER)
    status = entry.get("status", "reconciled")

    if status in ("upcoming", "pending"):
        rows = []
        for fx in entry["fixtures"]:
            cells = []
            for m in MARKET_ORDER:
                by_model = fx["markets"].get(m)
                if not by_model:
                    cells.append("<td>&mdash;</td>")
                    continue
                lines = [
                    f'<div class="model-pred">'
                    f'{html.escape(e["label"])} <span class="conf">{e["confidence"] * 100:.0f}%</span></div>'
                    for mv, e in sorted(by_model.items())
                ]
                cells.append(f'<td>{"".join(lines)}</td>')
            rows.append(
                f'<tr><td class="teams">{html.escape(fx["team_a"])} <span class="vs">vs</span> '
                f'{html.escape(fx["team_b"])}</td>{"".join(cells)}</tr>'
            )
        body = (
            f'<p class="section-note">{PENDING_NOTES[status]}</p>'
            f'<div class="table-scroll"><table><thead><tr><th>Fixture</th>{header_cells}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
        )
        label = f'Round {entry["round_number"]} ({status})'
    elif status == "no_prediction":
        rows = []
        for m in entry["matches"]:
            score = f'{m["ft_a"]}-{m["ft_b"]}' + (f' (HT {m["ht_a"]}-{m["ht_b"]})' if m["ht_a"] is not None else "")
            rows.append(
                f'<tr><td class="teams">{html.escape(m["team_a"])} <span class="vs">vs</span> '
                f'{html.escape(m["team_b"])}</td><td class="score">{score}</td></tr>'
            )
        body = (
            f'<p class="section-note">{NO_PREDICTION_NOTE}</p>'
            f'<div class="table-scroll"><table><thead><tr><th>Fixture</th><th>Result</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
        )
        label = f'Round {entry["round_number"]} (no prediction)'
    else:
        rows = []
        market_profit = {}
        for m in entry["matches"]:
            cells = []
            for market in MARKET_ORDER:
                by_model = m["markets"].get(market)
                if not by_model:
                    cells.append("<td>&mdash;</td>")
                    continue
                actual = next(iter(by_model.values()))["actual"]
                lines = [f'<div class="model-pred-actual">actual: {html.escape(actual)}</div>']
                for mv, e in sorted(by_model.items()):
                    mark = "&#10003;" if e["correct"] else "&#10007;"
                    cls = "hit" if e["correct"] else "miss"
                    money = ""
                    if e["profit"] is not None:
                        money = f' <span class="conf">@{e["price"]:.2f} {e["profit"]:+.2f}</span>'
                        market_profit[market] = market_profit.get(market, 0.0) + e["profit"]
                    lines.append(f'<div class="model-pred {cls}">{mark} {html.escape(e["predicted"])}{money}</div>')
                cells.append(f'<td>{"".join(lines)}</td>')
            score = f'{m["ft_a"]}-{m["ft_b"]}' + (f' (HT {m["ht_a"]}-{m["ht_b"]})' if m["ht_a"] is not None else "")
            rows.append(
                f'<tr><td class="teams">{html.escape(m["team_a"])} <span class="vs">vs</span> '
                f'{html.escape(m["team_b"])} <span class="score">{score}</span></td>{"".join(cells)}</tr>'
            )
        total_cells = "".join(
            f'<td class="{"hit" if market_profit[mk] > 0 else "miss"}">{market_profit[mk]:+.2f}</td>'
            if mk in market_profit else "<td>&mdash;</td>"
            for mk in MARKET_ORDER
        )
        round_total = sum(market_profit.values())
        rows.append(f'<tr class="round-total"><td><strong>Round profit</strong> '
                    f'<span class="conf">{round_total:+.2f} units</span></td>{total_cells}</tr>')
        body = (
            '<p class="section-note">Each pick shows its closing odds and its result on a 1-unit '
            'stake (win: odds &minus; 1, loss: &minus;1). Bottom row: this round\'s total per market.</p>'
            f'<div class="table-scroll"><table><thead><tr><th>Result</th>{header_cells}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
        )
        label = f'Round {entry["round_number"]} (reconciled, {round_total:+.2f} units)'

    active_cls = " active" if index == total - 1 else ""
    return f'<div class="round-panel{active_cls}" data-label="{html.escape(label)}">{body}</div>'


def _render_round_browser(history):
    if not history:
        return '<p class="empty">No predictions yet -- run `python run.py predict` after the next scrape.</p>'

    panels = "".join(_render_round_panel(entry, i, len(history)) for i, entry in enumerate(history))
    last = history[-1]
    default_label = f'Round {last["round_number"]} ({last.get("status", "reconciled")})'

    nav = (
        '<div class="round-nav">'
        '<button id="round-prev" type="button" aria-label="Previous round">&larr;</button>'
        f'<span id="round-nav-label" class="section-note">{html.escape(default_label)}</span>'
        '<button id="round-next" type="button" aria-label="Next round">&rarr;</button>'
        '</div>'
    )
    return nav + panels


def _render_verdict_summary(stats):
    lines = []
    for market in MARKET_ORDER:
        s = stats.get(market)
        if not s or not s["n"]:
            continue
        model_acc = s["model_correct"] / s["n"]
        baseline_acc = s["baseline_correct"] / s["baseline_n"] if s["baseline_n"] else None
        odds_acc = s["odds_correct"] / s["odds_n"] if s["odds_n"] else None
        vs = [v for v in [verdict(model_acc, baseline_acc, "baseline"), verdict(model_acc, odds_acc, "odds")] if v]
        if vs:
            lines.append(f'<li><strong>{MARKET_LABELS[market]}</strong>: {", ".join(vs)}</li>')
    return f'<ul class="verdicts">{"".join(lines)}</ul>' if lines else ""


def _render_market_trend_sections(conn, season_ids):
    """One trend line per market instead of a single all-markets-blended
    line -- same reasoning as _render_market_tiles: CorrectScore sitting at
    ~13% would flatten every other market's real movement into noise on a
    shared 0-100% line blended together with them."""
    parts = []
    for market in MARKET_ORDER:
        trend = get_accuracy_trend(conn, model_version=DASHBOARD_MODELS, market=market, season_ids=season_ids)
        parts.append(f'<h3>{html.escape(MARKET_LABELS[market])}</h3>')
        parts.append(accuracy_trend_chart(trend))
    return "".join(parts)


def _render_model_accuracy_sections(models_and_stats):
    """One chart + verdict list per live model, instead of blending them
    together into one misleading 'model' column."""
    if not models_and_stats:
        return '<p class="empty">No reconciled predictions yet.</p>'

    parts = []
    for model_version in sorted(models_and_stats):
        stats = models_and_stats[model_version]
        parts.append(f'<h3>{html.escape(model_version)}</h3>')
        if not stats:
            parts.append('<p class="empty">No reconciled predictions yet for this model.</p>')
            continue
        parts.append(market_accuracy_chart(stats))
        parts.append(_render_verdict_summary(stats))
    return "".join(parts)


def _render_profit_section(conn, models):
    """Flat-stake ROI per market, one column per live model plus the
    always-back-the-favourite reference. Accuracy alone can't show whether
    picks would actually make money -- this does (see track/report.py's
    profit_stats docstring)."""
    if not models:
        return '<p class="empty">No reconciled predictions yet.</p>'

    per_model = {m: profit_stats(conn, m) for m in models}
    # Favourite reference: the same for every model up to which fixtures
    # each one covered -- use the model with the most priced bets.
    ref = max(models, key=lambda m: sum(s["fav_bets"] for s in per_model[m].values()))

    def _cell(returns, bets):
        r = roi(returns, bets)
        if r is None:
            return '<td>n/a</td>'
        cls = "hit" if r > 0 else "miss"
        return f'<td class="{cls}">{r * 100:+.1f}%</td>'

    header = "".join(f"<th>{html.escape(_model_tag(m))}</th>" for m in models) + "<th>Odds fav</th>"
    rows = []
    for market in MARKET_ORDER:
        if not any(market in per_model[m] for m in models):
            continue
        cells = "".join(
            _cell(per_model[m][market]["returns"], per_model[m][market]["bets"])
            if market in per_model[m] else "<td>n/a</td>"
            for m in models
        )
        fav = per_model[ref].get(market)
        cells += _cell(fav["fav_returns"], fav["fav_bets"]) if fav else "<td>n/a</td>"
        rows.append(f"<tr><td>{html.escape(MARKET_LABELS[market])}</td>{cells}</tr>")

    return (f'<div class="table-scroll"><table><thead><tr><th>Market</th>{header}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zoom Prediction Dashboard</title>
<style>
:root {{
  color-scheme: light;
  --surface-1: #fcfcfb; --page: #f9f9f7;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #898781;
  --gridline: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --series-2: #008300; --series-3: #e87ba4;
  --good: #0ca30c; --bad: #d03b3b;
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) {{
    color-scheme: dark;
    --surface-1: #1a1a19; --page: #0d0d0d;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
    --gridline: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --series-1: #3987e5; --series-2: #008300; --series-3: #d55181;
    --good: #0ca30c; --bad: #e66767;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --surface-1: #1a1a19; --page: #0d0d0d;
  --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
  --gridline: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --series-1: #3987e5; --series-2: #008300; --series-3: #d55181;
  --good: #0ca30c; --bad: #e66767;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; background: var(--page); color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  padding: 24px 16px 64px;
}}
.wrap {{ max-width: 980px; margin: 0 auto; }}
header {{ display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 8px; margin-bottom: 20px; }}
h1 {{ font-size: 1.4rem; margin: 0; }}
.meta {{ color: var(--text-secondary); font-size: 0.85rem; }}
#theme-toggle {{
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 6px;
  color: var(--text-primary); cursor: pointer; padding: 6px 10px; font-size: 0.85rem;
}}
section {{
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
  padding: 18px 20px; margin-bottom: 20px;
}}
section h2 {{ font-size: 1.05rem; margin: 0 0 12px; }}
.section-note {{ color: var(--text-secondary); font-size: 0.85rem; margin: 0 0 10px; }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 20px; }}
.tile {{ background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }}
.tile-value {{ font-size: 1.6rem; font-weight: 600; font-variant-numeric: tabular-nums; }}
.tile-label {{ color: var(--text-secondary); font-size: 0.82rem; margin-top: 2px; }}
.tile-sub {{ color: var(--text-muted); font-size: 0.75rem; margin-top: 4px; }}
.table-scroll {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; }}
th, td {{ padding: 7px 10px; text-align: left; white-space: nowrap; border-bottom: 1px solid var(--gridline); }}
th {{ color: var(--text-secondary); font-weight: 600; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.02em; }}
td.teams {{ font-weight: 500; }}
.vs {{ color: var(--text-muted); font-weight: 400; }}
.score {{ color: var(--text-secondary); font-weight: 400; margin-left: 6px; }}
.conf {{ color: var(--text-muted); font-size: 0.78rem; }}
td.hit {{ color: var(--good); }}
td.miss {{ color: var(--bad); }}
.model-pred {{ margin-bottom: 2px; }}
tr.round-total td {{ border-top: 2px solid var(--axis); font-variant-numeric: tabular-nums; }}
.slip {{ border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; margin: 10px 0; background: var(--page); }}
.slip-head {{ margin-bottom: 6px; font-size: 0.9rem; }}
.model-pred.hit {{ color: var(--good); }}
.model-pred.miss {{ color: var(--bad); }}
.slip-head .hit {{ color: var(--good); font-weight: 600; }}
.slip-head .miss {{ color: var(--bad); font-weight: 600; }}
.model-pred-actual {{ color: var(--text-secondary); font-size: 0.78rem; margin-bottom: 3px; }}
.model-tag {{ display: inline-block; min-width: 32px; color: var(--text-muted); font-size: 0.72rem;
  font-weight: 600; text-transform: uppercase; }}
.round-nav {{ display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }}
.round-nav button {{
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 6px;
  color: var(--text-primary); cursor: pointer; padding: 4px 12px; font-size: 0.95rem; line-height: 1.6;
}}
.round-nav button:disabled {{ opacity: 0.35; cursor: default; }}
.round-nav button:not(:disabled):hover {{ background: var(--page); }}
#round-nav-label {{ margin: 0; font-weight: 500; }}
.round-panel {{ display: none; }}
.round-panel.active {{ display: block; }}
#slip-nav-label {{ margin: 0; font-weight: 500; }}
.slip-panel {{ display: none; }}
.slip-panel.active {{ display: block; }}
section h3 {{ font-size: 0.9rem; margin: 16px 0 8px; color: var(--text-secondary); }}
section h3:first-of-type {{ margin-top: 0; }}
.chart {{ width: 100%; height: auto; }}
.gridline {{ stroke: var(--gridline); stroke-width: 1; }}
.axis-line {{ stroke: var(--axis); stroke-width: 1; }}
.axis-label {{ fill: var(--text-muted); font-size: 10px; }}
.bar-label {{ fill: var(--text-primary); font-size: 10px; font-variant-numeric: tabular-nums; }}
.legend {{ display: flex; gap: 16px; margin-top: 8px; font-size: 0.8rem; color: var(--text-secondary); }}
.legend-item {{ display: inline-flex; align-items: center; gap: 6px; }}
.swatch {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; }}
.empty {{ color: var(--text-muted); font-size: 0.9rem; }}
.verdicts {{ margin: 12px 0 0; padding-left: 20px; font-size: 0.85rem; color: var(--text-secondary); }}
.verdicts li {{ margin-bottom: 4px; }}
footer {{ color: var(--text-muted); font-size: 0.78rem; line-height: 1.6; }}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>Zoom Prediction Dashboard</h1>
  <div style="display:flex; align-items:center; gap:12px;">
    <span class="meta">Generated {generated_at} UTC</span>
    <button id="theme-toggle" aria-label="Toggle dark mode">&#9680;</button>
  </div>
</header>

{meta_section}

<section>
  <h2>Pipeline health</h2>
  {health_html}
</section>

<section>
  <h2>Accuracy by market</h2>
  <p class="section-note">All-time, live model only. Markets aren't blended together here: CorrectScore's exact-match
  difficulty sits on a totally different scale from a three-way pick like 1X2, so averaging them
  into one number would hide more than it shows.</p>
  <div class="tiles">{market_tiles}</div>
</section>

<section>
  <h2>Round browser: predictions and results</h2>
  {round_browser_html}
</section>

<section>
  <h2>Accuracy by market: each live model vs baseline vs odds-implied</h2>
  {model_accuracy_html}
</section>

<section>
  <h2>Paper betslips: {slips_per_round} slips, ₦{round_stake} every round ({stake_breakdown})</h2>
  <p class="section-note">Simulated accumulators -- no real bets are placed. Every round gets one
  slip per market (the 5 games with the strongest pick in that market) plus one slip with every
  game's single strongest pick across all six markets. "Chance" = the combined estimate used to pick:
  the average of the model, Bet9ja's margin-free odds, and how often similar past picks won (learned
  only from earlier rounds). A slip pays stake &times; all odds multiplied, and only if every pick
  wins. Back-tested over every round since tracking began, so the totals include history from before
  this section existed.</p>
  {betslips_html}
</section>

<section>
  <h2>Other leagues &amp; merged tickets (paper)</h2>
  <p class="section-note">Liga, Bundes, Serie A, Ligue 1, Primeira and Eredivisie each get Premier's 3 tickets
  (1X2, HT O/U 0.5, BTTS top 5, ₦500 each); each league's money starts from ₦0 at its next season.
  Merged tickets take the strongest 1 or 2 picks per market from every league's next round (including
  Premier) into one ticket &mdash; a new set each Premier round, each league round used once. Expected from Premier's history: merged top 1 about
  &minus;31%, merged top 2 about &minus;52% &mdash; every pick adds bet9ja's ~5% cut.</p>
  {leagues_html}
</section>

<section>
  <h2>Cut tracker: are "sure" picks losing more than the odds say?</h2>
  <p class="section-note">Checks the suspicion that the bookmaker makes low-odds favourites lose more often
  than their prices imply. Every number is compared with what Bet9ja's own odds predicted (margin removed).
  ✅ = within normal luck; ⚠️ = a gap bigger than luck usually explains (|z| &ge; 2) -- only meaningful if it
  keeps showing up. Includes tickets losing by exactly one pick, the most painful kind of loss.</p>
  {cuts_html}
</section>

<section>
  <h2>Return on a flat 1-unit stake, per market</h2>
  <p class="section-note">What accuracy can't show: whether backing every pick at its closing
  odds would have made money. Bet9ja's Zoom odds are very well calibrated (outcomes priced at 25%
  happen ~25% of the time), so every strategy trends toward minus the bookmaker's margin (~3-8%
  depending on market). A model with a real edge would show up here as a positive ROI that holds
  over thousands of bets. "Odds fav" = always backing the bookmaker's favourite. Correct Score's
  history includes months of "Other" picks from a since-fixed bug, which almost never won.</p>
  {profit_html}
  <h3>Running profit, round by round (last {dashboard_seasons} seasons, {trend_seasons})</h3>
  <p class="section-note">Live model, every priced pick except Correct Score, 1 unit each. Hover a
  point for that round's own profit; the round browser above shows it per game.</p>
  {profit_trend_html}
</section>

<section>
  <h2>Probability quality: log-loss, per market</h2>
  <p class="section-note">Grades the predicted probabilities, not just the top pick -- lower is
  better. Each model column sits next to the bookmaker's own (margin-removed) odds scored on the
  exact same predictions; green would mean the model's probabilities beat the market's. Correct
  Score is left out (the model's top-8 grid and the ~35 priced scores aren't comparable).</p>
  {probability_html}
</section>

<section>
  <h2>Accuracy trend across rounds, per market</h2>
  <p class="section-note">Last {dashboard_seasons} seasons only ({trend_seasons}), live model only,
  one line per market -- see above for why markets aren't combined into a single line.</p>
  {market_trend_html}
</section>

<footer>
  <p><strong>Reading this dashboard:</strong> since 2026-10-11 only <code>baseline_v0</code> runs
  live -- <code>ml_v1</code> and <code>blend_v1</code> were retired after ~20k predictions showed
  neither beating the odds (see the return and log-loss tables); their history stays above. Original
  notes: three models ran live in parallel and were tracked independently. <code>baseline_v0</code> is a placeholder using bookmaker odds where scraped (1X2,
  BTTS, O/U 2.5, O/U 1.5, HT 1X2, HT O/U 1.5/0.5) and a Poisson goal-expectation model elsewhere
  (Correct Score) -- not a trained model. <code>ml_v1</code> is a real logistic-regression (1X2, BTTS)
  + Poisson-regression (goals) model, trained only on team form/H2H/season-rate/cross-season-strength
  stats -- deliberately NOT on odds, so "beats odds" stays a meaningful comparison rather than a
  circular one. A 5-fold cross-validated backtest (2,300+ historical matches) shows ml_v1 beating dumb
  baselines on 1X2, CorrectScore, and HT_1X2, still losing on OU2.5 -- it is not yet clearly better
  than the placeholder overall, hence running both live rather than replacing one with the other.
  "Baseline" (the reference column, not baseline_v0 the model) is a dumb non-model reference:
  always-Home for 1X2, most-common outcome so far for everything else. "Odds-implied" now applies to
  every market, including the 1st-half markets (a separate page/tab on bet9ja, easy to miss -- see
  scraper/fixtures_scraper.py) and CorrectScore (reads straight off the scraped full grid). Small
  sample sizes make any of these percentages noisy; treat them as directional until many more rounds
  have been reconciled.</p>
  <p>Round numbers on the "upcoming" panel of the round browser are the pipeline's own best-guess
  inference (the live round read straight off the site, plus one -- see scraper/results_scraper.py), made
  against a fixtures page that carries no round label at all -- it can still occasionally be off. Use the
  &larr;/&rarr; buttons above to step through recent history; reconciliation itself does not depend on
  this number (matches are found by team pairing, not round number), so accuracy figures are unaffected
  either way.</p>
</footer>
</div>
<script>
(function() {{
  var btn = document.getElementById('theme-toggle');
  var stored = localStorage.getItem('zoom-dashboard-theme');
  if (stored) document.documentElement.setAttribute('data-theme', stored);
  btn.addEventListener('click', function() {{
    var current = document.documentElement.getAttribute('data-theme');
    var next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('zoom-dashboard-theme', next);
  }});
}})();
function wireBrowser(panelSel, labelId, prevId, nextId) {{
  var panels = Array.prototype.slice.call(document.querySelectorAll(panelSel));
  var label = document.getElementById(labelId);
  var prevBtn = document.getElementById(prevId);
  var nextBtn = document.getElementById(nextId);
  if (!panels.length || !prevBtn) return;

  var idx = panels.length - 1;
  panels.forEach(function(p, i) {{ if (p.classList.contains('active')) idx = i; }});

  function render() {{
    panels.forEach(function(p, i) {{ p.classList.toggle('active', i === idx); }});
    label.textContent = panels[idx].dataset.label;
    prevBtn.disabled = idx === 0;
    nextBtn.disabled = idx === panels.length - 1;
  }}
  prevBtn.addEventListener('click', function() {{ if (idx > 0) {{ idx--; render(); }} }});
  nextBtn.addEventListener('click', function() {{ if (idx < panels.length - 1) {{ idx++; render(); }} }});
  render();
}}
wireBrowser('.round-panel', 'round-nav-label', 'round-prev', 'round-next');
wireBrowser('.slip-panel', 'slip-nav-label', 'slip-prev', 'slip-next');
</script>
</body>
</html>
"""


def _render_cuts_section(built):
    """Cut tracker -- see track/cuts.py."""
    seasons = season_totals(built)
    if not seasons:
        return '<p class="empty">No settled rounds yet.</p>'
    current = next(reversed(seasons))
    cur_rows = {r["label"]: r for r in cut_stats(built, season=current)}
    all_rows = cut_stats(built)

    def cell(r):
        if r is None:
            return "<td>&mdash;</td>"
        pct = lambda x: f"{x / r['n'] * 100:.1f}%"
        if r["unit"] in ("games", "picks"):
            text = f"{pct(r['actual'])} vs {pct(r['expected'])} expected"
        else:
            text = f"{r['actual']:.0f} vs {r['expected']:.0f} expected"
        flag = "miss" if r["unusual"] else "hit"
        return (f'<td class="{flag}">{"⚠️" if r["unusual"] else "✅"} {text} '
                f'<span class="conf">(n={r["n"]:,})</span></td>')

    body = "".join(f'<tr><td>{html.escape(r["label"])}</td>{cell(cur_rows.get(r["label"]))}{cell(r)}</tr>'
                   for r in all_rows)
    return (f'<p class="section-note"><strong>Season {current}:</strong> {html.escape(cut_verdict(list(cur_rows.values())))}'
            f' &middot; <strong>all seasons:</strong> {html.escape(cut_verdict(all_rows))}</p>'
            '<div class="table-scroll"><table><thead><tr><th>Check</th>'
            f'<th>Season {current}</th><th>All seasons</th></tr></thead><tbody>{body}</tbody></table></div>')


def _render_leagues_section(conn):
    """Other leagues' paper tickets + merged cross-league tickets
    (track/league_slips.py)."""
    from track.league_slips import (FLAGS, LEAGUES, build_league, league_totals, merged_name, merged_tickets,
                                    start_after)
    rows = []
    for cid, name in LEAGUES.items():
        b = build_league(conn, cid)
        after = start_after(conn, cid)
        totals = league_totals(b)
        staked = {s: t for s, t in totals.items() if after is not None and s > after}
        nxt = next((html.escape(e["round"]["label"]) for e in reversed(b) if e["round"]["status"] == "upcoming"), "&mdash;")
        if staked:
            t = {k: sum(x[k] for x in staked.values()) for k in ("rounds", "slips", "won", "staked", "returned", "profit")}
            status = "staked (season from ₦0)"
            money = (f'<td>{t["won"]}/{t["slips"]}</td><td>{_naira(t["staked"])}</td>'
                     f'<td class="{"hit" if t["profit"] > 0 else "miss"}">{fmt_naira(t["profit"])}</td>')
        else:
            status = "tracking only &mdash; money starts next season"
            money = "<td>&mdash;</td><td>&mdash;</td><td>&mdash;</td>"
        rows.append(f"<tr><td>{FLAGS[cid]} {html.escape(name)}</td><td>{status}</td><td>{nxt}</td>{money}</tr>")
    league_table = ('<div class="table-scroll"><table><thead><tr><th>League</th><th>Status</th><th>Next round</th>'
                    '<th>Tickets won</th><th>Staked</th><th>Profit</th></tr></thead>'
                    f'<tbody>{"".join(rows)}</tbody></table></div>')

    merged = merged_tickets(conn)
    by_type = {}
    for t in merged:
        x = by_type.setdefault(merged_name(t), {"n": 0, "settled": 0, "won": 0, "staked": 0.0, "returned": 0.0})
        x["n"] += 1
        if t["settled"]:
            x["settled"] += 1
            x["won"] += bool(t["won"])
            x["staked"] += t["stake"]
            x["returned"] += t["returned"]
    def _merged_row(n, x):
        still_open = x["n"] - x["settled"]
        won = f'{x["won"]}/{x["settled"]}' + (f" (+{still_open} open)" if still_open else "")
        profit = x["returned"] - x["staked"]
        return (f'<tr><td>{html.escape(n)}</td><td>{won}</td><td>{_naira(x["staked"])}</td>'
                f'<td>{_naira(x["returned"])}</td><td class="{"hit" if profit > 0 else "miss"}">{fmt_naira(profit)}</td></tr>')

    mrows = "".join(_merged_row(n, x) for n, x in sorted(by_type.items()))
    merged_table = ('<div class="table-scroll"><table><thead><tr><th>Merged ticket</th><th>Won</th><th>Staked</th>'
                    '<th>Returned</th><th>Profit</th></tr></thead>'
                    f'<tbody>{mrows or "<tr><td colspan=5>None yet</td></tr>"}</tbody></table></div>')
    latest = [t for t in merged if merged and t["hour"] == merged[-1]["hour"]]
    latest_html = "".join(
        f'<div class="slip"><div class="slip-head"><strong>{html.escape(merged_name(t))}</strong> '
        f'<span class="conf">{len(t["legs"])} picks &middot; odds {t["odds"]:,.2f} &middot; stake {_naira(t["stake"])} &middot; </span>'
        + (f'<span class="hit">WON {_naira(t["returned"])}</span>' if t["won"] else
           f'<span class="miss">LOST</span> ({t["legs_won"]}/{len(t["legs"])})' if t["settled"] else
           f'pending &middot; pays {_naira(t["stake"] * t["odds"])}')
        + '</div><div class="table-scroll"><table><tbody>'
        + "".join(f'<tr><td>{FLAGS.get(l["league"], "")}</td><td class="teams">{html.escape(l["teams"][0])} <span class="vs">vs</span> '
                  f'{html.escape(l["teams"][1])}</td><td>{html.escape(l["selection"])}</td><td>{l["price"]:.2f}</td>'
                  f'<td class="{"hit" if l["won"] else "miss" if l["won"] is False else ""}">'
                  f'{"&#10003;" if l["won"] else "&#10007;" if l["won"] is False else "pending"}</td></tr>' for l in t["legs"])
        + "</tbody></table></div></div>" for t in latest)
    return "".join([league_table, "<h3>Merged tickets (best picks across all 7 leagues, ₦500 each)</h3>", merged_table,
                    f"<h3>Latest merged tickets{(' &mdash; ' + html.escape(latest[0]['label'])) if latest else ''}</h3>",
                    latest_html or '<p class="empty">None yet.</p>'])


def build_dashboard(conn, out_path=OUT_PATH):
    built = build_slips(load_rounds(conn))
    # Heading shows the staking plan of the season being played right now.
    row = conn.execute("SELECT season_id FROM seasons WHERE status = 'current'").fetchone()
    plan_season = row["season_id"] if row else None
    meta = get_meta(conn)
    stats = accuracy_stats(conn, model_version=DASHBOARD_MODELS)  # per-market tiles
    models_and_stats = get_models_and_stats(conn)
    # Only the per-round trend lines are limited to the last DASHBOARD_SEASONS
    # seasons -- all-time, they're 700+ points squeezed into one chart.
    # Tiles, per-model charts and the ROI table stay all-time.
    season_ids = get_recent_season_ids(conn, DASHBOARD_SEASONS)
    history = get_round_history(conn)

    html_out = PAGE_TEMPLATE.format(
        generated_at=meta["generated_at"],
        meta_section=_render_meta_section(meta),
        market_tiles=_render_market_tiles(stats),
        round_browser_html=_render_round_browser(history),
        model_accuracy_html=_render_model_accuracy_sections(models_and_stats),
        market_trend_html=_render_market_trend_sections(conn, season_ids),
        dashboard_seasons=DASHBOARD_SEASONS,
        trend_seasons=", ".join(f"S{s}" for s in sorted(season_ids)) or "none yet",
        profit_html=_render_profit_section(conn, sorted(models_and_stats)),
        probability_html=_render_probability_section(conn, sorted(models_and_stats)),
        health_html=_render_health_section(get_health(conn)),
        betslips_html=_render_betslips_section(built),
        cuts_html=_render_cuts_section(built),
        leagues_html=_render_leagues_section(conn),
        stake_naira=STAKE_NAIRA,
        stake_breakdown=", ".join(
            f"{SLIP_MARKET_LABELS.get(k.split()[0], k)}{' ' + ' '.join(k.split()[1:]) if k.split()[0] in SLIP_MARKET_LABELS else ''}"
            f" ₦{stake_for(k, plan_season):,}" for k in played_kinds(plan_season)),
        slips_per_round=len(played_kinds(plan_season)),
        round_stake=f"{round_stake(plan_season):,}",
        profit_trend_html=cumulative_profit_chart(get_profit_trend(conn, season_ids)),
    )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_out, encoding="utf-8")
    return out_path
