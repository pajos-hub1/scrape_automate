"""scraper/zoom_api.py and the API-based scrape, with bet9ja's responses
faked -- the tests never touch the network."""
import pytest

import scraper.zoom_api as z
from db.connection import get_connection
from db.upsert import upsert_fixture
from track.reconcile import reconcile


def test_map_odds_matches_database_names():
    raw = {"VS_1X2_1": "1.33", "VS_1X2_X": "5.3", "VS_1X2_2": "8.5", "VS_GGNG_GG": "2.06", "VS_GGNG_NG": "1.76",
           "VS_OU@2.5_O": "1.65", "VS_OU@2.5_U": "2.24", "VS_HTOU@0.5_OverHT": "1.34", "VS_HTOU@0.5_UnderHT": "3.25",
           "VS_1X21T_X": "2.37", "VS_CS_2:1": "9.5", "VS_CS_Other": "120", "VS_HTFT_1/X": "15",
           "VS_1X2CH_1CHT1": "2.0", "VS_OUC@9.5_CO": "1.9", "VS_DC_1X": "1.08", "VS_12OU@0.5_AO": "3"}
    out = z.map_odds(raw)
    assert out["1X2"] == {"Home": 1.33, "Draw": 5.3, "Away": 8.5}
    assert out["BTTS"] == {"Yes": 2.06, "No": 1.76}
    assert out["OU2.5"] == {"Over": 1.65, "Under": 2.24}
    assert out["HT_OU0.5"] == {"Over": 1.34, "Under": 3.25}
    assert out["HT_1X2"] == {"Draw": 2.37}
    assert out["CorrectScore"] == {"2-1": 9.5, "Other": 120.0}
    assert out["HTFT"] == {"1/X": 15.0}
    assert out["HT_CORNER_1X2"] == {"Home": 2.0}
    assert out["CORNER_OU9.5"] == {"Over": 1.9}
    assert "DC" not in str(out) and len(out) == 9          # unmapped families ignored


def test_place_in_season():
    assert z.place_in_season(4267718, 4267407, 10, 38) == (32, 2)
    assert z.place_in_season(4267407, 4267407, 10, 38) == (1, 1)
    assert z.place_in_season(4267407 + 380, 4267407, 10, 38) is None   # next season's game
    assert z.place_in_season(5, None, 10, 38) is None


def _round(rid, first, n=10, base_team=1000):
    return {"id": rid, "time": f"21-10-2026 {rid:02d}:00:00", "matches": [
        {"matchId": first + k, "homeTeam": base_team + 2 * k, "awayTeam": base_team + 2 * k + 1,
         "homeScore": k % 3, "awayScore": 1, "halfTimeHomeScore": 0, "halfTimeAwayScore": k % 2} for k in reversed(range(n))]}


def test_season_results_pages_and_orders_by_id(monkeypatch):
    pages = {0: [_round(r, 100 + (r - 1) * 10) for r in range(1, 21)], 20: [_round(r, 100 + (r - 1) * 10) for r in range(21, 24)]}
    calls = []
    def fake(path, params=None, data=None):
        calls.append(params["offset"])
        return {"status": 1, "data": {"rounds": pages[params["offset"]]}}
    monkeypatch.setattr(z, "_results_api", fake)
    teams = {1000 + i: f"T{i}" for i in range(20)}
    s = z.season_results(1, teams)
    assert calls == [0, 20] and len(s["rounds"]) == 23
    assert s["first_id"] == 100 and s["games_per_round"] == 10
    assert [m["ext_match_id"] for m in s["rounds"][1]][:3] == [100, 101, 102]          # id order = slot order
    assert s["rounds"][1][0] == {"ext_match_id": 100, "match_number": 1, "team_a": "T0", "team_b": "T1",
                                 "ft_a": 0, "ft_b": 1, "ht_a": 0, "ht_b": 0}


def test_season_results_empty_season(monkeypatch):
    monkeypatch.setattr(z, "_results_api", lambda *a, **k: {"status": -1, "error": {"code": -24122}})
    assert z.season_results(1, {})["first_id"] is None


