"""Pulls plain dicts/lists out of SQLite for the dashboard to render --
kept separate from HTML generation so the query logic stays testable
without needing to touch markup.
"""
from datetime import datetime, timezone

from db.queries import get_current_fixture_batch
from track.report import accuracy_stats, list_model_versions


def get_meta(conn):
    seasons = conn.execute(
        "SELECT season_id, status, round_count, last_seen FROM seasons ORDER BY season_id"
    ).fetchall()
    current = next((s for s in seasons if s["status"] == "current"), None)
    previous = next((s for s in seasons if s["status"] == "previous"), None)
    archived_count = sum(1 for s in seasons if s["status"] == "archived")
    last_scraped = max((s["last_seen"] for s in seasons), default=None)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "current_round": current["round_count"] if current else None,
        "previous_complete": previous is not None,
        "seasons_tracked": len(seasons),
        "seasons_archived": archived_count,
        "last_scraped": last_scraped,
    }


def get_upcoming_predictions(conn):
    """Predictions with no prediction_results row yet, restricted to the
    current fixture batch (see db/queries.get_current_fixture_batch),
    across every model currently making live predictions.

    Reconciliation matches a prediction to its result by team pairing, not
    by our guessed round_number (see track/reconcile.py) -- a pairing only
    clears once it actually gets played, which can take more than one real
    round if the guess was off, or never if that exact poll's pairing was
    superseded before it ever played. That means older batches can still
    be sitting here unreconciled by the time a newer predict() run adds a
    fresh batch on top. Showing all of them at once looks like duplicate/
    doubled fixtures (the same team appearing twice against different
    opponents); the dashboard should only ever show the current one.
    """
    # season_id=None here is fine and intentional: get_current_fixture_batch
    # treats that as "look up the season-boundary orphan batch" -- the
    # fixtures page can roll over to a new season's Round 1 before that
    # season exists in our DB (see run.py cmd_scrape), and there may be no
    # 'current' season at all during that gap.
    current_season = conn.execute("SELECT season_id FROM seasons WHERE status = 'current'").fetchone()
    season_id = current_season["season_id"] if current_season else None
    batch = get_current_fixture_batch(conn, season_id)
    fixture_ids = [r["fixture_id"] for r in batch]
    if not fixture_ids:
        return []

    placeholders = ",".join("?" for _ in fixture_ids)
    rows = conn.execute(
        f"""SELECT p.fixture_ref, f.team_a, f.team_b, f.round_number, f.kickoff_time,
                   p.model_version, p.market, p.label, p.confidence
            FROM predictions p
            JOIN fixtures f ON f.fixture_id = p.fixture_ref
            LEFT JOIN prediction_results pr ON pr.prediction_id = p.prediction_id
            WHERE pr.result_id IS NULL AND p.fixture_ref IN ({placeholders})
            ORDER BY f.match_number, p.market, p.model_version""",
        fixture_ids,
    ).fetchall()

    fixtures = {}
    for r in rows:
        fx = fixtures.setdefault(r["fixture_ref"], {
            "team_a": r["team_a"], "team_b": r["team_b"],
            "round_number": r["round_number"], "kickoff_time": r["kickoff_time"],
            "markets": {},
        })
        fx["markets"].setdefault(r["market"], {})[r["model_version"]] = {
            "label": r["label"], "confidence": r["confidence"],
        }
    return list(fixtures.values())


