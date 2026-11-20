# fb_tracker: real-football odds across Nigerian bookmakers

A separate project from the Zoom pipeline. Goal: find **arbitrage** (bookmakers disagreeing so much that backing every outcome locks in a profit) and later **value bets**, on real football.

## Step 1: feasibility check (running now, no betting)

Every 10 minutes, a GitHub Actions job (`.github/workflows/fb_collect.yml`):

1. Reads every pre-match football match from **Bet9ja** (about 200 leagues) and **SportyBet** (about 1,700 matches), using the same public JSON their websites use, with polite gaps between requests.
2. **Matches the same game** at both bookmakers by kickoff time (within 15 minutes) and team names, which are normalised so "Man City" = "Manchester City" and "Viborg FF" = "Viborg", while youth and women's sides stay separate. In testing, about 90% of SportyBet's matches paired up.
3. **Checks five ways of covering every outcome** for a sure profit: 1X2; over/under 2.5; and home, away or draw against the matching double chance. It records each **arb** (start time, end time, best margin, and the stakes for ₦10,000), plus how close every game came to one.

BetKing is next: its odds are readable, but its full match list still has to be found.

```bash
python -m fb_tracker.collect      # one snapshot (about 5 minutes)
python -m fb_tracker.report       # how many arbs, how big, how long they lasted, which leagues
fb_tracker/data_branch.sh load    # get the collected data (it lives on the `fb-data` branch)
```

The data lives on its own `fb-data` branch as one snapshot that each save replaces. It never goes on `main`, and never near the Zoom `data` branch. To stop the collector, set the repo variable `FB_COLLECT=off` or disable the workflow.

**After 3–4 days**, the report decides whether to continue. If arbs are frequent enough, big enough (1–3%) and last long enough to place by hand, we build live alerts with exact stakes. If not, we stop.

## Risks to keep in mind

- Odds can change between placing the two sides of an arb.
- Bookmakers limit or close accounts that only take arbs.
- It needs money spread across several bookmaker accounts.
