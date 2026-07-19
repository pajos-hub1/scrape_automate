"""DB orchestration for predictions: finds the earliest unplayed round
(the fixtures currently sitting in `features`), runs a Predictor over each
one, and upserts the results. Always runs before kickoff -- fixtures only
exist in the DB pre-match, so there's no way to accidentally predict
something already played.
"""
from db.upsert import upsert_prediction


def build_predictions(conn, predictor):
    rows = [dict(r) for r in conn.execute("SELECT * FROM features WHERE fixture_ref IS NOT NULL")]
    if not rows:
        return {"round_number": None, "fixtures_predicted": 0, "predictions_written": 0}

    earliest_round = min(r["round_number"] for r in rows)
    target_rows = [r for r in rows if r["round_number"] == earliest_round]

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

    return {
        "round_number": earliest_round,
        "fixtures_predicted": len(target_rows),
        "predictions_written": written,
    }
