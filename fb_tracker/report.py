"""Feasibility report: how many arbs, how big, how long they last, which leagues.

    python -m fb_tracker.report
"""
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime

from fb_tracker.collect import DB_PATH, connect


def _t(s):
    return datetime.fromisoformat(s)


def main():
    conn = connect(DB_PATH)
    runs = conn.execute("SELECT ts, counts, matched, arbs, errors FROM runs ORDER BY ts").fetchall()
    if not runs:
        print("No snapshots yet.")
        return
    first, last = _t(runs[0][0]), _t(runs[-1][0])
    hours = max((last - first).total_seconds() / 3600, 1e-9)
    gaps = [(_t(b[0]) - _t(a[0])).total_seconds() / 60 for a, b in zip(runs, runs[1:])]
    interval = statistics.median(gaps) if gaps else 10
    print(f"Period: {runs[0][0]} -> {runs[-1][0]} ({hours:.1f} h, {len(runs)} snapshots, typical gap {interval:.0f} min)")
    books = defaultdict(list)
    for _, counts, *_ in runs:
        for b, n in json.loads(counts).items():
            books[b].append(n)
    print("Matches listed per snapshot: " + ", ".join(f"{b} {statistics.mean(v):.0f}" for b, v in books.items()))
    print(f"Matched across bookmakers per snapshot: {statistics.mean(r[2] for r in runs):.0f}")
    errs = sum(1 for r in runs if r[4])
    if errs:
        print(f"Snapshots with a bookmaker error: {errs}")

    near = [r[0] for r in conn.execute("SELECT closest FROM games WHERE closest IS NOT NULL AND closest < 9")]
    if near:
        print(f"\nHow close games came to an arb (best moment per game, {len(near)} games):")
        for lim in (1.0, 1.01, 1.02, 1.03, 1.05):
            print(f"  within {(lim - 1) * 100:.0f}% of an arb: {sum(x < lim for x in near)}" if lim > 1
                  else f"  real arb (below 1.00): {sum(x < lim for x in near)}")
        print(f"  typical game: {statistics.median(near):.3f} ({(statistics.median(near) - 1) * 100:.1f}% short)")

    arbs = conn.execute("SELECT a.key, a.cover, a.first_seen, a.last_seen, a.snapshots, a.best_margin, a.suspicious, "
                        "g.country, g.league, g.names FROM arbs a JOIN games g ON g.key = a.key").fetchall()
    real = [a for a in arbs if not a[6]]
    print(f"\nArb episodes: {len(real)} (+{len(arbs) - len(real)} set aside as suspicious, >10%: likely mismatched games)")
    if not real:
        return
    days = hours / 24
    print(f"  about {len(real) / days:.0f} a day" if days >= 0.5 else "")
    m = [a[5] for a in real]
    for lo in (0.0, 0.01, 0.02, 0.03, 0.05):
        print(f"  margin >= {lo * 100:.0f}%: {sum(x >= lo for x in m)}")
    life = [(_t(a[3]) - _t(a[2])).total_seconds() / 60 + interval for a in real]
    print(f"  how long they stayed open: median ~{statistics.median(life):.0f} min; "
          f"seen in only one snapshot: {sum(a[4] == 1 for a in real)} of {len(real)}")
    print("\nBy kind:", dict(Counter(a[1] for a in real).most_common()))
    print("\nTop leagues (episodes, best margin):")
    by = defaultdict(list)
    for a in real:
        by[f"{a[7]} / {a[8]}"].append(a[5])
    for lg, ms in sorted(by.items(), key=lambda kv: -len(kv[1]))[:15]:
        print(f"  {lg:<45} {len(ms):>4}   best {max(ms) * 100:.1f}%")
    print("\nBiggest five (check the names really are the same game):")
    for a in sorted(real, key=lambda a: -a[5])[:5]:
        print(f"  {a[5] * 100:.1f}% {a[1]:<22} {a[2][:16]}  {json.loads(a[9])}")


if __name__ == "__main__":
    main()
