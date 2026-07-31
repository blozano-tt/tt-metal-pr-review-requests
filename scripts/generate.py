#!/usr/bin/env python3
"""
Generate a static page listing open tenstorrent/tt-metal PRs (created in the last
2 months, drafts excluded) together with the individual CODEOWNERS whose approval would still
unblock each PR.

See README.md for the design assumptions.
"""

from __future__ import annotations

import html
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pathspec
import requests

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

SRC_OWNER = "tenstorrent"
SRC_REPO = "tt-metal"
CODEOWNERS_PATH = ".github/CODEOWNERS"

# Blanket bypass entry stripped from every matched owner line (see README).
BYPASS_TEAM = "@tenstorrent/codeowner-bypass"

MONTHS_BACK = 2
PRS_PER_GQL_PAGE = 20

# Draft PRs are excluded (see README assumptions).
INCLUDE_DRAFTS = False

# Titles longer than this are truncated with an ellipsis; the full title stays
# available as the link's tooltip.
TITLE_MAX_CHARS = 110
OUT_DIR = os.environ.get("OUT_DIR", "public")

API = "https://api.github.com"
GQL = "https://api.github.com/graphql"

WARNINGS: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


def get_token() -> str:
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not tok:
        sys.exit("ERROR: set GH_TOKEN or GITHUB_TOKEN")
    return tok


SESSION = requests.Session()


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "tt-metal-pr-review-requests",
    }


# --------------------------------------------------------------------------
# HTTP helpers (with rate-limit / transient-error backoff)
# --------------------------------------------------------------------------


def rest(token: str, path: str, params: dict | None = None, tries: int = 5):
    """GET a REST endpoint. Returns (json, response) or (None, response) on 403/404."""
    url = path if path.startswith("http") else f"{API}{path}"
    for attempt in range(tries):
        r = SESSION.get(url, headers=_headers(token), params=params, timeout=60)
        if r.status_code == 200:
            return r.json(), r
        if r.status_code in (403, 429):
            # Could be rate limiting or genuine permission denial.
            remaining = r.headers.get("X-RateLimit-Remaining")
            retry_after = r.headers.get("Retry-After")
            if retry_after:
                nap = min(int(retry_after), 300)
                log(f"  rate limited on {url}; sleeping {nap}s")
                time.sleep(nap)
                continue
            if remaining == "0":
                reset = int(r.headers.get("X-RateLimit-Reset", "0"))
                nap = max(5, min(reset - int(time.time()) + 5, 900))
                log(f"  rate limit exhausted; sleeping {nap}s")
                time.sleep(nap)
                continue
            return None, r  # genuine 403 (e.g. no permission to read team)
        if r.status_code == 404:
            return None, r
        if r.status_code >= 500 and attempt < tries - 1:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
    return None, r


def rest_paginated(token: str, path: str, params: dict | None = None):
    """Yield items across all pages of a REST list endpoint."""
    params = dict(params or {})
    params.setdefault("per_page", 100)
    url = f"{API}{path}"
    while url:
        data, r = rest(token, url, params=params)
        if data is None:
            return
        for item in data:
            yield item
        url = r.links.get("next", {}).get("url")
        params = None  # the next link already carries the query string


def graphql(token: str, query: str, variables: dict, tries: int = 6) -> dict:
    for attempt in range(tries):
        r = SESSION.post(
            GQL,
            headers=_headers(token),
            json={"query": query, "variables": variables},
            timeout=120,
        )
        if r.status_code in (403, 429) or r.status_code >= 500:
            retry_after = r.headers.get("Retry-After")
            nap = int(retry_after) if retry_after else min(60, 2 ** attempt * 5)
            log(f"  graphql http {r.status_code}; sleeping {nap}s")
            time.sleep(nap)
            continue
        r.raise_for_status()
        payload = r.json()
        errors = payload.get("errors")
        if errors:
            texts = "; ".join(e.get("message", "") for e in errors)
            if "rate limit" in texts.lower() or "timeout" in texts.lower():
                log(f"  graphql error ({texts}); retrying")
                time.sleep(min(60, 2 ** attempt * 5))
                continue
            raise RuntimeError(f"GraphQL errors: {texts}")
        return payload["data"]
    raise RuntimeError("GraphQL request failed after retries")


