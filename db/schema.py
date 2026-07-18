"""SQLite schema for the Zoom prediction pipeline.

Tables (per the project spec):
    seasons, matches, fixtures, odds, features, predictions, prediction_results

All natural keys carry UNIQUE constraints so every write in db/upsert.py can
use INSERT OR IGNORE / upsert-on-conflict and be safe to re-run every ~90
minutes without creating duplicates.
"""

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS seasons (
    season_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT UNIQUE NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('current', 'previous', 'archived')),
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    round_count  INTEGER NOT NULL DEFAULT 0
);

-- Results: source of truth for played matches.
CREATE TABLE IF NOT EXISTS matches (
    match_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id     INTEGER NOT NULL REFERENCES seasons(season_id),
    round_number  INTEGER NOT NULL,
    match_number  INTEGER NOT NULL,
    team_a        TEXT NOT NULL,
    team_b        TEXT NOT NULL,
    ft_a          INTEGER,
    ft_b          INTEGER,
    ht_a          INTEGER,
    ht_b          INTEGER,
    played_at     TEXT,
    scraped_at    TEXT NOT NULL,
    UNIQUE (season_id, round_number, match_number)
);

CREATE INDEX IF NOT EXISTS idx_matches_teams ON matches(team_a, team_b);
CREATE INDEX IF NOT EXISTS idx_matches_season_round ON matches(season_id, round_number);

-- Upcoming, unplayed fixtures -- prediction targets.
-- season_id is nullable: at scrape time we tag it with whichever season is
-- currently marked 'current' in the seasons table, but that link may not
-- be resolved yet on the very first run. fixture_key is the durable
-- natural key that keeps writes idempotent regardless.
CREATE TABLE IF NOT EXISTS fixtures (
    fixture_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id     INTEGER REFERENCES seasons(season_id),
    round_number  INTEGER NOT NULL,
    match_number  INTEGER NOT NULL,
    team_a        TEXT NOT NULL,
    team_b        TEXT NOT NULL,
    kickoff_time  TEXT,
    scraped_at    TEXT NOT NULL,
    fixture_key   TEXT UNIQUE NOT NULL
);

-- Odds move over time, so this is an append-only time series keyed by
-- capture. upsert.py skips inserting a new row when the price hasn't
-- changed since the last capture for that fixture/market/selection, so
-- idempotent re-runs don't bloat the table with unchanged odds.
CREATE TABLE IF NOT EXISTS odds (
    odds_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id     INTEGER NOT NULL REFERENCES fixtures(fixture_id),
    market         TEXT NOT NULL,
    selection      TEXT NOT NULL,
    price          REAL NOT NULL,
    implied_prob   REAL NOT NULL,
    captured_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_odds_fixture ON odds(fixture_id, market, selection);

-- One row per match/fixture. Leakage-safe: only ever computed from rounds
-- strictly before the target match/fixture's round.
CREATE TABLE IF NOT EXISTS features (
    feature_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    match_ref      INTEGER REFERENCES matches(match_id),
    fixture_ref    INTEGER REFERENCES fixtures(fixture_id),
    season_id      INTEGER NOT NULL REFERENCES seasons(season_id),
    round_number   INTEGER NOT NULL,
    team_a         TEXT NOT NULL,
    team_b         TEXT NOT NULL,

    -- Form, last 5 matches strictly before this round.
    a_form_games   INTEGER, a_form_pts INTEGER, a_form_wins INTEGER,
    a_form_draws   INTEGER, a_form_losses INTEGER,
    a_form_gf      REAL, a_form_ga REAL,
    b_form_games   INTEGER, b_form_pts INTEGER, b_form_wins INTEGER,
    b_form_draws   INTEGER, b_form_losses INTEGER,
    b_form_gf      REAL, b_form_ga REAL,

    -- Head-to-head, all prior meetings regardless of venue.
    h2h_games      INTEGER, h2h_a_wins INTEGER, h2h_b_wins INTEGER,
    h2h_draws      INTEGER, h2h_avg_goals REAL,

    -- Home/away splits, season-to-date, strictly prior rounds.
    a_home_played  INTEGER, a_home_wins INTEGER, a_home_draws INTEGER,
    a_home_losses  INTEGER, a_home_gf REAL, a_home_ga REAL,
    b_away_played  INTEGER, b_away_wins INTEGER, b_away_draws INTEGER,
    b_away_losses  INTEGER, b_away_gf REAL, b_away_ga REAL,

    -- Season-to-date rates (all matches so far, not just last 5).
    a_season_pts_rate REAL, a_season_gf_avg REAL, a_season_ga_avg REAL,
    b_season_pts_rate REAL, b_season_gf_avg REAL, b_season_ga_avg REAL,

    -- Odds-implied probabilities, captured closest to feature computation time.
    odds_home_prob REAL, odds_draw_prob REAL, odds_away_prob REAL,
    odds_over25_prob REAL, odds_under25_prob REAL,
    odds_btts_yes_prob REAL, odds_btts_no_prob REAL,

    computed_at    TEXT NOT NULL,
    UNIQUE (match_ref),
    UNIQUE (fixture_ref)
);

-- Always written before kickoff. One row per (fixture, model_version, market).
CREATE TABLE IF NOT EXISTS predictions (
    prediction_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_ref     INTEGER NOT NULL REFERENCES fixtures(fixture_id),
    model_version   TEXT NOT NULL,
    market          TEXT NOT NULL,   -- '1X2', 'OU2.5', 'BTTS', ...
    label           TEXT NOT NULL,   -- predicted outcome, e.g. 'Home', 'Over', 'Yes'
    probabilities   TEXT NOT NULL,   -- JSON object, e.g. {"Home":0.42,"Draw":0.30,"Away":0.28}
    confidence      REAL NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (fixture_ref, model_version, market)
);

-- Predictions joined to actuals once the round completes.
CREATE TABLE IF NOT EXISTS prediction_results (
    result_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id         INTEGER NOT NULL REFERENCES predictions(prediction_id),
    match_ref              INTEGER NOT NULL REFERENCES matches(match_id),
    market                TEXT NOT NULL,
    predicted_label       TEXT NOT NULL,
    actual_label          TEXT NOT NULL,
    correct               INTEGER NOT NULL,   -- 0/1

    baseline_label         TEXT,
    baseline_correct       INTEGER,
    odds_implied_label     TEXT,
    odds_implied_correct   INTEGER,

    reconciled_at         TEXT NOT NULL,
    UNIQUE (prediction_id)
);
"""


def init_db(conn):
    conn.executescript(SCHEMA_SQL)
    conn.commit()
