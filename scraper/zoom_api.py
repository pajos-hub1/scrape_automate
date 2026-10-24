"""Bet9ja Zoom data straight from the JSON APIs the site's own pages use --
no browser. Found 2026-10-23 by recording the pages' network calls:

Results app (zoomapi.bet9ja.com/zoomexternalapi), needs a `clientId` header:
  POST /Competition/Init              -> the 8 Zoom competitions, each with
                                         its competitors (team id -> name)
  GET  /SeasonResult/Results          -> finished rounds of the current
       ?competitionId=&previous=0|1      (previous=0) or previous (1) season;
       &offset=<page start>&limit=<n>    `offset` pages through rounds
Sportsbook (sports.bet9ja.com/mobile/feapi/PalimpsestAjax):
  GetSports                           -> group id per league ("Premier-Zoom")
  GetEventsInGroupV2?GROUPID=         -> the league's upcoming games; EXTID
                                         "ZM_<matchId>_1" carries the same
                                         matchId the results API uses
  GetEvent?EVENTID=                   -> every odds line for one game

Within a league's season, matchIds run consecutively, one per game in round
order (round r, slot k -> R1's first id + (r-1)*games_per_round + k-1), so an
upcoming game's round and slot are exact rather than inferred. IDs do NOT
continue across seasons: an id past the current season's range is a new
season's game.
"""
import gzip
import json
import re
import time
import urllib.error
import urllib.request

RESULTS_API = "https://zoomapi.bet9ja.com/zoomexternalapi"
SPORTSBOOK_API = "https://sports.bet9ja.com/mobile/feapi/PalimpsestAjax"
CLIENT_ID = "68"
USER_AGENT = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")
PAGE = 20          # rounds per results request (the site's own page size)
TIMEOUT = 30


class ZoomApiError(RuntimeError):
    pass


def _request(url, data=None, headers=None, retries=2):
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=hdrs), timeout=TIMEOUT) as r:
                raw = r.read()
            if raw[:2] == b"\x1f\x8b":            # the results API gzips even unasked
                raw = gzip.decompress(raw)
            return json.loads(raw)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            if attempt == retries:
                raise ZoomApiError(f"{url.split('?')[0]}: {e}") from e
            time.sleep(2 * (attempt + 1))


def _results_api(path, params=None, data=None):
    url = f"{RESULTS_API}{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    return _request(url, data=data, headers={"clientId": CLIENT_ID})


def competitions():
    """{competition_id: {"name": "Premier-Zoom", "teams": {team_id: "Z.Arsenal"}}}"""
    d = _results_api("/Competition/Init", data={"clientId": CLIENT_ID, "isShop": 0})
    if d.get("status") != 1:
        raise ZoomApiError(f"Competition/Init: {d.get('error')}")
    return {c["id"]: {"name": c["name"], "teams": {t["id"]: t["name"] for t in c.get("competitors", [])}}
            for c in d["data"]["competitions"]}


def season_results(competition_id, teams, previous=False):
    """Every finished round of a league's current (or previous) season:
    {"rounds": {round_no: [match, ...]}, "first_id": R1's first matchId,
     "games_per_round": n, "round_times": {round_no: "dd-mm-yyyy hh:mm:ss"}}
    Each match: ext_match_id, match_number, team_a, team_b, ft_a, ft_b,
    ht_a, ht_b. Empty rounds ({}) when the season has no finished matches."""
    rounds, times, offset = {}, {}, 0
    while True:
        d = _results_api("/SeasonResult/Results", params={
            "clientId": CLIENT_ID, "competitionId": competition_id,
            "previous": 1 if previous else 0, "offset": offset, "limit": PAGE})
        if d.get("status") != 1:
            if (d.get("error") or {}).get("code") == -24122:      # "There are no finished matches."
                break
            raise ZoomApiError(f"SeasonResult/Results {competition_id}: {d.get('error')}")
        page = d["data"]["rounds"]
        for r in page:
            ms = sorted(r["matches"], key=lambda m: m["matchId"])
            rounds[r["id"]] = [{
                "ext_match_id": m["matchId"], "match_number": i,
                "team_a": teams.get(m["homeTeam"], f"#{m['homeTeam']}"),
                "team_b": teams.get(m["awayTeam"], f"#{m['awayTeam']}"),
                "ft_a": m["homeScore"], "ft_b": m["awayScore"],
                "ht_a": m["halfTimeHomeScore"], "ht_b": m["halfTimeAwayScore"],
            } for i, m in enumerate(ms, start=1)]
            times[r["id"]] = r.get("time")
        if len(page) < PAGE:
            break
        offset += PAGE
    first_id = min((m["ext_match_id"] for m in rounds.get(1, [])), default=None)
    gpr = len(rounds[1]) if 1 in rounds else None
    return {"rounds": rounds, "first_id": first_id, "games_per_round": gpr, "round_times": times}


