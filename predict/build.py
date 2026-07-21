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
    rows = [dict(r) for r in conn.execute("SELECT * FROM features WHERE fixture_ref IS NOT NULL")]

    if rows:
        earliest_round = min(r["round_number"] for r in rows)
        target_rows = [r for r in rows if r["round_number"] == earliest_round]
        written = _predict_rows(conn, predictor, target_rows)
        return {
            "round_number": earliest_round,
            "fixtures_predicted": len(target_rows),
            "predictions_written": written,
        }

    # Nothing in `features` -- check for a season-boundary orphan batch: the
    # fixtures page already rolled over to a new season's Round 1, but that
    # season doesn't exist in our DB yet (won't, until its own Round 1
    # finishes and gets fingerprinted -- see run.py cmd_scrape). It has zero
    # history by definition (brand new season), so there's nothing for
    # features/build.py to compute from, and features.season_id is NOT NULL
    # in the schema -- predicted directly here from odds alone instead
    # (still valuable, especially for the baseline model's 1X2/BTTS) rather
    # than silently skipped until the season resolves.
    batch = get_current_fixture_batch(conn, None)
    if not batch:
        return {"round_number": None, "fixtures_predicted": 0, "predictions_written": 0}

    odds_map = get_latest_odds_by_fixture(conn, [r["fixture_id"] for r in batch])
    target_rows = []
    for r in batch:
        row = {c: None for c in STAT_FEATURE_COLS}
        row["fixture_ref"] = r["fixture_id"]
        row["team_a"], row["team_b"] = r["team_a"], r["team_b"]
        fixture_odds = odds_map.get(r["fixture_id"], {})
        for (market, selection), field in ODDS_FIELD_MAP.items():
            row[field] = fixture_odds.get((market, selection))
        target_rows.append(row)

    written = _predict_rows(conn, predictor, target_rows)
    return {
        "round_number": batch[0]["round_number"],
        "fixtures_predicted": len(target_rows),
        "predictions_written": written,
    }
