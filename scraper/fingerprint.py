"""Season identity, inferred without any season label on the page.

The results carousel only ever shows two seasons: whatever rounds have
been played so far in the "current" one, and the full 38 rounds of
"previous". Re-scraping the "current" season 90 minutes later reveals one
more completed round -- so a fingerprint over *all* visible rows is not
stable across the season's own lifetime and can't be used as identity by
itself.

Round 1 is different: once it has been played, its 10 results (pairings +
scores) never change again for that season, whether the season is
currently "current" (in progress) or has rotated to "previous" (complete).
So we fingerprint a season by its Round 1 results alone. That's both
stable across repeat scrapes of the same season, and how we recognize
"current" reappearing as "previous" later -- same Round 1 data, same
fingerprint, same season_id.

Team pairings alone would be a weak fingerprint if bet9ja reuses a fixed
Round 1 schedule across seasons (this needs live confirmation -- see the
Round 1/Round 2 duplicate question). Folding in the actual scores adds
enough entropy (goals are RNG per season) that a collision between two
different real seasons is effectively impossible either way.
"""
import hashlib


def compute_fingerprint(round_1_matches):
    """round_1_matches: list of dicts with match_number, team_a, team_b,
    ft_a, ft_b for Round 1 of a single season. Order-independent.
    """
    parts = sorted(
        f"{m['match_number']}|{m['team_a']}|{m['team_b']}|{m.get('ft_a')}|{m.get('ft_b')}"
        for m in round_1_matches
    )
    blob = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