# --------------------------------------------------------------------------
# CODEOWNERS
# --------------------------------------------------------------------------


GLOB_CHARS = "*?["


def _exact_depth(pattern: str) -> int | None:
    """
    GitHub's CODEOWNERS docs deviate from gitignore for anchored patterns whose
    final segment is a glob:

        # @octocat owns any file in the root of the repository,
        # but not in subdirectories
        /* @octocat

    Plain gitignore semantics (which `pathspec` implements) would let `/*` match
    a top-level *directory* and therefore everything beneath it. For CODEOWNERS
    such a pattern must match only at its own depth. Return that required path
    depth, or None if the normal gitignore behaviour applies.
    """
    if pattern.endswith("/") or "**" in pattern:
        return None  # explicit directory rule, or an explicit recursive glob
    body = pattern.lstrip("/")
    if "/" not in body and not pattern.startswith("/"):
        return None  # unanchored basename pattern: matches at any depth
    segments = body.split("/")
    if not any(ch in segments[-1] for ch in GLOB_CHARS):
        return None  # may legitimately name a directory -> recursive
    return len(segments)


class CodeownersRule:
    __slots__ = ("pattern", "owners", "spec", "lineno", "exact_depth")

    def __init__(self, pattern: str, owners: tuple[str, ...], lineno: int):
        self.pattern = pattern
        self.owners = owners
        self.lineno = lineno
        self.spec = pathspec.PathSpec.from_lines("gitwildmatch", [pattern])
        self.exact_depth = _exact_depth(pattern)

    def matches(self, path: str) -> bool:
        if not self.spec.match_file(path):
            return False
        if self.exact_depth is not None and path.count("/") + 1 != self.exact_depth:
            return False
        return True


def parse_codeowners(text: str) -> list[CodeownersRule]:
    """Parse CODEOWNERS into an ordered list of rules (last match wins)."""
    rules: list[CodeownersRule] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Strip a trailing inline comment (whitespace + '#').
        if " #" in line:
            line = line.split(" #", 1)[0].strip()
        parts = line.split()
        if not parts:
            continue
        pattern, owners = parts[0], tuple(parts[1:])
        # Drop the blanket bypass team from every rule (design decision #1).
        owners = tuple(o for o in owners if o.lower() != BYPASS_TEAM.lower())
        try:
            rules.append(CodeownersRule(pattern, owners, lineno))
        except Exception as exc:  # pragma: no cover - defensive
            warn(f"CODEOWNERS line {lineno} pattern {pattern!r} unusable: {exc}")
    return rules


def owner_group_for(path: str, rules: list[CodeownersRule]) -> tuple[str, ...] | None:
    """Return the owner list of the LAST rule matching `path`, or None if unowned."""
    match = None
    for rule in rules:
        if rule.matches(path):
            match = rule
    if match is None:
        return None
    return match.owners


# --------------------------------------------------------------------------
# Team expansion
# --------------------------------------------------------------------------


class TeamResolver:
    """Expands @org/team-slug to individual logins, cached for the whole run."""

    def __init__(self, token: str):
        self.token = token
        self.cache: dict[str, tuple[str, ...]] = {}
        self.failed: set[str] = set()

    def expand(self, handle: str) -> tuple[str, ...]:
        if "/" not in handle:
            return (handle.lstrip("@"),)
        key = handle.lower()
        if key in self.cache:
            return self.cache[key]
        org, _, slug = handle.lstrip("@").partition("/")
        members: list[str] = []
        data, r = rest(self.token, f"/orgs/{org}/teams/{slug}/members", {"per_page": 100})
        if data is None:
            # Graceful degradation: keep the raw team handle rather than dropping it.
            warn(
                f"could not read members of {handle} "
                f"(HTTP {r.status_code}); keeping raw team handle"
            )
            self.failed.add(handle)
            result = (handle,)
            self.cache[key] = result
            return result
        members.extend(m["login"] for m in data)
        next_url = r.links.get("next", {}).get("url")
        while next_url:
            page, r = rest(self.token, next_url)
            if page is None:
                break
            members.extend(m["login"] for m in page)
            next_url = r.links.get("next", {}).get("url")
        result = tuple(sorted(set(members), key=str.lower))
        self.cache[key] = result
        log(f"  team {handle}: {len(result)} members")
        return result


