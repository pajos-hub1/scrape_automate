"""Find the same match at different bookmakers.

Team names differ between books ("Man City" / "Manchester City", "QPR" /
"Queens Park Rangers"), so a match is: kickoff within KICKOFF_SLACK, and both
team names similar enough after normalising. Words that tell teams apart
(women, U21, II, reserves) are kept; filler (FC, AFC, CF, ...) is dropped.
"""
import re
import unicodedata
from datetime import datetime, timedelta
from difflib import SequenceMatcher

KICKOFF_SLACK = timedelta(minutes=15)
MIN_SIDE = 0.55          # each team name at least this similar
MIN_PAIR = 0.75          # and the two together, on average
FILLER = {"fc", "afc", "cf", "sc", "ac", "fk", "sk", "cd", "ud", "club", "de", "the", "calcio", "sv", "if", "bk",
          "cfc", "ssc", "as", "sl", "ff", "football", "futbol", "clube"}
ALIASES = {"man": "manchester", "utd": "united", "qpr": "queens park rangers", "wolves": "wolverhampton",
           "spurs": "tottenham", "psg": "paris saint germain", "st": "saint", "w": "women", "wfc": "women"}


def normalise(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    words = []
    for w in s.split():
        w = ALIASES.get(w, w)
        if w not in FILLER:
            words.append(w)
    return " ".join(words)


def similarity(a, b):
    a, b = normalise(a), normalise(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    overlap = len(ta & tb) / min(len(ta), len(tb))        # "brighton" vs "brighton hove albion"
    # one name contained in the other counts, unless it's a youth/women's side vs the senior team
    marks = {"women", "u19", "u20", "u21", "u23", "ii", "b", "reserves", "youth"}
    if (ta & marks) != (tb & marks):
        return min(ratio, 0.4)
    return max(ratio, overlap * 0.95)


def _ko(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def match_books(events_by_book):
    """[{book: Event}] -- one dict per real match, covering 2+ books.
    The first book anchors; each other book's events are paired greedily by score."""
    books = list(events_by_book)
    if len(books) < 2:
        return []
    groups = [{books[0]: e} for e in events_by_book[books[0]]]
    for book in books[1:]:
        candidates = []
        for gi, g in enumerate(groups):
            anchor = next(iter(g.values()))
            ka = _ko(anchor.kickoff)
            for ei, e in enumerate(events_by_book[book]):
                if abs(_ko(e.kickoff) - ka) > KICKOFF_SLACK:
                    continue
                sh, sa = similarity(anchor.home, e.home), similarity(anchor.away, e.away)
                if sh >= MIN_SIDE and sa >= MIN_SIDE and (sh + sa) / 2 >= MIN_PAIR:
                    candidates.append(((sh + sa) / 2, gi, ei))
        used_g, used_e = set(), set()
        for score, gi, ei in sorted(candidates, reverse=True):
            if gi not in used_g and ei not in used_e:
                groups[gi][book] = events_by_book[book][ei]
                used_g.add(gi)
                used_e.add(ei)
    return [g for g in groups if len(g) >= 2]
