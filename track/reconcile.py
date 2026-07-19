"""Reconciles predictions against actual results once the predicted round
completes, scoring the model against two reference points every prediction
should be judged against: a dumb baseline and the market's own odds.
"""
from collections import Counter

from db.upsert import upsert_prediction_result


def _ft_result(a, b):
    if a > b:
        return "Home"
    if a < b:
        return "Away"
    return "Draw"


def actual_labels(match):
    """match: dict with ft_a, ft_b, ht_a, ht_b. Returns {market: actual_label}
    for every market the pipeline predicts."""
    ft_a, ft_b, ht_a, ht_b = match["ft_a"], match["ft_b"], match["ht_a"], match["ht_b"]
    total_ft = ft_a + ft_b

    labels = {
        "1X2": _ft_result(ft_a, ft_b),
        "BTTS": "Yes" if (ft_a > 0 and ft_b > 0) else "No",
        "OU2.5": "Over" if total_ft > 2.5 else "Under",
        "CorrectScore": f"{ft_a}-{ft_b}",
    }
    if ht_a is not None and ht_b is not None:
        total_ht = ht_a + ht_b
        labels["HT_1X2"] = _ft_result(ht_a, ht_b)
        labels["HT_OU1.5"] = "Over" if total_ht > 1.5 else "Under"
    return labels


def compute_dynamic_baselines(conn):
    """Dumb, non-model reference label per market. 1X2 is hardcoded to
    "Home" per the project brief's own historical base rate; everything
    else is "whichever outcome has actually been most common across every
    played match so far" -- recomputed fresh each run since it's a
    diagnostic reference point, not a model input, so there's no leakage
    concern in using all data available at reconciliation time.
    """
    matches = conn.execute(
        "SELECT ft_a, ft_b, ht_a, ht_b FROM matches WHERE ft_a IS NOT NULL"
    ).fetchall()

    counters = {market: Counter() for market in ["BTTS", "OU2.5", "CorrectScore", "HT_1X2", "HT_OU1.5"]}
    for m in matches:
        for market, label in actual_labels(dict(m)).items():
            if market != "1X2":
                counters[market][label] += 1

    baselines = {"1X2": "Home"}
    for market, counter in counters.items():
        if counter:
            baselines[market] = counter.most_common(1)[0][0]
    return baselines


def _latest_odds_map(conn, fixture_id):
    """Raw odds pulled straight from the `odds` table, never the derived
    `features` table -- features rows for a fixture get pruned once its
    round is played (see features/build.py), but the odds captured for it
    pre-kickoff need to survive for this historical comparison."""
    rows = conn.execute(
        """SELECT o.market, o.selection, o.implied_prob
           FROM odds o
           JOIN (
               SELECT market, selection, MAX(captured_at) AS latest
               FROM odds WHERE fixture_id = ?
               GROUP BY market, selection
           ) lo ON o.market = lo.market AND o.selection = lo.selection AND o.captured_at = lo.latest
           WHERE o.fixture_id = ?""",
        (fixture_id, fixture_id),
    ).fetchall()
    return {(r["market"], r["selection"]): r["implied_prob"] for r in rows}


def odds_implied_label(odds_map, market):
    """What the bookmaker's own odds favored pre-kickoff, for markets we
    actually scrape odds for (1X2, BTTS). None for OU2.5/CorrectScore/HT_*
    -- we don't scrape those markets' odds (OU2.5 is the open Stage 1 gap;
    the others were never in scope)."""
    if market == "1X2":
        vals = {"Home": odds_map.get(("1X2", "Home")),
                "Draw": odds_map.get(("1X2", "Draw")),
                "Away": odds_map.get(("1X2", "Away"))}
    elif market == "BTTS":
        vals = {"Yes": odds_map.get(("BTTS", "Yes")), "No": odds_map.get(("BTTS", "No"))}
    elif market == "OU2.5":
        vals = {"Over": odds_map.get(("OU2.5", "Over")), "Under": odds_map.get(("OU2.5", "Under"))}
    else:
        return None

    if any(v is None for v in vals.values()):
        return None
    return max(vals, key=vals.get)


def reconcile(conn):
    """Finds every prediction without a prediction_results row yet, checks
    whether its fixture has since been played (matched by
    season_id/round_number/team_a/team_b -- NOT match_number, which the
    fixtures page and results page assign independently and aren't
    guaranteed to agree on), and reconciles it if so.
    """
    pending = conn.execute(
        """SELECT p.prediction_id, p.fixture_ref, p.market, p.label AS predicted_label,
                  f.season_id, f.round_number, f.team_a, f.team_b
           FROM predictions p
           JOIN fixtures f ON f.fixture_id = p.fixture_ref
           LEFT JOIN prediction_results pr ON pr.prediction_id = p.prediction_id
           WHERE pr.result_id IS NULL"""
    ).fetchall()

    if not pending:
        return {"reconciled": 0, "still_pending": 0}

    baselines = compute_dynamic_baselines(conn)
    match_cache, odds_cache = {}, {}
    reconciled = still_pending = 0

    for row in pending:
        # Matched on (season_id, team_a, team_b) only -- NOT round_number.
        # A fixture's round_number is our own inference (last-played-round + 1)
        # made at scrape time against a page that has no round label at all;
        # it can be off by one in practice (e.g. the odds page can already be
        # showing the round *after* next once betting closes on the imminent
        # one). Team pairing is the reliable key: under a double round-robin,
        # a given (team_a, team_b) ordering occurs at most once per season.
        key = (row["season_id"], row["team_a"], row["team_b"])
        if key not in match_cache:
            m = conn.execute(
                """SELECT match_id, round_number, ft_a, ft_b, ht_a, ht_b FROM matches
                   WHERE season_id = ? AND team_a = ? AND team_b = ?""",
                key,
            ).fetchone()
            match_cache[key] = dict(m) if m else None
        match = match_cache[key]

        if match is None or match["ft_a"] is None:
            still_pending += 1
            continue

        if row["fixture_ref"] not in odds_cache:
            odds_cache[row["fixture_ref"]] = _latest_odds_map(conn, row["fixture_ref"])
        odds_map = odds_cache[row["fixture_ref"]]

        market = row["market"]
        actual = actual_labels(match).get(market)
        if actual is None:
            still_pending += 1
            continue

        upsert_prediction_result(
            conn,
            prediction_id=row["prediction_id"],
            match_ref=match["match_id"],
            market=market,
            predicted_label=row["predicted_label"],
            actual_label=actual,
            baseline_label=baselines.get(market),
            odds_implied_label=odds_implied_label(odds_map, market),
        )
        reconciled += 1

    return {"reconciled": reconciled, "still_pending": still_pending}