def test_upsert_fixture_by_ext_id_and_adoption(env):
    with get_connection(env[0]) as conn:
        old, _ = upsert_fixture(conn, None, 32, 1, "Arsenal", "Sunderland", scraped_at="2026-10-23T19:44:26+00:00")
        adopted, was_new = upsert_fixture(conn, None, 32, 2, "Arsenal", "Sunderland",
                                          scraped_at="2026-10-23T19:49:41+00:00", ext_match_id=4267718)
        assert adopted == old and not was_new
        assert conn.execute("SELECT match_number, ext_match_id FROM fixtures WHERE fixture_id = ?", (old,)).fetchone()[:] == (2, 4267718)
        again, was_new = upsert_fixture(conn, None, 32, 2, "Arsenal", "Sunderland",
                                        scraped_at="2026-10-23T20:04:41+00:00", ext_match_id=4267718)
        assert again == old and not was_new


def test_reconcile_joins_by_ext_id_even_with_wrong_tags(env):
    with get_connection(env[0]) as conn:
        conn.execute("INSERT INTO seasons (season_id, fingerprint, status, first_seen, last_seen, round_count) "
                     "VALUES (33, 'fp', 'current', '2026-10-21T00:00:00+00:00', '2026-10-23T00:00:00+00:00', 30)")
        mid = conn.execute("""INSERT INTO matches (season_id, round_number, match_number, team_a, team_b, ft_a, ft_b, ht_a, ht_b,
                              scraped_at, ext_match_id) VALUES (33, 32, 2, 'Arsenal', 'Sunderland', 0, 0, 0, 0,
                              '2026-10-23T22:00:00+00:00', 4267718)""").lastrowid
        fx = conn.execute("""INSERT INTO fixtures (season_id, round_number, match_number, team_a, team_b, scraped_at, fixture_key, ext_match_id)
                             VALUES (NULL, 1, 9, 'Arsenal', 'Sunderland', '2026-10-01T00:00:00+00:00', 'k', 4267718)""").lastrowid
        conn.execute("""INSERT INTO predictions (fixture_ref, model_version, market, label, probabilities, confidence, created_at)
                        VALUES (?, 'baseline_v0', '1X2', 'Draw', '{"Home":0.5,"Draw":0.3,"Away":0.2}', 0.3, 'x')""", (fx,))
        assert reconcile(conn)["reconciled"] == 1
        assert conn.execute("SELECT match_ref, actual_label FROM prediction_results").fetchone()[:] == (mid, "Draw")


def test_scrape_falls_back_to_browser_when_api_fails(monkeypatch):
    import run
    used = []
    monkeypatch.setattr(run.zoom_api, "competitions", lambda: (_ for _ in ()).throw(z.ZoomApiError("down")))
    monkeypatch.setattr(run, "_scrape_via_browser", lambda args: used.append("browser"))
    run.cmd_scrape(type("A", (), {"headed": False, "browser": False})())
    assert used == ["browser"]


def test_leagues_collector(env, monkeypatch):
    import track.leagues as lg
    import track.leagues
    monkeypatch.setattr(track.leagues, "get_connection", lambda: get_connection(env[0]))
    monkeypatch.setattr(lg.zoom_api, "competitions", lambda: {3: {"name": "Bundes-Zoom", "teams": {}}})
    monkeypatch.setattr(lg.zoom_api, "league_groups", lambda: {"Bundes-Zoom": 567120})
    season = {"first_id": 500, "games_per_round": 9, "round_times": {1: "t"}, "rounds": {1: [
        {"ext_match_id": 500 + k, "match_number": k + 1, "team_a": f"A{k}", "team_b": f"B{k}",
         "ft_a": 1, "ft_b": 0, "ht_a": 0, "ht_b": 0} for k in range(9)]}}
    monkeypatch.setattr(lg.zoom_api, "season_results", lambda cid, teams, previous=False: season if not previous else
                        {"first_id": None, "rounds": {}, "games_per_round": None, "round_times": {}})
    monkeypatch.setattr(lg.zoom_api, "upcoming_events", lambda gid: [{"event_id": 1, "ext_match_id": 509, "team_a": "A",
                                                                       "team_b": "B", "kickoff_time": "k"}])
    odds_calls = []
    monkeypatch.setattr(lg.zoom_api, "event_odds", lambda eid: odds_calls.append(eid) or
                        {"1X2": {"Home": 1.5, "Draw": 4.0, "Away": 6.0}, "HTFT": {"1/1": 2.5}})
    s1 = lg.collect_leagues()
    s2 = lg.collect_leagues()
    assert s1["new_results"] == 9 and s1["new_events"] == 1 and s1["odds_rows"] == 3      # HTFT not an analysis market
    assert s2["new_results"] == 0 and s2["new_events"] == 0 and odds_calls == [1]         # odds captured once
