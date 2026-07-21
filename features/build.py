"""DB orchestration for feature engineering: pulls matches/fixtures/odds
out of SQLite, runs them through features/engineer.py, and upserts the
result into the `features` table.

Recomputes every season on every run rather than tracking what's already
built -- season sizes are tiny (<=380 rows), so a full recompute is cheap
and avoids a whole class of incremental-update bugs.
"""
import pandas as pd

from db.queries import ODDS_FIELD_MAP, get_current_fixture_batch, get_latest_odds_by_fixture
from db.upsert import upsert_feature
from features.engineer import build_season_features

MATCH_QUERY = """
    SELECT match_id, round_number, match_number, team_a, team_b, ft_a, ft_b
    FROM matches WHERE season_id = ?
    ORDER BY round_number, match_number
"""


def build_features(conn):
    """Returns a summary dict: rows written for matches vs fixtures, per season."""
    summary = {"seasons_processed": 0, "match_features_written": 0, "fixture_features_written": 0}

    seasons = conn.execute("SELECT season_id, status FROM seasons").fetchall()

    # Any fixture-linked feature row belonging to a season that ISN'T
    # currently 'current' is definitionally stale -- fixtures only ever
    # make sense for whichever season is presently active (or is
    # unattached, season_id NULL, handled separately by predict/build.py's
    # orphan path). Without this, a season's leftover rows from while it
    # WAS current never get cleaned up once it moves on to
    # 'previous'/'archived', and permanently block predict/build.py's "is
    # there anything in `features` at all" check from ever falling through
    # to the orphan-batch path.
    non_current_ids = [s["season_id"] for s in seasons if s["status"] != "current"]
    if non_current_ids:
        placeholders = ",".join("?" for _ in non_current_ids)
        conn.execute(
            f"""DELETE FROM features WHERE fixture_ref IN (
                    SELECT fixture_id FROM fixtures WHERE season_id IN ({placeholders})
                )""",
            non_current_ids,
        )

    for season in seasons:
        season_id, status = season["season_id"], season["status"]
        matches = pd.read_sql_query(MATCH_QUERY, conn, params=(season_id,))
        if matches.empty:
            continue
        summary["seasons_processed"] += 1

        fixtures = None
        if status == "current":
            # fixtures accumulates every fixture ever scraped (by design, for
            # idempotency) -- get_current_fixture_batch picks out just the
            # single freshest poll's batch (see db/queries.py for why
            # fixture_id-recency, not round_number, is what identifies it).
            batch = get_current_fixture_batch(conn, season_id)
            current_ids = [r["fixture_id"] for r in batch]
            if batch:
                fixtures = pd.DataFrame(batch)[["fixture_id", "round_number", "match_number", "team_a", "team_b"]]

            # Prune feature rows for any fixture that's no longer the current
            # batch -- covers both "round has since been played" AND "this
            # exact pairing was superseded by a fresher poll before it ever
            # got played" -- otherwise stale rows sit in `features` forever
            # with fixture_ref still set, and predict/build.py's "earliest
            # unplayed round" query would keep finding them alongside the
            # real current batch.
            all_ids = [r["fixture_id"] for r in conn.execute(
                "SELECT fixture_id FROM fixtures WHERE season_id = ?", (season_id,)
            )]
            stale_ids = [fid for fid in all_ids if fid not in current_ids]
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                conn.execute(f"DELETE FROM features WHERE fixture_ref IN ({placeholders})", stale_ids)

        feat = build_season_features(matches, fixture_rows=fixtures)

        odds_map = {}
        if fixtures is not None:
            odds_map = get_latest_odds_by_fixture(conn, fixtures["fixture_id"].tolist())

        for _, row in feat.iterrows():
            payload = row.to_dict()
            payload["season_id"] = season_id
            payload["match_ref"] = int(payload["match_id"]) if pd.notna(payload["match_id"]) else None
            payload["fixture_ref"] = int(payload["fixture_id"]) if pd.notna(payload["fixture_id"]) else None

            if payload["fixture_ref"] is not None:
                fixture_odds = odds_map.get(payload["fixture_ref"], {})
                for (market, selection), field in ODDS_FIELD_MAP.items():
                    payload[field] = fixture_odds.get((market, selection))

            for k, v in list(payload.items()):
                if isinstance(v, float) and pd.isna(v):
                    payload[k] = None

            upsert_feature(conn, payload)

            if payload["match_ref"] is not None:
                summary["match_features_written"] += 1
            else:
                summary["fixture_features_written"] += 1

    return summary