def league_groups():
    """{"Premier-Zoom": sportsbook group id, ...} for every Zoom league."""
    d = _request(f"{SPORTSBOOK_API}/GetSports?DISP=0")
    pal = d["D"]["PAL"]
    sport = next((s for s in pal.values() if s.get("S_DESC") == "Zoom Soccer"), None)
    if sport is None:
        raise ZoomApiError("GetSports: no 'Zoom Soccer' sport")
    out = {}
    for sg in sport.get("SG", {}).values():
        for gid, g in sg.get("G", {}).items():
            out[g.get("G_DESC") or sg.get("SG_DESC")] = int(gid)
    return out


def upcoming_events(group_id):
    """The league's upcoming games: [{event_id, ext_match_id, team_a, team_b, kickoff_time}]"""
    d = _request(f"{SPORTSBOOK_API}/GetEventsInGroupV2?GROUPID={group_id}&GROUPMARKETID=93&DISP=0&matches=false&LIMIT=30")
    out = []
    for e in (d.get("D") or {}).get("E", []):
        m = re.match(r"ZM_(\d+)_", e.get("EXTID") or "")
        if not m or " - " not in e.get("DS", ""):
            continue
        a, b = e["DS"].split(" - ", 1)
        out.append({"event_id": e["ID"], "ext_match_id": int(m.group(1)), "team_a": a.strip(),
                    "team_b": b.strip(), "kickoff_time": e.get("STARTDATEUTC") or e.get("STARTDATE")})
    return sorted(out, key=lambda e: e["ext_match_id"])


def event_odds(event_id):
    """Every odds line for one game, as {our market: {selection: price}}."""
    d = _request(f"{SPORTSBOOK_API}/GetEvent?EVENTID={event_id}&ISCODE=0")
    return map_odds((d.get("D") or {}).get("O") or {})


_ONEX2 = {"1": "Home", "X": "Draw", "2": "Away"}
_FLAT = {
    "VS_1X2": ("1X2", _ONEX2),
    "VS_1X21T": ("HT_1X2", _ONEX2),
    "VS_1X2C": ("CORNER_1X2", _ONEX2),
    "VS_1X2CH": ("HT_CORNER_1X2", {"1CHT1": "Home", "1CHTX": "Draw", "1CHT2": "Away"}),
    "VS_GGNG": ("BTTS", {"GG": "Yes", "NG": "No"}),
    "VS_HTFT": ("HTFT", None),                       # selections already "1/X" etc.
    "VS_CS": ("CorrectScore", "score"),              # "2:1" -> "2-1", "Other"
}
_LINES = {
    "VS_OU": ("OU", {"O": "Over", "U": "Under"}),
    "VS_HTOU": ("HT_OU", {"OverHT": "Over", "UnderHT": "Under"}),
    "VS_OUC": ("CORNER_OU", {"CO": "Over", "CU": "Under"}),
    "VS_OUCH": ("HT_CORNER_OU", {"CO": "Over", "CU": "Under"}),
}
_KEY = re.compile(r"^(VS_[A-Z0-9]+)(?:@([\d.\-]+))?_(.+)$")


