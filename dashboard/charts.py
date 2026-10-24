"""Server-rendered SVG charts -- no client-side charting library. Colors
are the validated 3-slot categorical subset (blue/green/magenta) from the
dataviz skill's reference palette, run through scripts/validate_palette.js
for both light and dark modes before use (all checks pass; magenta carries
a light-mode contrast WARN, mitigated here with direct value labels on
every bar per the relief rule).
"""
import html

SERIES_COLORS = {
    "model": ("var(--series-1)", "Model"),
    "baseline": ("var(--series-2)", "Baseline"),
    "odds": ("var(--series-3)", "Odds-implied"),
}


def market_accuracy_chart(stats_by_market, width=680, height=340):
    """Grouped bar chart: one group per market, up to 3 bars
    (model/baseline/odds) per group, fixed color order, direct labels."""
    markets = sorted(stats_by_market)
    if not markets:
        return '<p class="empty">No reconciled predictions yet.</p>'

    margin_left, margin_right, margin_top, margin_bottom = 40, 16, 16, 56
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    group_w = plot_w / len(markets)
    bar_gap = 4
    max_bars = 3
    bar_w = (group_w - bar_gap * (max_bars + 1)) / max_bars

    def y_of(frac):
        return margin_top + plot_h * (1 - frac)

    svg = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
           f'aria-label="Model vs baseline vs odds-implied accuracy by market">']

    # gridlines + y-axis labels at 0/25/50/75/100%
    for pct in (0, 25, 50, 75, 100):
        y = y_of(pct / 100)
        svg.append(f'<line x1="{margin_left}" y1="{y:.1f}" x2="{width - margin_right}" y2="{y:.1f}" '
                    f'class="gridline" />')
        svg.append(f'<text x="{margin_left - 8}" y="{y:.1f}" class="axis-label" '
                    f'text-anchor="end" dominant-baseline="middle">{pct}%</text>')

    for gi, market in enumerate(markets):
        s = stats_by_market[market]
        model_acc = s["model_correct"] / s["n"] if s["n"] else None
        baseline_acc = s["baseline_correct"] / s["baseline_n"] if s["baseline_n"] else None
        odds_acc = s["odds_correct"] / s["odds_n"] if s["odds_n"] else None

        group_x0 = margin_left + gi * group_w
        bars = [("model", model_acc), ("baseline", baseline_acc), ("odds", odds_acc)]

        for bi, (series, acc) in enumerate(bars):
            bx = group_x0 + bar_gap + bi * (bar_w + bar_gap)
            if acc is None:
                continue
            color, series_label = SERIES_COLORS[series]
            by = y_of(acc)
            bh = margin_top + plot_h - by
            label = f"{acc * 100:.0f}%"
            svg.append(
                f'<g class="bar-group">'
                f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" rx="3" '
                f'fill="{color}"><title>{html.escape(market)} · {series_label}: {label} '
                f'(n={s["n"] if series == "model" else (s["baseline_n"] if series == "baseline" else s["odds_n"])})'
                f'</title></rect>'
                f'<text x="{bx + bar_w / 2:.1f}" y="{by - 6:.1f}" class="bar-label" '
                f'text-anchor="middle">{label}</text>'
                f'</g>'
            )

        svg.append(f'<text x="{group_x0 + group_w / 2:.1f}" y="{height - margin_bottom + 20:.1f}" '
                    f'class="axis-label" text-anchor="middle">{html.escape(market)}</text>')

    svg.append(f'<line x1="{margin_left}" y1="{margin_top + plot_h:.1f}" '
                f'x2="{width - margin_right}" y2="{margin_top + plot_h:.1f}" class="axis-line" />')
    svg.append('</svg>')

    legend = '<div class="legend">' + "".join(
        f'<span class="legend-item"><span class="swatch" style="background:{color}"></span>{label}</span>'
        for color, label in SERIES_COLORS.values()
    ) + '</div>'

    return "".join(svg) + legend


