"""DB orchestration for feature engineering: pulls matches/fixtures/odds
out of SQLite, runs them through features/engineer.py, and upserts the
result into the `features` table.

Recomputes every season on every run rather than tracking what's already
built -- season sizes are tiny (<=380 rows), so a full recompute is cheap
and avoids a whole class of incremental-update bugs.
"""
import pandas as pd

from db.upsert import upsert_feature
from features.engineer import build_season_features

MATCH_QUERY = """
    SELECT match_id, round_number, match_number, team_a, team_b, ft_a, ft_b
    FROM matches WHERE season_id = ?
    ORDER BY round_number, match_number
"""
FIXTURE_QUERY = """
    SELECT fixture_id, round_number, match_number, team_a, team_b
    FROM fixtures WHERE season_id = ? AND round_number > ?
    ORDER BY round_number, match_number
"""


def _latest_odds_by_fixture(conn, fixture_ids):
    if not fixture_ids:
        return {}
    placeholders = ",".join("?" for _ in fixture_ids)
    rows = conn.execute(
        f"""SELECT o.fixture_id, o.market, o.selection, o.implied_prob
            FROM odds o
            JOIN (
                SELECT fixture_id, market, selection, MAX(captured_at) AS latest
                FROM odds WHERE fixture_id IN ({placeholders})
                GROUP BY fixture_id, market, selection
            ) latest_o
            ON o.fixture_id = latest_o.fixture_id AND o.market = latest_o.market
               AND o.selection = latest_o.selection AND o.captured_at = latest_o.latest""",
        fixture_ids,
    ).fetchall()

    odds_map = {}
    for r in rows:
        odds_map.setdefault(r["fixture_id"], {})[(r["market"], r["selection"])] = r["implied_prob"]
    return odds_map


ODDS_FIELD_MAP = {
    ("1X2", "Home"): "odds_home_prob",
    ("1X2", "Draw"): "odds_draw_prob",
    ("1X2", "Away"): "odds_away_prob",
    ("BTTS", "Yes"): "odds_btts_yes_prob",
    ("BTTS", "No"): "odds_btts_no_prob",
    ("OU2.5", "Over"): "odds_over25_prob",
    ("OU2.5", "Under"): "odds_under25_prob",
}


def build_features(conn):
    """Returns a summary dict: rows written for matches vs fixtures, per season."""
    summary = {"seasons_processed": 0, "match_features_written": 0, "fixture_features_written": 0}

    seasons = conn.execute("SELECT season_id, status FROM seasons").fetchall()

    for season in seasons:
        season_id, status = season["season_id"], season["status"]
        matches = pd.read_sql_query(MATCH_QUERY, conn, params=(season_id,))
        if matches.empty:
            continue
        summary["seasons_processed"] += 1

        fixtures = None
        if status == "current":
            # fixtures accumulates every fixture ever scraped (by design, for
            # idempotency) -- once its round is played, it's superseded by real
            # match data at that same round_number, so only rounds strictly
            # after the latest played one are still genuinely upcoming.
            max_played_round = int(matches["round_number"].max())
            fx = pd.read_sql_query(FIXTURE_QUERY, conn, params=(season_id, max_played_round))
            fixtures = fx if not fx.empty else None

            # Prune feature rows left over from fixtures whose round has since
            # been played -- otherwise they'd sit in `features` forever with
            # fixture_ref still set, and predict/build.py's "earliest unplayed
            # round" query would keep finding them.
            conn.execute(
                """DELETE FROM features WHERE fixture_ref IN (
                       SELECT fixture_id FROM fixtures WHERE season_id = ? AND round_number <= ?
                   )""",
                (season_id, max_played_round),
            )

        feat = build_season_features(matches, fixture_rows=fixtures)

        odds_map = {}
        if fixtures is not None:
            odds_map = _latest_odds_by_fixture(conn, fixtures["fixture_id"].tolist())

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
