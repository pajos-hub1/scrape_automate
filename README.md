# Can a bookmaker's virtual football be beaten?

A self-running data pipeline that has watched every game of Bet9ja's **Zoom** virtual football since July 2026: about 29,000 results and 1.8 million odds across 7 leagues. It predicts every round, tracks every prediction against its result, runs paper betting strategies, and publishes a live dashboard and Telegram alerts. There's no server: it all runs on GitHub Actions, every 15 minutes.

**Live dashboard:** https://pajos-hub1.github.io/scrape_automate/

> ⚠️ **Not betting advice.** Every ticket here is **paper only**, with no real money. The answer to the title question is **no**, and the evidence is below.

---

## What it found

Over 29,000 games, these are the findings, from most to least important:

**1. Zoom's odds are almost perfectly accurate.** Outcomes priced at 25% happen 25% of the time; priced at 55%, 54.4% of the time. Every trained model (form, head-to-head, league table, team history) predicted *worse* than the odds alone.

**2. Zoom has a simple, fixed design, and we measured it.** From each game's correct-score odds, every game's expected goals follow one formula:

```
log(home goals) = base + home advantage + attack[home team] − defence[away team]
log(away goals) = base                  + attack[away team] − defence[home team]
```

- **The team ratings never change.** They're the same in season 7 and season 38, and the same in round 1 and round 38. There's no form, no momentum and no table pressure: Arsenal is +0.81 every season.
- **Home advantage is fixed:** exactly ×1.20 goals in Premier, and ×1.27–1.29 in the other six leagues.
- **Each game also gets a small random adjustment** (~5%), which the odds already include.
- **Goals are Poisson, and each half gets exactly half of them.**

That fixed-ratings model explains 97.8% of Zoom's odds in every season.

**3. That's why it can't be beaten.** Zoom prices every game straight from that engine, so a perfect copy of it reproduces Bet9ja's odds and finds nothing they missed. A full audit tested every route to an edge:

| Test | Result |
|---|---|
| Every selection in every market (71, incl. all correct scores, HT/FT, every goal line) | none pays more than it costs |
| Do markets disagree (correct score vs 1X2 / over-under / BTTS)? | agree to within 0.5 points |
| Is the randomness flawed (previous game, same slot, rematch, slot, time of day, late season, games at the same moment)? | all within chance |
| Does the engine use anything the odds ignore (form, table, points gap)? | no |
| Value bets from our copy of the engine, on unseen seasons | −11.7% over 2,765 bets |

**4. The one real crack is too small.** The strongest favourites win 1–2 points more often than their odds say. That's measurable (z ≈ 3), but it's smaller than Bet9ja's ~5% cut, so favourites still lose about 3% a pick instead of 5%.

**5. What that means for a punter.** The strategy tester plays a plan over 2,000 simulated months on a copy of Zoom's engine:

| Plan (₦500 a ticket) | Average month | Months in profit |
|---|---|---|
| Premier: 3 accumulator tickets a round | −10% to −17% | 1–8% |
| Merged cross-league accumulators | −12% to −17% | 0% |
| Single bets on picks with a 75%+ chance | −3% to −4% | 0–2% |

The simulator is checked against reality: our actual paper results land at the 87th and 95th percentile of its range, which is ordinary good luck.

## How it works

Every ~15 minutes, one GitHub Actions run:

1. **Collects** results, upcoming games and full odds for 7 leagues from Bet9ja's own JSON APIs. I found these by recording the website's network calls, and they replaced a fragile Selenium scraper (kept as an automatic fallback).
2. **Predicts** every game. The live model is odds-based, because nothing beat the odds.
3. **Reconciles** each prediction with its result, matched by Bet9ja's game ID.
4. **Runs paper strategies**: per-league accumulators, plus "merged" tickets built from the strongest picks across all leagues. Each pick must clear a minimum chance ("bar"), and money is tracked per season from ₦0.
5. **Books every ticket on Bet9ja** through the site's own "Book a Bet" call (no login, no stake), so each Telegram ticket carries a code you can load on the site.
6. **Publishes** the dashboard (GitHub Pages) and Telegram messages. Each message is sent once, tracked in the database, with a separate bot for merged tickets.
7. **Saves** the database and makes a daily backup.

## Engineering notes

The parts that took real work:

- **Keeping a free scheduler alive.** GitHub's cron dropped about 80% of scheduled runs. Each run now starts the next one itself, a watchdog alerts if data goes stale for 2 hours, and failures arrive on Telegram.
- **Storage under GitHub's limits.** The SQLite database is stored gzipped. Old rows move into frozen archive files that are queried together with the live data through views. The database lives on a separate `data` branch as **one snapshot that every run replaces**, plus dated backup tags: 30 daily, then one per month kept for good. Before that change, a fresh database copy was committed every 15 minutes and the repo had grown to 17 GB.
- **A data-loss bug, found and fully recovered.** Archive files were numbered by text order, so `zoom_archive_1000` sorted before `_997`, and every run overwrote archive 1000. I found it during an audit, fixed it, and rebuilt every lost row (about 120,000) from the 80 overwritten versions in git history, checked against everything else for duplicates.
- **Checking my own results.** Two "discoveries" during the analysis were bugs. Duplicate rows faked a strong pattern between consecutive games, and pairing odds with results by slot number (which didn't match between the old scraper and the API) faked a +15% "edge". Both fell apart under checks, and both checks are now part of the method.

## Project layout

```
scraper/zoom_api.py      Bet9ja JSON APIs: results, upcoming games, odds, Book-a-Bet codes
db/                      SQLite schema, idempotent upserts, gzip storage, archiving
track/                   reconcile, paper tickets (Premier, other leagues, merged), Telegram, weekly summary
sim/                     copy of Zoom's engine: ratings measured from odds, the strategy tester
dashboard/               static HTML dashboard, server-rendered SVG charts, phone-friendly
scripts/data_branch.sh   load / save / back up / restore the database on the `data` branch
.github/workflows/       cycle.yml (every 15 min), freshness.yml (watchdog), tests.yml
tests/                   pytest suite (90 tests), no network: the APIs are faked
```

## Running it yourself

```bash
python -m venv env && source env/bin/activate
pip install -r requirements.txt pytest

scripts/data_branch.sh load        # fetch the latest database + archives from the `data` branch

python run.py cycle                # the full pipeline once
python run.py simulate --help      # the strategy tester: try your own plan, stake and budget
python run.py dashboard            # rebuild docs/index.html
python -m pytest -q tests          # the test suite
```

`run.py --help` lists every step. Telegram is optional: set the repo secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (a comma-separated list is fine; the first chat also gets failure alerts).

**Backups:** `scripts/data_branch.sh list` shows them; `scripts/data_branch.sh restore backup-YYYY-MM-DD` puts one back.

**Clone tip:** `main`'s history before 2026-11-06 contains a database copy every 15 minutes, so use a shallow clone: `git clone --depth 1 https://github.com/pajos-hub1/scrape_automate.git`