# --------------------------------------------------------------------------
# PR fetching (GraphQL: PRs + changed files + reviews in batched requests)
# --------------------------------------------------------------------------

PR_QUERY = """
query($owner:String!, $name:String!, $cursor:String, $prs:Int!) {
  rateLimit { cost remaining resetAt }
  repository(owner:$owner, name:$name) {
    pullRequests(states: OPEN, first: $prs, after: $cursor,
                 orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        url
        title
        createdAt
        isDraft
        files(first: 100) {
          pageInfo { hasNextPage endCursor }
          nodes { path }
        }
        reviews(first: 100) {
          pageInfo { hasNextPage endCursor }
          nodes { state submittedAt createdAt author { login } }
        }
      }
    }
  }
}
"""

PR_FILES_QUERY = """
query($owner:String!, $name:String!, $number:Int!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      files(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { path }
      }
    }
  }
}
"""

PR_REVIEWS_QUERY = """
query($owner:String!, $name:String!, $number:Int!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      reviews(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { state submittedAt createdAt author { login } }
      }
    }
  }
}
"""


def months_ago(dt: datetime, months: int) -> datetime:
    """Subtract calendar months, clamping the day to the target month's length."""
    year, month = dt.year, dt.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = dt.day
    while day > 0:
        try:
            return dt.replace(year=year, month=month, day=day)
        except ValueError:
            day -= 1
    raise ValueError("unreachable")


def fetch_prs(token: str, cutoff: datetime) -> list[dict]:
    """
    Paginate open PRs newest-first, stopping once we pass the cutoff date.

    Drafts are dropped here. GitHub's GraphQL `pullRequests` connection has no
    server-side draft filter (only `states`, `labels`, `baseRefName`, ...), so
    this is done client-side; `build_rows` re-checks as a safety net.
    """
    prs: list[dict] = []
    cursor = None
    page = 0
    skipped = 0
    while True:
        page += 1
        data = graphql(
            token,
            PR_QUERY,
            {"owner": SRC_OWNER, "name": SRC_REPO, "cursor": cursor, "prs": PRS_PER_GQL_PAGE},
        )
        rl = data.get("rateLimit") or {}
        conn = data["repository"]["pullRequests"]
        stop = False
        drafts = 0
        for node in conn["nodes"]:
            created = datetime.fromisoformat(node["createdAt"].replace("Z", "+00:00"))
            if created < cutoff:
                # Results are ordered newest-first, so everything after this is older.
                stop = True
                continue
            if node["isDraft"] and not INCLUDE_DRAFTS:
                drafts += 1
                continue
            prs.append(node)
        skipped += drafts
        log(
            f"  page {page}: +{len(conn['nodes'])} PRs scanned, "
            f"{len(prs)} in window (skipped {skipped} draft{'s' if skipped != 1 else ''}) "
            f"(gql remaining {rl.get('remaining')})"
        )
        if stop or not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return prs


def complete_pr(token: str, pr: dict) -> tuple[list[str], list[dict]]:
    """Return (all changed file paths, all reviews), paginating if truncated."""
    files = [n["path"] for n in pr["files"]["nodes"]]
    if pr["files"]["pageInfo"]["hasNextPage"]:
        cursor = pr["files"]["pageInfo"]["endCursor"]
        while cursor:
            data = graphql(
                token,
                PR_FILES_QUERY,
                {"owner": SRC_OWNER, "name": SRC_REPO, "number": pr["number"], "cursor": cursor},
            )
            conn = data["repository"]["pullRequest"]["files"]
            files.extend(n["path"] for n in conn["nodes"])
            cursor = conn["pageInfo"]["endCursor"] if conn["pageInfo"]["hasNextPage"] else None

    reviews = list(pr["reviews"]["nodes"])
    if pr["reviews"]["pageInfo"]["hasNextPage"]:
        cursor = pr["reviews"]["pageInfo"]["endCursor"]
        while cursor:
            data = graphql(
                token,
                PR_REVIEWS_QUERY,
                {"owner": SRC_OWNER, "name": SRC_REPO, "number": pr["number"], "cursor": cursor},
            )
            conn = data["repository"]["pullRequest"]["reviews"]
            reviews.extend(conn["nodes"])
            cursor = conn["pageInfo"]["endCursor"] if conn["pageInfo"]["hasNextPage"] else None
    return files, reviews


