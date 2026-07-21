"""Generates the static dashboard HTML (docs/index.html) from the current
DB state. Re-run every cycle (see run.py cmd_dashboard / cmd_cycle) so the
GitHub Pages copy always reflects the latest scrape/predict/track pass.
"""
import html
from pathlib import Path

from config import BASE_DIR, ROUNDS_PER_SEASON
from dashboard.charts import accuracy_trend_chart, market_accuracy_chart
from dashboard.data import get_accuracy_trend, get_meta, get_models_and_stats, get_round_history
from track.report import accuracy_stats, verdict

OUT_PATH = BASE_DIR / "docs" / "index.html"

MARKET_ORDER = ["1X2", "BTTS", "OU2.5", "CorrectScore", "HT_1X2", "HT_OU1.5"]
MODEL_SHORT = {"baseline_v0": "Base", "ml_v1": "ML"}


def _model_tag(model_version):
    return MODEL_SHORT.get(model_version, model_version)


def _stat_tile(label, value, sub=None):
    sub_html = f'<div class="tile-sub">{html.escape(sub)}</div>' if sub else ""
    return (f'<div class="tile"><div class="tile-value">{html.escape(str(value))}</div>'
            f'<div class="tile-label">{html.escape(label)}</div>{sub_html}</div>')


def _render_meta_section(meta):
    round_str = f"{meta['current_round']} / {ROUNDS_PER_SEASON}" if meta["current_round"] is not None else "n/a"
    tiles = [
        _stat_tile("Current season round", round_str),
        _stat_tile("Seasons preserved", meta["seasons_tracked"], f"{meta['seasons_archived']} archived"),
        _stat_tile("Last scraped", (meta["last_scraped"] or "n/a").replace("T", " ").split("+")[0] + " UTC"),
    ]
    return f'<div class="tiles">{"".join(tiles)}</div>'


def _render_overall_tile(stats):
    overall_n = sum(s["n"] for s in stats.values())
    overall_correct = sum(s["model_correct"] for s in stats.values())
    if overall_n == 0:
        return _stat_tile("Overall accuracy", "n/a", "no reconciled predictions yet")
    acc = overall_correct / overall_n
    return _stat_tile("Overall accuracy", f"{acc * 100:.1f}%",
                       f"{overall_correct}/{overall_n} predictions, all markets AND models blended")


MARKET_LABELS = {
    "1X2": "1X2", "BTTS": "BTTS", "OU2.5": "O/U 2.5",
    "CorrectScore": "Correct Score", "HT_1X2": "HT 1X2", "HT_OU1.5": "HT O/U 1.5",
}


PENDING_NOTES = {
    "upcoming": "Upcoming -- best-guess round numbering, see note below",
    "pending": ("Pending -- superseded by a newer batch before this round was confirmed played; "
                "outcome not yet known, will fill in once reconciled"),
}


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
                    f'<div class="model-pred"><span class="model-tag">{_model_tag(mv)}</span> '
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
    else:
        rows = []
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
                    lines.append(f'<div class="model-pred {cls}"><span class="model-tag">{_model_tag(mv)}</span> '
                                  f'{mark} {html.escape(e["predicted"])}</div>')
                cells.append(f'<td>{"".join(lines)}</td>')
            score = f'{m["ft_a"]}-{m["ft_b"]}' + (f' (HT {m["ht_a"]}-{m["ht_b"]})' if m["ht_a"] is not None else "")
            rows.append(
                f'<tr><td class="teams">{html.escape(m["team_a"])} <span class="vs">vs</span> '
                f'{html.escape(m["team_b"])} <span class="score">{score}</span></td>{"".join(cells)}</tr>'
            )
        body = (
            f'<div class="table-scroll"><table><thead><tr><th>Result</th>{header_cells}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
        )
        label = f'Round {entry["round_number"]} (reconciled)'

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
.model-pred.hit {{ color: var(--good); }}
.model-pred.miss {{ color: var(--bad); }}
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
  <h2>Overall</h2>
  <div class="tiles">{overall_tile}</div>
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
  <h2>Accuracy trend across rounds (all models blended)</h2>
  {trend_chart_html}
</section>

<footer>
  <p><strong>Reading this dashboard:</strong> two models run live in parallel and get tracked
  independently. <code>baseline_v0</code> is a placeholder using bookmaker odds where scraped (1X2,
  BTTS) and a Poisson goal-expectation model elsewhere (O/U 2.5, Correct Score, HT markets) -- not a
  trained model. <code>ml_v1</code> is a real logistic-regression (1X2, BTTS) + Poisson-regression
  (goals) model, trained only on team form/H2H/season-rate stats -- deliberately NOT on odds, so
  "beats odds" stays a meaningful comparison rather than a circular one. A 5-fold cross-validated
  backtest (540 historical matches) showed ml_v1 modestly beating dumb baselines on 1X2 and HT_1X2, but
  actually losing to them on OU2.5 and CorrectScore -- it is not yet clearly better than the placeholder
  overall, hence running both live rather than replacing one with the other. "Baseline" (the reference
  column, not baseline_v0 the model) is a dumb non-model reference: always-Home for 1X2, most-common
  outcome so far for everything else. "Odds-implied" only applies to 1X2/BTTS -- Over/Under odds were
  never successfully scraped (a known gap from the results scraper build). Small sample sizes make any
  of these percentages noisy; treat them as directional until many more rounds have been reconciled.</p>
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
(function() {{
  var panels = Array.prototype.slice.call(document.querySelectorAll('.round-panel'));
  var label = document.getElementById('round-nav-label');
  var prevBtn = document.getElementById('round-prev');
  var nextBtn = document.getElementById('round-next');
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
}})();
</script>
</body>
</html>
"""


def build_dashboard(conn, out_path=OUT_PATH):
    meta = get_meta(conn)
    stats = accuracy_stats(conn)  # blended across models -- used only for the headline "Overall" tile
    models_and_stats = get_models_and_stats(conn)
    history = get_round_history(conn)
    trend = get_accuracy_trend(conn)

    html_out = PAGE_TEMPLATE.format(
        generated_at=meta["generated_at"],
        meta_section=_render_meta_section(meta),
        overall_tile=_render_overall_tile(stats),
        round_browser_html=_render_round_browser(history),
        model_accuracy_html=_render_model_accuracy_sections(models_and_stats),
        trend_chart_html=accuracy_trend_chart(trend),
    )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_out, encoding="utf-8")
    return out_path
