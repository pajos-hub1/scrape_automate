"""Pre-match football odds from each bookmaker's public JSON (the calls its own
website makes). Every source returns the same shape:

    Event(book, country, league, home, away, kickoff "YYYY-MM-DDTHH:MM:SSZ", odds)
    odds = {("1X2", "1"): 2.54, ("1X2", "X"): 3.4, ("1X2", "2"): 2.7,
            ("DC", "1X"): ..., ("DC", "12"): ..., ("DC", "X2"): ...,
            ("OU2.5", "O"): 1.72, ("OU2.5", "U"): 2.06}

Requests are spaced out (POLITE_DELAY) so a snapshot never hammers a site.
BetKing is next once its full-listing call is found (its site builds the
request addresses at runtime).
"""
import gzip
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

USER_AGENT = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")
POLITE_DELAY = 0.2     # seconds between requests to the same site
TIMEOUT = 30


@dataclass
class Event:
    book: str
    country: str
    league: str
    home: str
    away: str
    kickoff: str
    odds: dict = field(default_factory=dict)


def get_json(url, retries=2):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return json.loads(raw)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))


def _price(v):
    try:
        p = float(v)
    except (TypeError, ValueError):
        return None
    return p if p > 1.0 else None


# ---- Bet9ja -----------------------------------------------------------------

BET9JA = "https://sports.bet9ja.com/mobile/feapi/PalimpsestAjax"
BET9JA_KEYS = {"S_1X2_1": ("1X2", "1"), "S_1X2_X": ("1X2", "X"), "S_1X2_2": ("1X2", "2"),
               "S_DC_1X": ("DC", "1X"), "S_DC_12": ("DC", "12"), "S_DC_X2": ("DC", "X2"),
               "S_OU@2.5_O": ("OU2.5", "O"), "S_OU@2.5_U": ("OU2.5", "U")}


def bet9ja_leagues():
    """[(country, league name, league id)] for every soccer league with matches."""
    pal = get_json(f"{BET9JA}/GetSports?DISP=0")["D"]["PAL"]
    soccer = next(v for v in pal.values() if isinstance(v, dict) and v.get("S_DESC") == "Soccer")
    return [(sg.get("SG_DESC", ""), g.get("G_DESC", ""), gid)
            for sg in soccer.get("SG", {}).values() for gid, g in (sg.get("G") or {}).items() if (g.get("NUM") or 0) > 0]


def parse_bet9ja(payload, country, league):
    out = []
    for e in (payload.get("D") or {}).get("E", []) or []:
        ds = e.get("DS") or ""
        if " - " not in ds or not e.get("STARTDATEUTC"):
            continue
        home, away = (x.strip() for x in ds.split(" - ", 1))
        odds = {}
        for k, key in BET9JA_KEYS.items():
            p = _price((e.get("O") or {}).get(k))
            if p:
                odds[key] = p
        if odds:
            out.append(Event("bet9ja", country, league, home, away, e["STARTDATEUTC"], odds))
    return out


def bet9ja():
    events = []
    for country, league, gid in bet9ja_leagues():
        time.sleep(POLITE_DELAY)
        try:
            payload = get_json(f"{BET9JA}/GetEventsInGroupV2?GROUPID={gid}&DISP=0&matches=false&LIMIT=200&GROUPMARKETID=1")
        except Exception as e:  # noqa: BLE001 -- one league failing mustn't stop the rest
            print(f"   (bet9ja {country} / {league}: {type(e).__name__})")
            continue
        events += parse_bet9ja(payload, country, league)
    return events


# ---- SportyBet --------------------------------------------------------------

SPORTY = ("https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents?sportId=sr%3Asport%3A1"
          "&marketId=1%2C18%2C10&pageSize=100&pageNum={page}&option=1")
SPORTY_1X2 = {"Home": "1", "Draw": "X", "Away": "2"}
SPORTY_DC = {"Home or Draw": "1X", "Home or Away": "12", "Draw or Away": "X2"}


def parse_sportybet(payload):
    out = []
    for t in (payload.get("data") or {}).get("tournaments", []) or []:
        for e in t.get("events", []) or []:
            cat = (e.get("sport") or {}).get("category") or {}
            country, league = cat.get("name", ""), (cat.get("tournament") or {}).get("name", t.get("name", ""))
            ko = datetime.fromtimestamp(e["estimateStartTime"] / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            odds = {}
            for m in e.get("markets", []) or []:
                mid, spec = str(m.get("id")), m.get("specifier")
                for o in m.get("outcomes", []) or []:
                    if not o.get("isActive", 1):
                        continue
                    p, desc = _price(o.get("odds")), o.get("desc", "")
                    if not p:
                        continue
                    if mid == "1" and desc in SPORTY_1X2:
                        odds[("1X2", SPORTY_1X2[desc])] = p
                    elif mid == "10" and desc in SPORTY_DC:
                        odds[("DC", SPORTY_DC[desc])] = p
                    elif mid == "18" and spec == "total=2.5" and desc in ("Over 2.5", "Under 2.5"):
                        odds[("OU2.5", desc[0])] = p
            if odds:
                out.append(Event("sportybet", country, league, e.get("homeTeamName", ""), e.get("awayTeamName", ""), ko, odds))
    return out


def sportybet():
    first = get_json(SPORTY.format(page=1))
    total = (first.get("data") or {}).get("totalNum") or 0
    events = parse_sportybet(first)
    for page in range(2, total // 100 + 2):
        time.sleep(POLITE_DELAY)
        events += parse_sportybet(get_json(SPORTY.format(page=page)))
    return events


SOURCES = {"bet9ja": bet9ja, "sportybet": sportybet}