# --------------------------------------------------------------------------
# Review state resolution
# --------------------------------------------------------------------------

# COMMENTED / PENDING reviews do not change a reviewer's approval standing on
# GitHub, so they never supersede an earlier APPROVED / CHANGES_REQUESTED.
DECISIVE_STATES = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}


def approvers(reviews: list[dict]) -> set[str]:
    """Logins whose most recent *decisive* review state is APPROVED."""
    latest: dict[str, tuple[str, str]] = {}
    for rv in reviews:
        author = (rv.get("author") or {}).get("login")
        state = rv.get("state")
        if not author or state not in DECISIVE_STATES:
            continue
        when = rv.get("submittedAt") or rv.get("createdAt") or ""
        prev = latest.get(author.lower())
        if prev is None or when >= prev[0]:
            latest[author.lower()] = (when, state)
    return {login for login, (_, state) in latest.items() if state == "APPROVED"}


def human_age(created: datetime, now: datetime) -> tuple[str, int]:
    delta = now - created
    days = delta.days
    if days >= 1:
        return (f"{days} day{'s' if days != 1 else ''}", days)
    hours = delta.seconds // 3600
    if hours >= 1:
        return (f"{hours} hour{'s' if hours != 1 else ''}", 0)
    minutes = max(1, delta.seconds // 60)
    return (f"{minutes} minute{'s' if minutes != 1 else ''}", 0)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def build_rows(token: str, rules: list[CodeownersRule], teams: TeamResolver,
               prs: list[dict], now: datetime) -> list[dict]:
    rows = []
    unowned_only = 0
    for i, pr in enumerate(prs, start=1):
        # Safety net: drafts are filtered during fetch, but never let one through.
        if pr["isDraft"] and not INCLUDE_DRAFTS:
            continue
        files, reviews = complete_pr(token, pr)
        approved_by = approvers(reviews)

        # Distinct owner-groups across all changed files (last match wins per file).
        groups: set[tuple[str, ...]] = set()
        for path in files:
            grp = owner_group_for(path, rules)
            if grp:  # None = unowned, () = owners were only the bypass team
                groups.add(grp)

        outstanding: set[str] = set()
        for grp in groups:
            members: set[str] = set()
            for handle in grp:
                members.update(teams.expand(handle))
            if not members:
                continue
            # Satisfied if any member of this group has already approved.
            if any(m.lstrip("@").lower() in approved_by for m in members):
                continue
            outstanding.update(members)

        if not groups:
            unowned_only += 1

        created = datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
        age_text, age_days = human_age(created, now)
        rows.append(
            {
                "number": pr["number"],
                "url": pr["url"],
                "title": pr["title"],
                "draft": pr["isDraft"],
                "created": created,
                "age_text": age_text,
                "age_days": age_days,
                "files": len(files),
                "groups": len(groups),
                "codeowners": sorted(outstanding, key=str.lower),
            }
        )
        if i % 50 == 0 or i == len(prs):
            log(f"  processed {i}/{len(prs)} PRs")
    if unowned_only:
        log(f"  note: {unowned_only} PRs had no CODEOWNERS-matched files")
    rows.sort(key=lambda r: r["number"])  # ascending PR number (oldest first)
    return rows


CSS = """
:root {
  --bg: #0f1116; --panel: #171a21; --line: #262b36; --text: #e6e9ef;
  --muted: #98a1b3; --link: #7cb7ff; --accent: #4ade80; --chip: #222836;
}
@media (prefers-color-scheme: light) {
  :root { --bg:#f7f8fa; --panel:#fff; --line:#e3e6ec; --text:#1a1d24;
          --muted:#5d6677; --link:#0a58ca; --accent:#137a3d; --chip:#eef1f6; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 32px 20px 64px; }
h1 { font-size: 22px; margin: 0 0 6px; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
.sub a { color: var(--link); }
.stats { display:flex; gap:10px; flex-wrap:wrap; margin: 0 0 20px; }
.stat { background:var(--panel); border:1px solid var(--line); border-radius:8px;
  padding:8px 14px; font-size:13px; }
.stat b { font-size:17px; display:block; color:var(--text); }
a.stat { color:var(--link); text-decoration:none; transition:border-color .12s ease; }
a.stat:hover, a.stat:focus-visible { border-color:var(--link); text-decoration:underline; }
a.stat::after { content:" \2197"; font-size:11px; opacity:.75; }
table { width:100%; border-collapse: collapse; background:var(--panel);
  border:1px solid var(--line); border-radius:10px; overflow:hidden; }
th, td { text-align:left; padding:9px 14px; border-bottom:1px solid var(--line);
  vertical-align: top; }
th { position: sticky; top:0; background:var(--panel); font-size:12px;
  text-transform:uppercase; letter-spacing:.06em; color:var(--muted); z-index:1; }
tr:last-child td { border-bottom:none; }
td.pr { white-space:nowrap; font-variant-numeric: tabular-nums; }
td.age { white-space:nowrap; color:var(--muted); font-variant-numeric: tabular-nums; }
td.title { max-width: 420px; overflow-wrap: anywhere; }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
.owner { display:inline-block; background:var(--chip); border:1px solid var(--line);
  border-radius:5px; padding:1px 6px; margin:2px 3px 2px 0; font-size:13px;
  white-space:nowrap; }
.none { color: var(--accent); }
.warnbox { background:var(--panel); border:1px solid var(--line); border-left:3px solid #d29922;
  border-radius:8px; padding:12px 16px; margin-bottom:20px; font-size:13px; }
.warnbox ul { margin:6px 0 0; padding-left:18px; }
footer { color:var(--muted); font-size:12px; margin-top:24px; }
"""


def pulls_url(*qualifiers: str) -> str:
    """Build a github.com PR-search URL from real, supported search qualifiers."""
    query = " ".join(qualifiers)
    return (
        f"https://github.com/{SRC_OWNER}/{SRC_REPO}/pulls"
        f"?{urlencode({'q': query})}"
    )


def render_html(rows: list[dict], now: datetime, cutoff: datetime) -> str:
    total = len(rows)
    blocked = sum(1 for r in rows if r["codeowners"])
    clear = total - blocked
    distinct = len({o for r in rows for o in r["codeowners"]})

    # Base filters mirror this page's dataset: open, non-draft, within the window.
    base = ("is:pr", "is:open", "-is:draft", f"created:>={cutoff:%Y-%m-%d}")

    # NOTE: GitHub has no search qualifier for "has an unsatisfied CODEOWNERS
    # group", which is what this page actually computes. The review:* qualifiers
    # below are the closest supported approximations, so the counts shown here
    # will not match GitHub's result counts exactly. The tooltips say so.
    approx = (
        " Approximate: GitHub has no search qualifier for "
        "'unsatisfied CODEOWNERS group', so this count will not match exactly."
    )
    tiles = [
        (
            total,
            "open PRs (non-draft)",
            pulls_url(*base),
            "All open, non-draft PRs created in this window, on GitHub.",
        ),
        (
            blocked,
            "awaiting codeowner approval",
            pulls_url(*base, "review:required"),
            "PRs GitHub still marks as requiring review (review:required)." + approx,
        ),
        (
            clear,
            "no outstanding codeowners",
            pulls_url(*base, "review:approved"),
            "PRs GitHub marks as approved (review:approved)." + approx,
        ),
        (distinct, "distinct reviewers needed", None, None),
    ]

    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>tt-metal open PRs &middot; outstanding codeowner reviews</title>",
        f"<style>{CSS}</style></head><body><div class='wrap'>",
        "<h1>tt-metal &mdash; outstanding codeowner reviews</h1>",
        "<p class='sub'>Open, non-draft pull requests in "
        f"<a href='https://github.com/{SRC_OWNER}/{SRC_REPO}'>{SRC_OWNER}/{SRC_REPO}</a> "
        f"created on or after {cutoff:%Y-%m-%d} (last {MONTHS_BACK} months). "
        "The <em>Codeowners</em> column lists the individual accounts whose approval "
        "would still unblock the PR.</p>",
        "<div class='stats'>"
        + "".join(
            (
                f"<a class='stat' href='{html.escape(url)}' "
                f"title='{html.escape(tip)}'><b>{value}</b>{label}</a>"
            )
            if url
            else f"<div class='stat'><b>{value}</b>{label}</div>"
            for value, label, url, tip in tiles
        )
        + "</div>",
    ]

    if WARNINGS:
        uniq = sorted(set(WARNINGS))
        parts.append("<div class='warnbox'><strong>Build warnings</strong><ul>")
        parts.extend(f"<li>{html.escape(w)}</li>" for w in uniq[:20])
        if len(uniq) > 20:
            parts.append(f"<li>&hellip; and {len(uniq) - 20} more</li>")
        parts.append("</ul></div>")

    parts.append(
        "<table><thead><tr><th>PR</th><th>Title</th><th>Age</th>"
        "<th>Codeowners</th></tr></thead><tbody>"
    )
    for r in rows:
        if r["codeowners"]:
            owners = "".join(
                f"<span class='owner'>@{html.escape(o.lstrip('@'))}</span>"
                for o in r["codeowners"]
            )
        else:
            owners = "<span class='none'>&mdash; no outstanding codeowners &mdash;</span>"
        full_title = r["title"]
        shown = (
            full_title
            if len(full_title) <= TITLE_MAX_CHARS
            else full_title[: TITLE_MAX_CHARS - 1].rstrip() + "…"
        )
        parts.append(
            "<tr>"
            f"<td class='pr'><a href='{html.escape(r['url'])}'>"
            f"{SRC_REPO}#{r['number']}</a></td>"
            f"<td class='title' title='{html.escape(full_title)}'>"
            f"{html.escape(shown)}</td>"
            f"<td class='age'>{html.escape(r['age_text'])}</td>"
            f"<td>{owners}</td></tr>"
        )
    parts.append("</tbody></table>")
    parts.append(
        "<footer>Last refreshed "
        f"<strong>{now:%Y-%m-%d %H:%M:%S} UTC</strong>. Refreshed automatically every 3 hours. "
        "Sorted by PR number, ascending. Draft PRs excluded. "
        "<a href='https://github.com/blozano-tt/tt-metal-pr-review-requests'>Source &amp; assumptions</a>."
        "</footer>"
    )
    parts.append("</div></body></html>")
    return "\n".join(parts)


