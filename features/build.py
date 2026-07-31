"""DB orchestration for feature engineering: pulls matches/fixtures/odds
out of SQLite, runs them through features/engineer.py, and upserts the
result into the `features` table.

Recomputes every season on every run rather than tracking what's already
built -- season sizes are tiny (<=380 rows), so a full recompute is cheap
and avoids a whole class of incremental-update bugs.
"""
import numpy as np
import pandas as pd

from db.queries import ODDS_FIELD_MAP, get_current_fixture_batch, get_latest_odds_by_fixture
from db.upsert import upsert_feature
from features.engineer import build_season_features

MATCH_QUERY = """
    SELECT match_id, round_number, match_number, team_a, team_b, ft_a, ft_b
    FROM matches WHERE season_id = ?
    ORDER BY round_number, match_number
"""

PRIOR_RATING_STATS = ["pts_rate", "gf_avg", "ga_avg"]
PRIOR_RATING_FALLBACK = {"pts_rate": 1.3, "gf_avg": 1.3, "ga_avg": 1.3}  # neutral prior, no completed season to draw on yet


def _team_season_rates(conn):
    """One row per (season_id, team) that has played at least one match in
    that season: pts_rate/gf_avg/ga_avg for that team in that season."""
    df = pd.read_sql_query(
        "SELECT season_id, team_a, team_b, ft_a, ft_b FROM matches WHERE ft_a IS NOT NULL", conn
    )
    if df.empty:
        return pd.DataFrame(columns=["season_id", "team"] + PRIOR_RATING_STATS)

    home = df[["season_id", "team_a", "ft_a", "ft_b"]].rename(columns={"team_a": "team", "ft_a": "gf", "ft_b": "ga"})
    away = df[["season_id", "team_b", "ft_b", "ft_a"]].rename(columns={"team_b": "team", "ft_b": "gf", "ft_a": "ga"})
    long_df = pd.concat([home, away], ignore_index=True)
    long_df["pts"] = np.select([long_df.gf > long_df.ga, long_df.gf == long_df.ga], [3, 1], default=0)

    return (
        long_df.groupby(["season_id", "team"])
        .agg(pts_rate=("pts", "mean"), gf_avg=("gf", "mean"), ga_avg=("ga", "mean"))
        .reset_index()
    )


def compute_prior_season_ratings(conn):
    """{(season_id, team): {"pts_rate", "gf_avg", "ga_avg"}}, one entry per
    team/season that has at least one STRICTLY EARLIER completed season to
    draw on -- an expanding mean over every prior season only (`.shift(1)`
    after `.expanding()`), so a season's own results never feed its own
    rating. Team quality correlates 0.76-0.88 season-to-season in this data
    (verified empirically against the live DB) -- teams keep a persistent
    identity here rather than being reshuffled each season, so this is real
    signal the season-scoped features below can't see at all, and is worth
    the most in early rounds before within-season form/season-rate features
    have enough games to mean much. (season_id, team) pairs with no prior
    season (all of season 1) get no entry -- callers fall back to
    PRIOR_RATING_FALLBACK.
    """
    per_season = _team_season_rates(conn).sort_values(["team", "season_id"])
    g = per_season.groupby("team", sort=False)
    for col in PRIOR_RATING_STATS:
        per_season[f"prior_{col}"] = g[col].transform(lambda s: s.expanding().mean().shift(1))
    per_season = per_season.dropna(subset=["prior_pts_rate"])

    return {
        (row.season_id, row.team): {col: getattr(row, f"prior_{col}") for col in PRIOR_RATING_STATS}
        for row in per_season.itertuples()
    }


def compute_latest_team_ratings(conn):
    """{team: {"pts_rate", "gf_avg", "ga_avg"}} -- each team's mean rating
    across EVERY completed season to date, no shift needed since this is
    only for a brand-new, not-yet-fingerprinted season (predict/build.py's
    orphan-batch path), which by definition postdates every season already
    in the database, so nothing here can leak into it.
    """
    per_season = _team_season_rates(conn)
    if per_season.empty:
        return {}
    agg = per_season.groupby("team").agg(**{c: (c, "mean") for c in PRIOR_RATING_STATS})
    return {team: {c: row[c] for c in PRIOR_RATING_STATS} for team, row in agg.iterrows()}


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

    prior_ratings = compute_prior_season_ratings(conn)

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
            #
            # include_orphan=False is required here: this batch gets appended
            # onto THIS season's own match history as a virtual next round,
            # and an orphan batch is always labeled round_number=1 -- if this
            # season is itself sitting at its own finale (still 'current'
            # while a new, not-yet-fingerprinted season's orphan batch
            # already exists), this season already has a real, played Round
            # 1 of its own. Including the orphan here would collide every
            # team at round_number=1 twice and crash build_season_features.
            # predict/build.py's separate orphan-batch path handles it
            # instead, without ever mixing in a season's real history.
            batch = get_current_fixture_batch(conn, season_id, include_orphan=False)
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

        # Cross-season team-strength prior (see compute_prior_season_ratings) --
        # attached here rather than inside build_season_features since it needs
        # cross-season DB access that function deliberately doesn't have.
        for prefix, team_col in [("a", "team_a"), ("b", "team_b")]:
            for stat in PRIOR_RATING_STATS:
                feat[f"{prefix}_prior_{stat}"] = [
                    prior_ratings.get((season_id, t), {}).get(stat, PRIOR_RATING_FALLBACK[stat])
                    for t in feat[team_col]
                ]

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
