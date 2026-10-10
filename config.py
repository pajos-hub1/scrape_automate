from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "zoom.db"

# GitHub hard-rejects any single file over 100 MiB, and zoom.db crossed that
# in Sept 2026 -- every cycle's push failed from then on. So git only ever
# stores gzipped copies (see db/storage.py), and once the live DB grows past
# ARCHIVE_TRIGGER_MB, old reconciled rows from the three big append-only
# tables get moved into a new, frozen archive file (see db/archive.py).
DB_GZ_PATH = DB_PATH.with_suffix(".db.gz")
ARCHIVE_DIR = BASE_DIR / "data" / "archive"
ARCHIVE_TRIGGER_MB = 50
ARCHIVE_KEEP_DAYS = 2

# The dashboard's charts/stats cover only the most recent N seasons (the
# CLI report in track/report.py stays all-time).
DASHBOARD_SEASONS = 2

# Results page (Selenium-rendered carousel of played rounds for the two
# seasons bet9ja currently exposes: "current" and "previous").
# NOTE: matchId in this URL is tied to a specific live match/session on
# bet9ja's side and may need to be refreshed periodically -- verify this
# still loads real data when the scraper starts failing to find rounds.
RESULTS_URL = (
    "https://zoomapi.bet9ja.com/zoom/results/premier-zoom"
    "?matchId=2462420&clientId=68&offset=3600000"
)

# Upcoming fixtures + odds page (mobile sportsbook, Zoom Soccer hub).
# This page lists multiple virtual leagues -- we filter down to Premier
# League (Zoom) fixtures only.
FIXTURES_URL = "https://sports.bet9ja.com/mobile/sport/zoomsoccer/101"
LEAGUE_FILTER = "Premier"

ROUNDS_PER_SEASON = 38
MATCHES_PER_ROUND = 10

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)
