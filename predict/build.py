"""DB orchestration for predictions: finds the earliest unplayed round
(the fixtures currently sitting in `features`), runs a Predictor over each
one, and upserts the results. Always runs before kickoff -- fixtures only
exist in the DB pre-match, so there's no way to accidentally predict
something already played.
"""
from db.queries import ODDS_FIELD_MAP, get_current_fixture_batch, get_latest_odds_by_fixture
from db.upsert import upsert_prediction
from predict.training_data import STAT_FEATURE_COLS


def _predict_rows(conn, predictor, target_rows):
    written = 0
    for row in target_rows:
        markets = predictor.predict_markets(row)
        for market, result in markets.items():
            upsert_prediction(
                conn,
                fixture_ref=row["fixture_ref"],
                model_version=predictor.model_version,
                market=market,
                label=result["label"],
                probabilities=result["probabilities"],
                confidence=result["confidence"],
            )
            written += 1
    return written


def build_predictions(conn, predictor):
    summary = {
        "round_number": None, "fixtures_predicted": 0, "predictions_written": 0,
        "orphan_round_number": None, "orphan_fixtures_predicted": 0, "orphan_predictions_written": 0,
    }

    rows = [dict(r) for r in conn.execute("SELECT * FROM features WHERE fixture_ref IS NOT NULL")]
    if rows:
        earliest_round = min(r["round_number"] for r in rows)
        target_rows = [r for r in rows if r["round_number"] == earliest_round]
        summary["round_number"] = earliest_round
        summary["fixtures_predicted"] = len(target_rows)
        summary["predictions_written"] = _predict_rows(conn, predictor, target_rows)

    # Independently ALSO check for a season-boundary orphan batch: the
    # fixtures page already rolled over to a new season's Round 1, but that
    # season doesn't exist in our DB yet (won't, until its own Round 1
    # finishes and gets fingerprinted -- see run.py cmd_scrape). It has zero
    # history by definition (brand new season), so there's nothing for
    # features/build.py to compute from, and features.season_id is NOT NULL
    # in the schema -- predicted directly here from odds alone instead
    # (still valuable, especially for the baseline model's 1X2/BTTS) rather
    # than silently skipped until the season resolves.
    #
    # This is NOT an "else" -- a season's own final round can still be
    # sitting unreconciled in `features` at the exact same time a new
    # season's orphan Round 1 appears (its Round 38 hasn't been scraped as
    # played yet). Treating these as mutually exclusive meant the orphan
    # batch only got a chance to be predicted on a cycle where `features`
    # happened to be fully empty -- if the old season's leftovers were still
    # there, the orphan batch was skipped that cycle, and if a fresher poll
    # (e.g. Round 2's preview) superseded it before `features` next went
    # empty, it was silently skipped forever. This is exactly how Season 6's
    # Round 1 orphan batch was lost.
    batch = get_current_fixture_batch(conn, None)
    if batch:
        odds_map = get_latest_odds_by_fixture(conn, [r["fixture_id"] for r in batch])
        orphan_rows = []
        for r in batch:
            row = {c: None for c in STAT_FEATURE_COLS}
            row["fixture_ref"] = r["fixture_id"]
            row["team_a"], row["team_b"] = r["team_a"], r["team_b"]
            fixture_odds = odds_map.get(r["fixture_id"], {})
            for (market, selection), field in ODDS_FIELD_MAP.items():
                row[field] = fixture_odds.get((market, selection))
            orphan_rows.append(row)

        summary["orphan_round_number"] = batch[0]["round_number"]
        summary["orphan_fixtures_predicted"] = len(orphan_rows)
        summary["orphan_predictions_written"] = _predict_rows(conn, predictor, orphan_rows)

    return summary
