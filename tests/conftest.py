"""Shared fixtures: a throwaway pipeline DB + archive dir per test."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db.archive as archive_mod  # noqa: E402
from db.connection import get_connection  # noqa: E402

OLD = "2026-01-01T00:00:00+00:00"   # well past ARCHIVE_KEEP_DAYS


@pytest.fixture
def env(tmp_path, monkeypatch):
    """(db_path, archive_dir) with ARCHIVE_DIR redirected into tmp_path."""
    archive_dir = tmp_path / "archive"
    monkeypatch.setattr(archive_mod, "ARCHIVE_DIR", archive_dir)
    return tmp_path / "zoom.db", archive_dir


def add_played_fixture(conn, n, home_goals, away_goals, *, model="baseline_v0",
                       picks=None, prices=None, ts=OLD):
    """One season-1 match + its fixture, 1X2 odds, and reconciled 1X2
    predictions. picks: {model: label}. prices: {selection: decimal price}."""
    conn.execute(
        "INSERT OR IGNORE INTO seasons (season_id, fingerprint, status, first_seen, last_seen, round_count) "
        "VALUES (1, 'fp1', 'current', ?, ?, 38)", (ts, ts))
    cur = conn.execute(
        "INSERT INTO matches (season_id, round_number, match_number, team_a, team_b, ft_a, ft_b, ht_a, ht_b, scraped_at) "
        "VALUES (1, 1, ?, ?, ?, ?, ?, 0, 0, ?)", (n, f"A{n}", f"B{n}", home_goals, away_goals, ts))
    match_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO fixtures (season_id, round_number, match_number, team_a, team_b, scraped_at, fixture_key) "
        "VALUES (1, 1, ?, ?, ?, ?, ?)", (n, f"A{n}", f"B{n}", ts, f"key{n}"))
    fixture_id = cur.lastrowid
    prices = prices or {"Home": 2.0, "Draw": 3.5, "Away": 4.0}
    for sel, price in prices.items():
        conn.execute(
            "INSERT INTO odds (fixture_id, market, selection, price, implied_prob, captured_at) "
            "VALUES (?, '1X2', ?, ?, ?, ?)", (fixture_id, sel, price, round(1 / price, 4), ts))
    actual = "Home" if home_goals > away_goals else "Away" if home_goals < away_goals else "Draw"
    fav = min(prices, key=prices.get)
    for mv, label in (picks or {model: "Home"}).items():
        probs = {"Home": 0.5, "Draw": 0.3, "Away": 0.2}
        cur = conn.execute(
            "INSERT INTO predictions (fixture_ref, model_version, market, label, probabilities, confidence, created_at) "
            "VALUES (?, ?, '1X2', ?, ?, 0.5, ?)", (fixture_id, mv, label, json.dumps(probs), ts))
        conn.execute(
            "INSERT INTO prediction_results (prediction_id, match_ref, market, predicted_label, actual_label, correct, "
            "baseline_label, baseline_correct, odds_implied_label, odds_implied_correct, reconciled_at) "
            "VALUES (?, ?, '1X2', ?, ?, ?, 'Home', ?, ?, ?, ?)",
            (cur.lastrowid, match_id, label, actual, int(label == actual), int(actual == "Home"),
             fav, int(fav == actual), ts))
    return fixture_id


@pytest.fixture
def populated(env):
    """DB with 4 reconciled, old fixtures (3 home wins, 1 away win)."""
    db_path, _ = env
    with get_connection(db_path) as conn:
        for n, (h, a) in enumerate([(2, 0), (1, 0), (0, 1), (3, 1)], start=1):
            add_played_fixture(conn, n, h, a)
    return db_path
