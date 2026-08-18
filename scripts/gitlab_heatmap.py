#!/usr/bin/env python3
"""Build a contribution heatmap from GitLab commit history.

GitLab's events API drops anything older than about three years, so the deep
history comes from the repository commits endpoint instead, which keeps
everything. Fetching every project from 2020 on every run would be slow and
pointless, so the run caches what it found in data/gitlab-contributions.json
and only asks GitLab for the recent window after that.

    gitlab_heatmap.py --backfill       first run, walks the whole history
    gitlab_heatmap.py                  daily run, refreshes --window days

Needs GITLAB_TOKEN with read_api. Writes gitlab-heatmap.svg.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone

API = "https://gitlab.com/api/v4"
CACHE = "data/gitlab-contributions.json"
OUT = "gitlab-heatmap.svg"

# Commits carry whichever address git was configured with at the time, so one
# person owns several. Anything not in this set belongs to a colleague.
IDENTITIES = {
    "mohammad@moghaddas.com",
    "6602315-momoghaddas@users.noreply.gitlab.com",
    "mo@zeeg.me",
}


def get(path, token, **params):
    """One API read, retried on a timeout.

    A busy project answers slowly enough to time out on the first ask, and a
    dropped page silently removes whole months from the history rather than
    failing the run, so a retry here is what keeps the graph honest.
    """
    url = f"{API}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": token})
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r), r.headers
        except urllib.error.HTTPError:
            raise  # 404 on an empty project is an answer, not a failure
        except Exception as e:
            last = e
            time.sleep(2 ** attempt)
    raise last


def projects(token):
    out, page = [], 1
    while True:
        batch, _ = get("projects", token, membership="true", simple="true",
                       per_page=100, page=page)
        if not batch:
            return out
        out += [(p["id"], p["path_with_namespace"]) for p in batch]
        page += 1


def commit_days(token, project_id, since, until, seen):
    """Days on which one of IDENTITIES committed, and how many times.

    Reads the default branch only, which is what a contribution graph means and
    what GitHub counts. Asking for `all=true` instead returns one copy of a
    commit per branch that reaches it, which inflates busy days by an order of
    magnitude and breaks paging over the duplicated set. `seen` carries commit
    ids across projects so a fork or a mirror cannot double-count either.
    """
    days, page = Counter(), 1
    while True:
        try:
            batch, _ = get(f"projects/{project_id}/repository/commits", token,
                           since=since, until=until, per_page=100, page=page)
        except Exception as e:
            # An empty or archived project answers 404. That is not a failure
            # of the run, so record nothing and move on.
            print(f"    skipped {project_id}: {e}", file=sys.stderr)
            return days
        if not batch:
            return days
        for c in batch:
            if c["id"] in seen:
                continue
            seen.add(c["id"])
            if (c.get("author_email") or "").lower() in IDENTITIES:
                days[c["committed_date"][:10]] += 1
        page += 1


def collect(token, since, until):
    total, seen = Counter(), set()
    for pid, name in projects(token):
        found = commit_days(token, pid, since, until, seen)
        if found:
            print(f"    {name}: {sum(found.values())} commits", file=sys.stderr)
        total.update(found)
    return total


def load_cache():
    try:
        with open(CACHE) as f:
            return Counter(json.load(f))
    except FileNotFoundError:
        return Counter()


def save_cache(counts):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w") as f:
        json.dump(dict(sorted(counts.items())), f, indent=0, sort_keys=True)
        f.write("\n")


# --- rendering ---------------------------------------------------------------

CELL, GAP, RADIUS = 11, 3, 2
TOP, LEFT = 20, 30
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def level(n):
    if n == 0:
        return 0
    if n <= 2:
        return 1
    if n <= 5:
        return 2
    if n <= 10:
        return 3
    return 4


def render(counts, weeks=53):
    today = date.today()
    # GitHub's grid starts on a Sunday, so walk back to the Sunday that makes
    # the last column the current week.
    end = today + timedelta(days=(6 - today.weekday()) % 7)
    start = end - timedelta(weeks=weeks - 1, days=6)

    width = LEFT + weeks * (CELL + GAP) + 10
    height = TOP + 7 * (CELL + GAP) + 34

    cells, labels, seen_month = [], [], None
    for w in range(weeks):
        for d in range(7):
            day = start + timedelta(weeks=w, days=d)
            if day > today:
                continue
            n = counts.get(day.isoformat(), 0)
            x = LEFT + w * (CELL + GAP)
            y = TOP + d * (CELL + GAP)
            title = f"{n} commit{'s' if n != 1 else ''} on {day.isoformat()}"
            cells.append(
                f'<rect x="{x}" y="{y}" width="{CELL}" height="{CELL}" '
                f'rx="{RADIUS}" class="l{level(n)}"><title>{title}</title></rect>'
            )
        first = start + timedelta(weeks=w)
        if first.month != seen_month and first.day <= 7:
            seen_month = first.month
            labels.append(
                f'<text x="{LEFT + w * (CELL + GAP)}" y="{TOP - 6}" '
                f'class="cap">{MONTHS[first.month - 1]}</text>'
            )

    for d, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        labels.append(
            f'<text x="0" y="{TOP + d * (CELL + GAP) + 9}" class="cap">{name}</text>'
        )

    total = sum(v for k, v in counts.items() if start.isoformat() <= k <= today.isoformat())
    legend_x = width - 150
    legend = [f'<text x="{legend_x - 30}" y="{height - 12}" class="cap">Less</text>']
    for i in range(5):
        legend.append(
            f'<rect x="{legend_x + i * (CELL + GAP)}" y="{height - 22}" '
            f'width="{CELL}" height="{CELL}" rx="{RADIUS}" class="l{i}"/>'
        )
    legend.append(
        f'<text x="{legend_x + 5 * (CELL + GAP) + 4}" y="{height - 12}" class="cap">More</text>'
    )

    nl = "\n  "
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" \
viewBox="0 0 {width} {height}" role="img" \
aria-label="{total} GitLab commits in the last year">
  <style>
    .cap {{ font: 9px ui-monospace, SFMono-Regular, Menlo, monospace; fill: #656d76; }}
    .ttl {{ font: 600 11px ui-monospace, SFMono-Regular, Menlo, monospace; fill: #1f2328; }}
    .l0 {{ fill: #ebedf0; }} .l1 {{ fill: #9be9a8; }} .l2 {{ fill: #40c463; }}
    .l3 {{ fill: #30a14e; }} .l4 {{ fill: #216e39; }}
    @media (prefers-color-scheme: dark) {{
      .cap {{ fill: #8b949e; }} .ttl {{ fill: #e6edf3; }}
      .l0 {{ fill: #161b22; }} .l1 {{ fill: #0e4429; }} .l2 {{ fill: #006d32; }}
      .l3 {{ fill: #26a641; }} .l4 {{ fill: #39d353; }}
    }}
  </style>
  <text x="0" y="{height - 12}" class="ttl">{total} commits on GitLab in the last year</text>
  {nl.join(labels)}
  {nl.join(cells)}
  {nl.join(legend)}
</svg>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--backfill", action="store_true",
                    help="walk the whole history instead of the recent window")
    ap.add_argument("--since", default="2020-01-01", help="backfill start date")
    ap.add_argument("--window", type=int, default=21,
                    help="days to refresh on a normal run")
    ap.add_argument("--weeks", type=int, default=53, help="weeks to draw")
    a = ap.parse_args()

    token = os.environ.get("GITLAB_TOKEN") or os.environ.get("GITLAB_API_TOKEN")
    counts = load_cache()

    if token:
        now = datetime.now(timezone.utc)
        since = (f"{a.since}T00:00:00Z" if a.backfill
                 else (now - timedelta(days=a.window)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        print(f"fetching from {since[:10]}", file=sys.stderr)
        fresh = collect(token, since, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        # The refetched window is authoritative: drop the cached copy of those
        # days first, or an amended or rebased commit is counted twice.
        for day in [d for d in counts if d >= since[:10]]:
            del counts[day]
        counts.update(fresh)
        save_cache(counts)
        print(f"cached {len(counts)} active days, {sum(counts.values())} commits",
              file=sys.stderr)
    else:
        print("no GITLAB_TOKEN, drawing from cache only", file=sys.stderr)

    if not counts:
        sys.exit("nothing to draw: no cache and no token")

    with open(OUT, "w") as f:
        f.write(render(counts, a.weeks))
    print(f"wrote {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
