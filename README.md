# Zoom prediction pipeline

An automated data pipeline that collects Bet9ja **Zoom** (virtual football) results and odds every 15 minutes, makes predictions, tracks them against real results, and publishes a live dashboard and Telegram updates. It runs entirely on GitHub Actions, with no server.

**Live dashboard:** https://pajos-hub1.github.io/scrape_automate/

> ⚠️ **Not betting advice.** Every "ticket" in this project is **paper only**: simulated, with no real money. The project's main finding is that **Zoom's odds can't be beaten** (see [What the data shows](#what-the-data-shows)).

---

## What it does

Every ~15 minutes, one GitHub Actions run:

1. **Collects data** from Bet9ja's own JSON APIs, the same ones its web pages use. That covers results for the current and previous season, plus upcoming games with all odds for **7 Zoom leagues**: Premier, Liga, Bundesliga, Serie A, Ligue 1, Primeira and Eredivisie.
2. **Builds features and predictions.** The live model is odds-based, because no trained model beat the odds (details below).
3. **Reconciles** each prediction with its result, matched exactly by Bet9ja's own game ID.
4. **Builds paper tickets** (accumulators) per league, plus "merged" tickets combining the strongest picks across all leagues. It then tracks their profit and loss per season, starting from ₦0 each season.
5. **Publishes**:
   - the static **dashboard** (GitHub Pages)
   - **Telegram** messages: new slips, results, season summaries, a weekly "cut check", and a league comparison every season
6. **Commits** the updated database back to the repo, compressed and archived to keep commits small.

It also **looks after itself**:
- each run starts the next one, because GitHub's own scheduler dropped about 80% of runs
- a watchdog alerts if no new data arrives for 2 hours
- failures are reported on Telegram and on the dashboard's health panel

## What the data shows

Measured on tens of thousands of games and picks since July 2026:

| Question | Answer |
|---|---|
| Are Bet9ja's Zoom odds accurate? | **Yes, almost perfectly.** Outcomes priced at 25% happen 25.0% of the time, 55% → 54.4%, and so on. |
| Can a model beat them? | **No.** Trained models (form, head-to-head, league table, team history) all scored *worse* than the odds alone. |
| Do "sure" picks fail more than the odds say (a "trap")? | **No.** Strong picks (70%+) failed slightly *less* than expected, and no pattern (round, time, team, streak) predicts failures. |
| Where does the money go? | A flat **~5% margin on every pick**: 0 of 52,000+ picks paid more than fair. Accumulators multiply it (a 5-pick ticket is about −20%). |
| Do other leagues differ? | Being measured live: the dashboard's "League comparison" ranks them each season. |

The pipeline keeps measuring all of this. If Bet9ja's pricing ever changed, the profit tables and the cut tracker would show it.

## How it's built

```
scraper/zoom_api.py      Bet9ja JSON APIs: competitions, results, sportsbook events + odds
scraper/*_scraper.py     Old Selenium scrapers, kept as an automatic fallback
db/                      SQLite schema, idempotent upserts, gzip storage, archiving
features/, predict/      Feature engineering and predictors (odds-based baseline; retired ML models kept)
track/reconcile.py       Predictions → results, by Bet9ja game id
track/betslips.py        Premier paper tickets, staking plans per season
track/leagues.py         Collects every league's results + odds
track/league_slips.py    Other-league tickets and merged cross-league tickets
track/league_report.py   League comparison (accuracy vs odds, margin, ticket return)
track/cuts.py            "Cut tracker": do picks fail more than the odds say?
track/notify.py          Telegram messages (sent once each, tracked in the DB)
dashboard/               Static HTML dashboard (server-rendered SVG charts, no JS framework)
.github/workflows/       cycle.yml (the 15-minute pipeline), freshness.yml (watchdog), tests.yml
tests/                   pytest suite, no network: the APIs are faked
```

**Storage.** The SQLite database lives in the repo as `data/zoom.db.gz`. Once it passes 15 MB, old settled rows move into frozen archive files under `data/archive/`. Readers query live + archived data through views, so history is never lost.

## Running it yourself

```bash
python -m venv env && source env/bin/activate
pip install -r requirements.txt pytest

python run.py cycle          # the full pipeline once: scrape → predict → track → dashboard
python run.py track          # accuracy, profit and log-loss report in the terminal
python run.py dashboard      # rebuild docs/index.html
python -m pytest -q tests    # the test suite (no network needed)
```

`run.py --help` lists every step, including `scrape --browser` to force the old Selenium scraper.

**Telegram (optional):** add the repo secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. The chat ID can be a comma-separated list; the first chat also gets failure alerts. Without them, messages are skipped.

## Clone tip

The automation commits every 15 minutes, so the full history is large. A shallow clone is much faster:

```bash
git clone --depth 1 https://github.com/pajos-hub1/scrape_automate.git
```