def accuracy_trend_chart(trend, width=680, height=220):
    """Line chart of blended accuracy per reconciled round. Needs at least
    3 points to be worth reading as a trend rather than noise."""
    if len(trend) < 3:
        return (f'<p class="empty">Only {len(trend)} round(s) reconciled so far -- '
                 f'need at least 3 to show a meaningful trend.</p>')

    margin_left, margin_right, margin_top, margin_bottom = 40, 16, 16, 32
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    xs = [p["round_number"] for p in trend]
    x_min, x_max = min(xs), max(xs)
    x_span = max(x_max - x_min, 1)

    def x_of(rn):
        return margin_left + plot_w * (rn - x_min) / x_span

    def y_of(frac):
        return margin_top + plot_h * (1 - frac)

    svg = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
           f'aria-label="Blended prediction accuracy trend across reconciled rounds">']

    for pct in (0, 25, 50, 75, 100):
        y = y_of(pct / 100)
        svg.append(f'<line x1="{margin_left}" y1="{y:.1f}" x2="{width - margin_right}" y2="{y:.1f}" '
                    f'class="gridline" />')
        svg.append(f'<text x="{margin_left - 8}" y="{y:.1f}" class="axis-label" '
                    f'text-anchor="end" dominant-baseline="middle">{pct}%</text>')

    points = [(x_of(p["round_number"]), y_of(p["accuracy"])) for p in trend]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(points))
    svg.append(f'<path d="{path}" fill="none" stroke="var(--series-1)" stroke-width="2" />')

    for p, (x, y) in zip(trend, points):
        label = p.get("label", f'Round {p["round_number"]}')
        svg.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="var(--series-1)">'
                    f'<title>{label}: {p["accuracy"] * 100:.1f}% (n={p["n"]})</title>'
                    f'</circle>')

    last_x, last_y = points[-1]
    svg.append(f'<text x="{last_x:.1f}" y="{last_y - 10:.1f}" class="bar-label" '
               f'text-anchor="middle">{trend[-1]["accuracy"] * 100:.0f}%</text>')

    svg.append(f'<line x1="{margin_left}" y1="{margin_top + plot_h:.1f}" '
                f'x2="{width - margin_right}" y2="{margin_top + plot_h:.1f}" class="axis-line" />')
    svg.append('</svg>')
    return "".join(svg)


def _fmt_units(v, precise=False):
    return f"{v:+.2f}" if precise else f"{v:+.0f}"


def fmt_naira(v, precise=False):
    return f"{'-' if v < 0 else '+'}₦{abs(v):,.0f}"


def cumulative_profit_chart(trend, width=680, height=240, fmt=_fmt_units, unit=" units", bet_word="bets",
                            y_range=None, marker=None):
    """Running profit across rounds, with a zero line -- above zero would
    mean the picks are winning money overall. Each point's tooltip carries
    that single round's own profit. fmt/unit switch between flat-stake
    units and naira (fmt_naira, unit=""). y_range=(lo, hi, step) puts several
    charts on one scale; marker=(index, text) draws a dashed line before
    that point (e.g. where a rule changed)."""
    if len(trend) < 2:
        return (f'<p class="empty">Only {len(trend)} round(s) with priced, reconciled picks so far.</p>')

    margin_left, margin_right, margin_top, margin_bottom = 52, 16, 16, 32
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    vals = [p["cumulative"] for p in trend]
    if y_range:
        lo, hi, step = y_range
    else:
        lo, hi = min(min(vals), 0.0), max(max(vals), 0.0)
        span = (hi - lo) or 1.0
        lo, hi = lo - span * 0.08, hi + span * 0.08
    n = len(trend)

    def x_of(i):
        return margin_left + plot_w * i / max(n - 1, 1)

    def y_of(v):
        return margin_top + plot_h * (hi - v) / (hi - lo)

    svg = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
           f'aria-label="Running flat-stake profit across rounds">']

    ticks = ([lo + step * k for k in range(round((hi - lo) / step) + 1)] if y_range
             else [lo + (hi - lo) * k / 4 for k in range(5)])
    for v in ticks:
        y = y_of(v)
        svg.append(f'<line x1="{margin_left}" y1="{y:.1f}" x2="{width - margin_right}" y2="{y:.1f}" class="gridline" />')
        svg.append(f'<text x="{margin_left - 8}" y="{y:.1f}" class="axis-label" text-anchor="end" '
                   f'dominant-baseline="middle">{html.escape(fmt(v))}</text>')
    y0 = y_of(0)
    svg.append(f'<line x1="{margin_left}" y1="{y0:.1f}" x2="{width - margin_right}" y2="{y0:.1f}" class="axis-line" />')

    if marker is not None and 0 < marker[0] < n:
        mx = (x_of(marker[0] - 1) + x_of(marker[0])) / 2
        svg.append(f'<line x1="{mx:.1f}" y1="{margin_top}" x2="{mx:.1f}" y2="{margin_top + plot_h:.1f}" '
                   f'class="gridline" stroke-dasharray="4 3" />')
        svg.append(f'<text x="{mx + 4:.1f}" y="{margin_top + 10}" class="axis-label">{html.escape(marker[1])}</text>')
    points = [(x_of(p["index"]), y_of(p["cumulative"])) for p in trend]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(points))
    svg.append(f'<path d="{path}" fill="none" stroke="var(--series-1)" stroke-width="2" />')
    for p, (x, y) in zip(trend, points):
        svg.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="var(--series-1)">'
                   f'<title>{html.escape(p["label"])}: this round {html.escape(fmt(p["profit"], True))}{unit} on '
                   f'{p["bets"]} {bet_word}; running total {html.escape(fmt(p["cumulative"], True))}{unit}</title></circle>')

    last_x, last_y = points[-1]
    svg.append(f'<text x="{last_x:.1f}" y="{last_y - 10:.1f}" class="bar-label" text-anchor="end">'
               f'{html.escape(fmt(trend[-1]["cumulative"], True))}{unit}</text>')
    svg.append('</svg>')
    return "".join(svg)