def get_reconciled_round(conn, season_id, round_number):
    """Full predicted-vs-actual detail for one specific (season_id,
    round_number). Each market cell carries every live model's prediction
    separately. Scoped by season_id, not just round_number -- round numbers
    reset to 1 every season, so round_number alone can't distinguish
    "season 3's round 4" from "season 1's round 4."
    """
    rows = conn.execute(
        """SELECT f.team_a, f.team_b, m.ft_a, m.ft_b, m.ht_a, m.ht_b,
                  p.model_version, pr.market, pr.predicted_label, pr.actual_label, pr.correct
           FROM prediction_results pr
           JOIN predictions p ON p.prediction_id = pr.prediction_id
           JOIN fixtures f ON f.fixture_id = p.fixture_ref
           JOIN matches m ON m.match_id = pr.match_ref
           WHERE m.season_id = ? AND m.round_number = ?
           ORDER BY f.team_a, pr.market, p.model_version""",
        (season_id, round_number),
    ).fetchall()
    if not rows:
        return None

    matches = {}
    for r in rows:
        key = (r["team_a"], r["team_b"])
        m = matches.setdefault(key, {
            "team_a": r["team_a"], "team_b": r["team_b"],
            "ft_a": r["ft_a"], "ft_b": r["ft_b"], "ht_a": r["ht_a"], "ht_b": r["ht_b"],
            "markets": {},
        })
        m["markets"].setdefault(r["market"], {})[r["model_version"]] = {
            "predicted": r["predicted_label"], "actual": r["actual_label"],
            "correct": bool(r["correct"]),
        }
    return {"season_id": season_id, "round_number": round_number,
            "is_upcoming": False, "matches": list(matches.values())}


def get_round_history(conn, max_reconciled=20):
    """Chronological browsing sequence for the dashboard's </> round
    navigator: up to `max_reconciled` most recently reconciled rounds
    (oldest to newest), followed by the current upcoming batch if one
    exists (always last -- the default view).

    Ordered by (season_id, round_number), NOT reconciled_at -- reconciled_at
    reflects when a round's predictions were last WRITTEN, which isn't the
    same as true play order: a round can get its reconciliation row
    touched again later (e.g. a second model added after the fact,
    backfilling predictions for an already-played round), jumbling
    reconciled_at out of chronological order. season_id and round_number
    are both reliable, non-guessed values instead -- season_id is assigned
    in discovery order, and round_number for PLAYED matches comes from the
    authoritative results carousel, not our guess (that guessing only ever
    applies to the upcoming/unplayed fixture, handled separately below).
    """
    groups = conn.execute(
        """SELECT DISTINCT m.season_id, m.round_number
           FROM prediction_results pr JOIN matches m ON m.match_id = pr.match_ref
           ORDER BY m.season_id DESC, m.round_number DESC
           LIMIT ?""",
        (max_reconciled,),
    ).fetchall()
    groups = list(reversed(groups))

    history = []
    for g in groups:
        rr = get_reconciled_round(conn, g["season_id"], g["round_number"])
        if rr:
            history.append(rr)

    upcoming = get_upcoming_predictions(conn)
    if upcoming:
        history.append({
            "round_number": upcoming[0]["round_number"],
            "is_upcoming": True,
            "fixtures": upcoming,
        })
    return history


def get_accuracy_trend(conn, model_version=None):
    """Overall (all-markets-blended) accuracy per actual round, chronological.
    model_version=None blends every live model together.

    Grouped by (season_id, round_number), NOT round_number alone -- round
    numbers reset to 1 every season, so grouping by round_number alone
    would merge season 1's round 5 and season 3's round 5 into one point.
    The x-position returned is a synthetic sequential index (0, 1, 2, ...),
    not the real round_number, so points from different seasons can never
    collide on the chart; "label" carries the real season/round identity
    for the tooltip.
    """
    query = """SELECT m.season_id, m.round_number, COUNT(*) AS n, SUM(pr.correct) AS correct
               FROM prediction_results pr
               JOIN matches m ON m.match_id = pr.match_ref"""
    params = ()
    if model_version is not None:
        query += " JOIN predictions p ON p.prediction_id = pr.prediction_id WHERE p.model_version = ?"
        params = (model_version,)
    query += " GROUP BY m.season_id, m.round_number ORDER BY m.season_id, m.round_number"
    rows = conn.execute(query, params).fetchall()
    return [
        {"round_number": i, "label": f'S{r["season_id"]} R{r["round_number"]}',
         "n": r["n"], "accuracy": r["correct"] / r["n"]}
        for i, r in enumerate(rows)
    ]


def get_models_and_stats(conn):
    """{model_version: accuracy_stats(...)} for every model with live
    predictions -- the per-model comparison the dashboard's accuracy
    section renders one chart per model from."""
    return {m: accuracy_stats(conn, model_version=m) for m in list_model_versions(conn)}