def map_odds(raw):
    """Sportsbook odds keys ("VS_OU@2.5_O": "1.65") -> the market/selection
    names the database has always used ({"OU2.5": {"Over": 1.65}}), matching
    scraper/fixtures_scraper.py exactly."""
    out = {}
    for key, value in raw.items():
        m = _KEY.match(key)
        if not m:
            continue
        family, line, sel = m.groups()
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if family in _FLAT and line is None:
            market, names = _FLAT[family]
            if names == "score":
                sel = sel.replace(":", "-")
            elif names is not None:
                sel = names.get(sel)
            if sel:
                out.setdefault(market, {})[sel] = price
        elif family in _LINES and line is not None:
            prefix, names = _LINES[family]
            if sel in names:
                out.setdefault(f"{prefix}{line}", {})[names[sel]] = price
    return out


def place_in_season(ext_match_id, first_id, games_per_round, rounds_per_season):
    """(round_number, match_number) of a game in the season starting at
    first_id, or None if the id falls outside that season."""
    if first_id is None or not games_per_round:
        return None
    offset = ext_match_id - first_id
    if 0 <= offset < rounds_per_season * games_per_round:
        return offset // games_per_round + 1, offset % games_per_round + 1
    return None


# ---- booking codes ----------------------------------------------------------
# The site's own "Book a Bet" call (found 2026-10-24 in its JS bundle): no
# login, places nothing -- it returns a code anyone can load on bet9ja to
# get the same picks in their betslip. Only event id + odds key + price are
# needed; the response's "RIS" is the booking code.
BOOK_URL = "https://sports.bet9ja.com/mobile/feapi/PlacebetAjax/BookABetV2"


def raw_odds_key(market, selection):
    """Our (market, selection) -> the sportsbook odds key: the reverse of map_odds."""
    for family, (name, names) in _FLAT.items():
        if name == market:
            if names is None:
                return f"{family}_{selection}"
            if names == "score":
                return f"{family}_{selection.replace('-', ':')}"
            code = next((k for k, v in names.items() if v == selection), None)
            return f"{family}_{code}" if code else None
    for family, (prefix, names) in _LINES.items():
        if market.startswith(prefix) and market[len(prefix):][:1].isdigit():
            code = next((k for k, v in names.items() if v == selection), None)
            return f"{family}@{market[len(prefix):]}_{code}" if code else None
    return None


def book_bet(picks, stake=500):
    """picks: [(event_id, odds_key, price)] -> booking code (str). Raises ZoomApiError."""
    import math
    import urllib.parse
    odds = math.prod(p for _, _, p in picks)
    slip = {"BETS": [{"BSTYPE": 0, "TAB": 0, "NUMLINES": len(picks), "COMB": 1, "TYPE": len(picks), "STAKE": stake,
                      "POTWINMIN": round(stake * odds, 2), "POTWINMAX": round(stake * odds, 2), "BONUSMIN": 0,
                      "BONUSMAX": 0, "ODDMIN": odds, "ODDMAX": odds,
                      "ODDS": {f"{e}${k}": p for e, k, p in picks}, "FIXED": {}}],
            "IMPERSONIZE": 0}
    body = urllib.parse.urlencode({"BETSLIP": json.dumps(slip), "IS_PASSBET": 0}).encode()
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded"}
    try:
        with urllib.request.urlopen(urllib.request.Request(BOOK_URL, data=body, headers=hdrs), timeout=TIMEOUT) as r:
            raw = r.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        d = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        raise ZoomApiError(f"book a bet: {e}") from e
    if d.get("R") != "OK" or not d.get("D") or not d["D"][0].get("RIS"):
        raise ZoomApiError(f"book a bet refused: {str(d.get('D'))[:200]}")
    return d["D"][0]["RIS"]