def main() -> None:
    token = get_token()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    cutoff = months_ago(now, MONTHS_BACK)
    log(f"Cutoff: PRs created on/after {cutoff.isoformat()}")

    log("Fetching CODEOWNERS ...")
    data, r = rest(token, f"/repos/{SRC_OWNER}/{SRC_REPO}/contents/{CODEOWNERS_PATH}")
    if data is None:
        sys.exit(f"ERROR: cannot read CODEOWNERS (HTTP {r.status_code})")
    import base64

    text = base64.b64decode(data["content"]).decode("utf-8")
    rules = parse_codeowners(text)
    log(f"  parsed {len(rules)} CODEOWNERS rules")

    log("Fetching open PRs ...")
    prs = fetch_prs(token, cutoff)
    log(f"  {len(prs)} open PRs in the last {MONTHS_BACK} months")

    teams = TeamResolver(token)
    log("Resolving PRs (files, reviews, codeowners) ...")
    rows = build_rows(token, rules, teams, prs, now)

    os.makedirs(OUT_DIR, exist_ok=True)
    out_html = os.path.join(OUT_DIR, "index.html")
    with open(out_html, "w", encoding="utf-8") as fh:
        fh.write(render_html(rows, now, cutoff))
    log(f"Wrote {out_html} ({len(rows)} rows)")

    # Machine-readable companion output.
    with open(os.path.join(OUT_DIR, "data.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "generated_at": now.isoformat(),
                "cutoff": cutoff.isoformat(),
                "source_repo": f"{SRC_OWNER}/{SRC_REPO}",
                "warnings": sorted(set(WARNINGS)),
                "pull_requests": [
                    {
                        "number": r["number"],
                        "url": r["url"],
                        "title": r["title"],
                        "draft": r["draft"],
                        "created_at": r["created"].isoformat(),
                        "age": r["age_text"],
                        "changed_files": r["files"],
                        "owner_groups": r["groups"],
                        "codeowners": r["codeowners"],
                    }
                    for r in rows
                ],
            },
            fh,
            indent=1,
        )
    log("Done.")


if __name__ == "__main__":
    main()
