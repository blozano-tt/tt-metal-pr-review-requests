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
import shutil
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
    """
    Pick the first token that actually authenticates.

    `GH_TOKEN` (the optional narrowly-scoped PAT) is preferred because it can
    usually read org team membership, but a revoked or expired PAT must not take
    the whole dashboard down when the automatic `GITHUB_TOKEN` still works. Each
    candidate is probed against /rate_limit, which does not consume quota.
    """
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = (os.environ.get(name) or "").strip()
        if value and value not in seen:
            seen.add(value)
            candidates.append((name, value))
    if not candidates:
        sys.exit("ERROR: set GH_TOKEN or GITHUB_TOKEN")

    for index, (name, value) in enumerate(candidates):
        resp = SESSION.get(f"{API}/rate_limit", headers=_headers(value), timeout=30)
        if resp.status_code == 200:
            if index:
                warn(f"falling back to ${name} for API access")
            log(f"Authenticated with ${name}")
            return value
        remaining = candidates[index + 1:]
        warn(
            f"${name} failed authentication (HTTP {resp.status_code})"
            + (f"; trying ${remaining[0][0]}" if remaining else "")
        )
    sys.exit("ERROR: none of the supplied tokens could authenticate")


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
        author { login url }
        mergeable
        commits(last: 1) {
          nodes { commit { statusCheckRollup { state } } }
        }
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


MERGEABLE_RETRY_ROUNDS = 3
MERGEABLE_RETRY_DELAY = 8      # seconds between rounds
MERGEABLE_REFRESH_BATCH = 50   # PRs per aliased query


def refresh_unknown_mergeable(token: str, prs: list[dict]) -> None:
    """
    Re-poll PRs whose `mergeable` came back UNKNOWN, updating them in place.

    GitHub computes mergeability lazily: the first request for a PR only
    *enqueues* a background merge test and returns UNKNOWN, and every cached
    answer for the repo is invalidated whenever the base branch moves. On a
    repo as busy as tt-metal that means a build can easily catch ~90% of PRs
    mid-computation -- one observed run had 401 of 445 UNKNOWN, which left the
    entire Conflicts axis showing nothing but the fallback badge. GitHub's own
    guidance is to poll until the value is non-null, which is what this does.

    Cheap: the retry query asks only for `number` and `mergeable`, so a whole
    round over 400 PRs is ~8 shallow requests.
    """
    pending = [p for p in prs if p.get("mergeable") == "UNKNOWN"]
    if not pending:
        return
    by_number = {p["number"]: p for p in prs}
    log(f"  {len(pending)} PRs have UNKNOWN mergeability; polling GitHub to settle it")

    for round_no in range(1, MERGEABLE_RETRY_ROUNDS + 1):
        time.sleep(MERGEABLE_RETRY_DELAY)
        for start in range(0, len(pending), MERGEABLE_REFRESH_BATCH):
            chunk = pending[start:start + MERGEABLE_REFRESH_BATCH]
            aliases = " ".join(
                f"p{p['number']}: pullRequest(number: {p['number']}) "
                "{ number mergeable }"
                for p in chunk
            )
            query = (
                "query($owner:String!, $name:String!) { "
                "repository(owner:$owner, name:$name) { " + aliases + " } }"
            )
            data = graphql(token, query, {"owner": SRC_OWNER, "name": SRC_REPO})
            for node in (data.get("repository") or {}).values():
                if not isinstance(node, dict):
                    continue
                state = node.get("mergeable")
                if state and state != "UNKNOWN" and node["number"] in by_number:
                    by_number[node["number"]]["mergeable"] = state
        settled = [p for p in pending if p.get("mergeable") != "UNKNOWN"]
        pending = [p for p in pending if p.get("mergeable") == "UNKNOWN"]
        log(
            f"  mergeability round {round_no}: settled {len(settled)}, "
            f"{len(pending)} still unknown"
        )
        if not pending:
            break

    if pending:
        # Not fatal: those rows render the "merge unknown" badge, which is what
        # it is for. Only shout if it is a big enough share to matter.
        share = len(pending) / max(1, len(prs))
        msg = (
            f"{len(pending)} of {len(prs)} PRs still had UNKNOWN mergeability after "
            f"{MERGEABLE_RETRY_ROUNDS} polling rounds; their Conflicts badge is "
            "'merge unknown'"
        )
        if share > 0.2:
            warn(msg)
        else:
            log(f"  note: {msg}")


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


def latest_review_states(reviews: list[dict]) -> dict[str, str]:
    """
    Map lowercased login -> that reviewer's most recent *decisive* review state.

    Single source of truth for both the Codeowners column (who has approved)
    and the Status column's review axis (who has requested changes), so the two
    can never disagree.
    """
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
    return {login: state for login, (_, state) in latest.items()}


def approvers(reviews: list[dict]) -> set[str]:
    """Logins whose most recent *decisive* review state is APPROVED."""
    return {
        login for login, state in latest_review_states(reviews).items()
        if state == "APPROVED"
    }


# --------------------------------------------------------------------------
# Status badges
# --------------------------------------------------------------------------

# Single vocabulary shared by the Status cells and the on-page legend, so the
# two can never drift apart. Keyed by badge id:
#   (emoji, short label used in the cell, long label used in the legend,
#    CSS tone class, tooltip / legend explanation)
#
# Three independent axes. A PR can be blocked on more than one at once, so the
# cell renders one badge per axis rather than a single collapsed verdict.
BADGES: dict[str, tuple[str, str, str, str, str]] = {
    # Review axis -- driven entirely by the same data as the Codeowners column.
    "review-approved": (
        "✅", "approved", "approved", "ok",
        "Every CODEOWNERS group with a say on this PR already has an approval "
        "(the Codeowners column is empty).",
    ),
    "review-awaiting": (
        "👀", "awaiting review", "awaiting review", "muted",
        "At least one CODEOWNERS group still has no approval from any of its "
        "members -- see the Codeowners column.",
    ),
    "review-changes": (
        "✋", "changes requested", "changes requested", "bad",
        "At least one reviewer's most recent decisive review state is "
        "CHANGES_REQUESTED.",
    ),
    # Merge axis -- GitHub's `mergeable` field.
    "merge-clean": (
        "🔀", "no conflicts", "no merge conflicts", "muted",
        "GitHub reports the branch as mergeable into its base.",
    ),
    "merge-conflict": (
        "⚠️", "conflicts", "merge conflicts", "bad",
        "GitHub reports conflicts with the base branch; the author needs to "
        "rebase or merge.",
    ),
    "merge-unknown": (
        "❔", "merge unknown", "mergeability not yet known", "muted",
        "GitHub computes mergeability lazily and had not finished when this "
        "page was built.",
    ),
    # CI axis -- the status-check rollup of the PR's most recent commit.
    "checks-pass": (
        "🟢", "checks pass", "checks passing", "ok",
        "The status-check rollup on the latest commit is SUCCESS.",
    ),
    "checks-fail": (
        "🔴", "checks fail", "checks failing", "bad",
        "The status-check rollup on the latest commit is FAILURE or ERROR.",
    ),
    "checks-pending": (
        "🟡", "checks pending", "checks still running", "warn",
        "The status-check rollup on the latest commit is PENDING or EXPECTED.",
    ),
    "checks-none": (
        "⚪", "no checks", "no checks reported", "muted",
        "The latest commit has no status-check rollup at all.",
    ),
}

MERGEABLE_BADGE = {
    "MERGEABLE": "merge-clean",
    "CONFLICTING": "merge-conflict",
    "UNKNOWN": "merge-unknown",
}

CHECKS_BADGE = {
    "SUCCESS": "checks-pass",
    "FAILURE": "checks-fail",
    "ERROR": "checks-fail",
    "PENDING": "checks-pending",
    "EXPECTED": "checks-pending",
}


def checks_state(pr: dict) -> str | None:
    """
    Rollup state of the PR's latest commit, or None when GitHub reports no
    rollup (a PR with no CI configured, or with checks not yet created).
    """
    nodes = ((pr.get("commits") or {}).get("nodes")) or []
    if not nodes:
        return None
    rollup = ((nodes[0] or {}).get("commit") or {}).get("statusCheckRollup")
    if not rollup:
        return None
    return rollup.get("state")


# Per-axis "blocked-ness" rank, used only for sorting. Higher = more blocked.
# Ordering judgement inside each axis:
#   review  -- an approval is the goal, so approved < awaiting < changes-requested.
#   merge   -- unknown sits between clean and conflicting: it is not known to be
#              a problem, but it is not known to be fine either.
#   checks  -- "no checks" ranks above "passing" but below "pending": nothing is
#              wrong, but nothing has vouched for the PR either. Failing is worst.
STATUS_AXIS_RANK = {
    "review-approved": 0, "review-awaiting": 1, "review-changes": 2,
    "merge-clean": 0, "merge-unknown": 1, "merge-conflict": 2,
    "checks-pass": 0, "checks-none": 1, "checks-pending": 2, "checks-fail": 3,
}


def status_score(badge_ids: list[str]) -> int:
    """
    Collapse the three Status axes into one deterministic sort key.

    Status has no natural total order -- it is three independent axes, which is
    the whole reason the column renders separate badges. For *sorting* we need
    one number, so this packs the axes into digits, review-major:

        review * 100 + merge * 10 + checks      (higher = more blocked)

    Review is the most significant digit because "who still has to act" is what
    this page is about; conflicts outrank CI because a conflict blocks the merge
    button outright while a red check may be a flake. Sorting Status descending
    therefore surfaces "unapproved AND conflicting AND failing" first and
    "approved, clean, green" last. `max()` on the review axis is what makes the
    approved-plus-changes-requested rows (which carry two review badges) rank as
    changes-requested, i.e. the blocking one wins.
    """
    def axis(prefix: str, default: int = 0) -> int:
        ranks = [STATUS_AXIS_RANK[b] for b in badge_ids if b.startswith(prefix)]
        return max(ranks) if ranks else default

    return axis("review-") * 100 + axis("merge-") * 10 + axis("checks-")


def status_badges(outstanding: bool, changes_requested: bool,
                  mergeable: str | None, checks: str | None) -> list[str]:
    """Badge ids for one PR: review axis (1-2), merge axis (1), CI axis (1)."""
    ids = ["review-awaiting" if outstanding else "review-approved"]
    # Deliberately additive, not a precedence chain: "all codeowner groups have
    # an approval AND someone has requested changes" is a real, and genuinely
    # blocking, combination. Collapsing it either way would lose information.
    if changes_requested:
        ids.append("review-changes")
    ids.append(MERGEABLE_BADGE.get(mergeable or "", "merge-unknown"))
    ids.append(CHECKS_BADGE.get(checks or "", "checks-none"))
    return ids


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
        # One pass over the review history feeds both columns.
        latest_states = latest_review_states(reviews)
        approved_by = {l for l, s in latest_states.items() if s == "APPROVED"}
        changes_requested_by = sorted(
            l for l, s in latest_states.items() if s == "CHANGES_REQUESTED"
        )

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
        # Exact age in seconds for the client-side sort. The rendered "3 days"
        # string cannot be sorted as text ("2 days" would precede "10 days"),
        # and age_days floors sub-day PRs to 0, so it cannot order them either.
        age_seconds = int((now - created).total_seconds())
        # author is null for PRs opened by a since-deleted account.
        author = pr.get("author") or {}
        mergeable = pr.get("mergeable")
        checks = checks_state(pr)
        rows.append(
            {
                "number": pr["number"],
                "url": pr["url"],
                "title": pr["title"],
                "draft": pr["isDraft"],
                "created": created,
                "age_text": age_text,
                "age_days": age_days,
                "age_seconds": age_seconds,
                "files": len(files),
                "groups": len(groups),
                "codeowners": sorted(outstanding, key=str.lower),
                "author": author.get("login"),
                "author_url": author.get("url"),
                "changes_requested_by": changes_requested_by,
                "mergeable": mergeable,
                "checks": checks,
                "badges": status_badges(
                    bool(outstanding), bool(changes_requested_by), mergeable, checks
                ),
            }
        )
        if i % 50 == 0 or i == len(prs):
            log(f"  processed {i}/{len(prs)} PRs")
    if unowned_only:
        log(f"  note: {unowned_only} PRs had no CODEOWNERS-matched files")
    rows.sort(key=lambda r: r["number"])  # ascending PR number (oldest first)
    return rows


# NOTE: raw string. A non-raw literal here once turned the CSS escape "\2197"
# (an up-arrow glyph) into Python's octal escape \21 followed by a literal "97",
# which rendered a stray "97" after every linked stat tile.
CSS = r"""
/* Layout intent: the fractal is meant to be SEEN. Every piece of text lives on
   its own near-opaque card, which keeps contrast a fixed, checkable number; the
   scrim is then kept deliberately light so the image stays vivid in all the
   negative space around those cards (gutters, hero band, gaps, footer). */
:root {
  --bg: #0f1116; --panel: rgba(20,23,30,.95); --panel-solid: #14171e;
  --line: #333a49; --text: #e6e9ef; --muted: #a3abbd; --link: #8cc0ff;
  --accent: #5ce68d; --chip: rgba(38,44,58,.95);
  --bad: #ff8b8b; --warn: #ffc457;
  --scrim: rgba(8,10,16,.34); --img-filter: brightness(.92) saturate(1.1);
  --shadow: 0 8px 30px rgba(0,0,0,.50);
}
@media (prefers-color-scheme: light) {
  :root { --bg:#f7f8fa; --panel:rgba(255,255,255,.96); --panel-solid:#fff;
          --line:#dfe3ea; --text:#1a1d24; --muted:#4a5260; --link:#0a44a0;
          --accent:#0f6b35; --chip:rgba(238,241,246,.96);
          --bad:#a51023; --warn:#7a4a00;
          --scrim: rgba(247,248,250,.46); --img-filter: brightness(1.05) saturate(1);
          --shadow: 0 8px 30px rgba(18,22,40,.28); }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
/* Decorative Mandelbrot backdrop. Two fixed layers behind all content:
   the image, then a scrim that buys back the contrast the table needs.
   Fixed positioning (rather than relying on background-attachment alone)
   keeps it steady under a 400+ row scroll without repaint cost. */
body::before, body::after { content:""; position:fixed; inset:0;
  pointer-events:none; }
body::before {
  background-image:url("mandelbrot.jpg");
  background-size:cover; background-position:center; background-attachment:fixed;
  filter:var(--img-filter);
  z-index:-2;
}
body::after { background:var(--scrim); z-index:-1; }
@media (prefers-reduced-motion: reduce) {
  body::before { background-attachment: scroll; }
}
/* Roomy gutters + a tall hero band and footer gap: this is the negative space
   the fractal actually shows through, so it is deliberately generous. */
.wrap { max-width: 1120px; margin: 0 auto; padding: 128px 30px 132px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:12px;
  box-shadow:var(--shadow); }
.hero { padding:20px 22px; margin:0 0 18px; }
h1 { font-size: 22px; margin: 0 0 6px; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 13px; margin: 0; }
.sub a { color: var(--link); }
footer.card { padding:14px 18px; margin-top:22px; }
.stats { display:flex; gap:10px; flex-wrap:wrap; margin: 0 0 20px; }
.stat { background:var(--panel); border:1px solid var(--line); border-radius:10px;
  box-shadow:var(--shadow); padding:10px 15px; font-size:13px; }
.stat b { font-size:17px; display:block; color:var(--text); }
a.stat { color:var(--link); text-decoration:none; transition:border-color .12s ease; }
a.stat:hover, a.stat:focus-visible { border-color:var(--link); text-decoration:underline; }
a.stat::after { content:" ↗"; font-size:11px; opacity:.75; }
.filterbar { display:flex; align-items:center; gap:10px; flex-wrap:wrap;
  background:var(--panel); border:1px solid var(--line); border-radius:12px;
  box-shadow:var(--shadow); padding:12px 15px; margin:0 0 18px; font-size:13px; }
.filterbar[hidden] { display:none; }
.filterbar.active { border-color:var(--link); }
.filterbar label { color:var(--muted); }
.filterbar input { background:var(--bg); color:var(--text); font:inherit;
  border:1px solid var(--line); border-radius:6px; padding:6px 10px; min-width:220px; }
.filterbar input:focus { outline:none; border-color:var(--link); }
.filterbar button { background:var(--chip); color:var(--text); font:inherit;
  border:1px solid var(--line); border-radius:6px; padding:6px 12px; cursor:pointer; }
.filterbar button:hover { border-color:var(--link); color:var(--link); }
#filterStatus { color:var(--muted); }
.filterbar.active #filterStatus { color:var(--text); font-weight:600; }
tr[hidden] { display:none; }
/* `overflow:hidden` would clip the rounded corners for us, but it also creates a
   clip context that silently kills `position:sticky` on the header. Use
   border-collapse:separate + per-corner radii so the header can actually pin. */
table { width:100%; border-collapse:separate; border-spacing:0;
  background:var(--panel); border:1px solid var(--line); border-radius:12px;
  box-shadow:var(--shadow); }
thead th:first-child { border-top-left-radius:11px; }
thead th:last-child { border-top-right-radius:11px; }
tbody tr:last-child td:first-child { border-bottom-left-radius:11px; }
tbody tr:last-child td:last-child { border-bottom-right-radius:11px; }
th, td { text-align:left; padding:9px 14px; border-bottom:1px solid var(--line);
  vertical-align: top; }
/* Sticky header must be fully opaque or rows ghost through it as they scroll. */
th { position: sticky; top:0; background:var(--panel-solid); font-size:12px;
  text-transform:uppercase; letter-spacing:.06em; color:var(--muted); z-index:1; }
/* Sort controls are real <button>s so Enter/Space and focus come for free, but
   they must look exactly like the header text they replaced. */
.sortbtn { font:inherit; color:inherit; text-transform:inherit;
  letter-spacing:inherit; background:none; border:0; padding:0; margin:0;
  cursor:pointer; display:inline-flex; align-items:center; gap:4px;
  white-space:nowrap; }
.sortbtn:hover { color:var(--text); }
.sortbtn:focus-visible { outline:2px solid var(--link); outline-offset:3px;
  border-radius:3px; }
/* The arrow is always in the DOM but only inked on the active column, so
   turning sort on and off cannot shift the header's width. */
.arrow::after { content:"\2191"; opacity:0; font-size:11px; }
th.sorted .sortbtn { color:var(--text); }
th.sorted .arrow::after { opacity:1; }
th.sorted.desc .arrow::after { content:"\2193"; }
tr:last-child td { border-bottom:none; }
td.pr { white-space:nowrap; font-variant-numeric: tabular-nums; }
td.age { white-space:nowrap; color:var(--muted); font-variant-numeric: tabular-nums; }
td.pr, th:nth-child(1), td.age, th:nth-child(3),
td.author, th:nth-child(5), td.status, th:nth-child(6) { width:1%; }
/* Title is the only column that can give ground, so it is the one that absorbs
   the two new columns. `overflow-wrap: anywhere` (unlike `break-word`) also
   shrinks the column's *min-content* width, which is what keeps the table
   inside its card: with Author and Status added, Title's longest unbreakable
   word alone pushed the table 68px past the card edge. An earlier attempt at
   `anywhere` was reverted because the owner chips then squeezed Title to a few
   characters per line -- the explicit percentage below is what makes it safe
   now, since it gives Title a floor the chips cannot bid away. */
th:nth-child(2) { width:28%; }
td.title { min-width:200px; overflow-wrap:anywhere; }
td.author { white-space:nowrap; }
/* Wide enough that the longest badge ("changes requested") never wraps
   mid-label, narrow enough that badges stack instead of stretching the row. */
td.status { min-width:152px; }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
.owner { display:inline-block; background:var(--chip); border:1px solid var(--line);
  border-radius:5px; padding:1px 6px; margin:2px 3px 2px 0; font-size:13px;
  white-space:nowrap; }
.none { color: var(--accent); }
/* Status badges. Colour is a redundant cue only: every badge also carries an
   emoji and a text label, so the cell still reads correctly in monochrome. */
.badge { display:inline-block; background:var(--chip); border:1px solid var(--line);
  border-radius:5px; padding:1px 6px; margin:2px 3px 2px 0; font-size:12px;
  white-space:nowrap; color:var(--muted); }
.badge.ok { color:var(--accent); }
.badge.bad { color:var(--bad); }
.badge.warn { color:var(--warn); }
.badge i { font-style:normal; margin-right:4px; }
.legend { padding:11px 15px; margin:0 0 18px; font-size:12px; color:var(--muted); }
.legend .row { display:flex; align-items:baseline; gap:8px; flex-wrap:wrap;
  margin-top:6px; }
.legend .row:first-of-type { margin-top:4px; }
.legend .axis { min-width:74px; color:var(--text); font-weight:600; }
.legend strong { color:var(--text); font-size:13px; }
.warnbox { background:var(--panel); border:1px solid var(--line); border-left:3px solid #d29922;
  border-radius:12px; box-shadow:var(--shadow); padding:12px 16px; margin-bottom:18px; font-size:13px; }
.warnbox ul { margin:6px 0 0; padding-left:18px; }
footer { color:var(--muted); font-size:12px; }
/* Horizontal scroll container for the table. Deliberately NOT scrollable at
   desktop widths: any scrolling overflow value here makes this element the
   sticky header's scrollport, and because it never scrolls vertically the
   header stops pinning -- the same class of bug as the overflow:hidden one
   fixed in e66c28b. So it stays overflow:visible until the table cannot fit. */
.scrollhint { margin:0 0 8px; font-size:12px; color:var(--muted); }
.scrollhint[hidden] { display:none; }

/* Below ~1040px the six-column table's min-content width (~957px) exceeds the
   content column, so without this the *whole page* slid sideways -- up to
   630px on a 375px phone, dragging the cards out of view. Give the table its
   own scroller instead. Accepted cost: the sticky header does not pin below
   this width. No CSS allows both (overflow-y:visible computes to auto as soon
   as overflow-x scrolls), and a page that scrolls sideways is the worse bug. */
@media (max-width: 1040px) {
  .tablewrap { overflow-x:auto; overscroll-behavior-x:contain; }
}
@media (max-width: 700px) {
  /* A 128px hero band and 30px gutters are generous on a 1120px column and
     wasteful on a 375px one; this buys the table ~28px of width back. */
  .wrap { padding:72px 16px 76px; }
  h1 { font-size:20px; }
  /* `cover` on a tall narrow viewport zooms ~3x into the set's black interior,
     which reads as a featureless grey field. Framing off-centre keeps the
     antenna and boundary filigree -- the parts that look like a Mandelbrot --
     in shot. `scroll` is repeated from the pointer query below because that
     query cannot be verified in a headless browser (it reports a fine pointer
     even under device emulation), and a width query certainly matches a phone. */
  body::before { background-position:18% center; background-attachment:scroll; }
}
/* background-attachment:fixed is unreliable on touch browsers (iOS Safari in
   particular ignores or mis-sizes it, with repaint jank while scrolling). The
   layer is already position:fixed, so `scroll` renders identically here and
   simply avoids that code path. Mirrors the existing reduced-motion fallback. */
@media (hover: none) and (pointer: coarse) {
  body::before { background-attachment:scroll; }
}
"""


# Purely client-side: no network calls, no auth, no backend. Matches the typed
# text against each row's data-owners attribute (lowercased, @-prefixed logins).
# Raw string so backslashes reach the browser intact (see the CSS note above).
FILTER_JS = r"""
(function () {
  var KEY = 'ttMetalCodeownerFilter';
  var bar = document.getElementById('filterBar');
  var input = document.getElementById('ownerFilter');
  var clearBtn = document.getElementById('ownerFilterClear');
  var status = document.getElementById('filterStatus');
  var noMatches = document.getElementById('noMatches');
  var noMatchesClear = document.getElementById('noMatchesClear');
  if (!bar || !input) { return; }

  // Only real data rows carry data-owners, so the "no matches" row is excluded.
  var rows = Array.prototype.slice.call(
    document.querySelectorAll('#prTable tbody tr[data-owners]')
  );
  var total = rows.length;

  function store(value) {
    try {
      if (value) { localStorage.setItem(KEY, value); }
      else { localStorage.removeItem(KEY); }
    } catch (e) { /* private mode / storage disabled: filtering still works */ }
  }

  function apply(raw) {
    var typed = (raw || '').trim();
    // GitHub logins are case-insensitive; a leading @ is optional.
    var needle = typed.toLowerCase().replace(/^@+/, '');
    var shown = 0;
    for (var i = 0; i < total; i++) {
      var hit = needle === '' ||
        rows[i].getAttribute('data-owners').indexOf(needle) !== -1;
      rows[i].hidden = !hit;
      if (hit) { shown++; }
    }
    if (needle === '') {
      bar.classList.remove('active');
      status.textContent = 'Showing all ' + total + ' PRs.';
      if (noMatches) { noMatches.hidden = true; }
    } else {
      bar.classList.add('active');
      status.textContent = 'Filter active — showing ' + shown + ' of ' +
        total + ' PRs awaiting ' + typed + '. Clear the box to see all.';
      if (noMatches) { noMatches.hidden = shown !== 0; }
    }
    store(typed);
  }

  function reset() {
    input.value = '';
    apply('');
    input.focus();
  }

  input.addEventListener('input', function () { apply(input.value); });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { reset(); }
  });
  clearBtn.addEventListener('click', reset);
  if (noMatchesClear) {
    noMatchesClear.addEventListener('click', function (e) {
      e.preventDefault();
      reset();
    });
  }

  var saved = '';
  try { saved = localStorage.getItem(KEY) || ''; } catch (e) { saved = ''; }
  if (saved) { input.value = saved; }

  bar.hidden = false;   // reveal only once wired up
  apply(input.value);
}());
"""


# Click-to-sort on the column headers. Purely client-side, like the filter:
# every sort key is already on the row as a data-* attribute, so this never
# parses rendered text and never touches the network.
SORT_JS = r"""
(function () {
  var table = document.getElementById('prTable');
  if (!table) { return; }
  var tbody = table.tBodies[0];
  var noMatches = document.getElementById('noMatches');
  // Only real data rows move; the "no matches" row is re-pinned to the end.
  var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr[data-owners]'));
  if (!rows.length) { return; }

  // key -> [attribute, kind]. 'num' compares numerically, 'str' compares
  // case-insensitively (the attributes are already lowercased server-side).
  var KEYS = {
    pr:         ['data-number',   'num'],
    title:      ['data-title',    'str'],
    age:        ['data-age',      'num'],
    codeowners: ['data-owncount', 'num'],
    author:     ['data-author',   'str'],
    status:     ['data-status',   'num']
  };

  var current = 'pr';      // the page ships sorted by PR ascending
  var ascending = true;

  function value(row, key) {
    var spec = KEYS[key];
    var raw = row.getAttribute(spec[0]) || '';
    return spec[1] === 'num' ? parseFloat(raw) || 0 : raw;
  }

  function compare(a, b, key) {
    var av = value(a, key), bv = value(b, key);
    if (av < bv) { return -1; }
    if (av > bv) { return 1; }
    // Codeowners is a count, so ties are common and meaningless on their own:
    // fall back to the alphabetically-first outstanding handle before the
    // universal PR-number tie-break.
    if (key === 'codeowners') {
      var af = a.getAttribute('data-ownfirst') || '';
      var bf = b.getAttribute('data-ownfirst') || '';
      if (af !== bf) { return af < bf ? -1 : 1; }
    }
    return 0;
  }

  function apply(key) {
    var sorted = rows.slice().sort(function (a, b) {
      var d = compare(a, b, key);
      if (d !== 0) { return ascending ? d : -d; }
      // Deterministic final tie-break, always ascending by PR number, so the
      // order never depends on the browser's sort stability.
      return value(a, 'pr') - value(b, 'pr');
    });

    // One reflow instead of 449: detach into a fragment, then re-attach.
    // Re-appending existing nodes preserves each row's `hidden` state, so an
    // active username filter keeps hiding exactly the same rows and what the
    // user sees is the filtered set, re-ordered.
    var frag = document.createDocumentFragment();
    for (var i = 0; i < sorted.length; i++) { frag.appendChild(sorted[i]); }
    tbody.appendChild(frag);
    if (noMatches) { tbody.appendChild(noMatches); }

    var ths = table.querySelectorAll('thead th');
    for (var j = 0; j < ths.length; j++) {
      var active = ths[j].getAttribute('data-col') === key;
      ths[j].setAttribute('aria-sort', active ? (ascending ? 'ascending' : 'descending') : 'none');
      ths[j].classList.toggle('sorted', active);
      ths[j].classList.toggle('desc', active && !ascending);
    }
  }

  table.querySelector('thead').addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('.sortbtn') : null;
    if (!btn) { return; }
    var key = btn.getAttribute('data-key');
    if (!KEYS[key]) { return; }
    // Same column toggles direction; a new column starts ascending.
    ascending = (key === current) ? !ascending : true;
    current = key;
    apply(key);
  });

  // Reflect the initial PR-ascending state in the arrow without reordering.
  apply('pr');
}());
"""


# Shows the sideways-scroll hint only when the table actually overflows its
# container, which depends on viewport width and on the widest chip in the
# current dataset -- neither of which a media query can know.
SCROLLHINT_JS = r"""
(function () {
  var wrap = document.getElementById('tableWrap');
  var hint = document.getElementById('scrollHint');
  if (!wrap || !hint) { return; }
  function sync() {
    hint.hidden = wrap.scrollWidth <= wrap.clientWidth + 1;
  }
  sync();
  window.addEventListener('resize', sync);
  // Once the user has scrolled the table, the hint has done its job.
  wrap.addEventListener('scroll', function () {
    if (wrap.scrollLeft > 8) { hint.hidden = true; }
  }, { passive: true });
}());
"""


def pulls_url(*qualifiers: str) -> str:
    """Build a github.com PR-search URL from real, supported search qualifiers."""
    query = " ".join(qualifiers)
    return (
        f"https://github.com/{SRC_OWNER}/{SRC_REPO}/pulls"
        f"?{urlencode({'q': query})}"
    )


def badge_html(badge_id: str, label_index: int = 1) -> str:
    """Render one status badge. label_index 1 = short (cell), 2 = long (legend)."""
    emoji, short, long, tone, tip = BADGES[badge_id]
    label = short if label_index == 1 else long
    return (
        f"<span class='badge {tone}' title='{html.escape(tip)}'>"
        f"<i aria-hidden='true'>{emoji}</i>{html.escape(label)}</span>"
    )


def legend_html() -> str:
    axes = [
        ("Review", ["review-approved", "review-awaiting", "review-changes"]),
        ("Conflicts", ["merge-clean", "merge-conflict", "merge-unknown"]),
        ("CI checks", ["checks-pass", "checks-fail", "checks-pending", "checks-none"]),
    ]
    rows = "".join(
        f"<div class='row'><span class='axis'>{axis}</span>"
        + "".join(badge_html(b, 2) for b in ids)
        + "</div>"
        for axis, ids in axes
    )
    return (
        "<div class='legend card'><strong>Status legend</strong> &mdash; three "
        "independent reasons a PR may not be merged yet; a PR can show more than "
        "one. Hover any badge for detail."
        f"{rows}</div>"
    )


# (sort key, header label, what ascending means -- shown as the button tooltip
# so the two judgement-call columns are not a mystery in the UI either).
COLUMNS: list[tuple[str, str, str]] = [
    ("pr", "PR", "Sort by PR number"),
    ("title", "Title", "Sort by title, case-insensitive"),
    ("age", "Age", "Sort by exact age; ascending puts the newest PRs first"),
    (
        "codeowners",
        "Codeowners",
        "Sort by how many codeowners are still outstanding (ties broken by the "
        "first handle); ascending puts fully-approved PRs first",
    ),
    ("author", "Author", "Sort by author, case-insensitive"),
    (
        "status",
        "Status",
        "Sort by how blocked the PR is, review first, then conflicts, then "
        "checks; descending puts the most blocked PRs first",
    ),
]


def header_cells_html() -> str:
    """
    Header cells as real <button>s so they are keyboard-operable and announced
    as controls for free -- no tabindex/keydown plumbing needed, since a button
    already handles Enter and Space.
    """
    cells = []
    for i, (key, label, tip) in enumerate(COLUMNS):
        # The table ships pre-sorted by PR ascending, so say so from the start
        # rather than claiming nothing is sorted.
        aria = "ascending" if key == "pr" else "none"
        cells.append(
            f"<th aria-sort='{aria}' data-col='{html.escape(key)}'>"
            f"<button type='button' class='sortbtn' data-key='{html.escape(key)}' "
            f"title='{html.escape(tip)}'>{html.escape(label)}"
            "<span class='arrow' aria-hidden='true'></span></button></th>"
        )
    return "".join(cells)


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
        "<div class='card hero'>"
        "<h1>tt-metal &mdash; outstanding codeowner reviews</h1>"
        "<p class='sub'>Open, non-draft pull requests in "
        f"<a href='https://github.com/{SRC_OWNER}/{SRC_REPO}'>{SRC_OWNER}/{SRC_REPO}</a> "
        f"created on or after {cutoff:%Y-%m-%d} (last {MONTHS_BACK} months). "
        "The <em>Codeowners</em> column lists the individual accounts whose approval "
        "would still unblock the PR; <em>Status</em> shows why it is not merged yet "
        "across three independent axes (review, merge conflicts, CI checks)."
        "</p></div>",
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
        parts.append("<div class='warnbox card'><strong>Build warnings</strong><ul>")
        parts.extend(f"<li>{html.escape(w)}</li>" for w in uniq[:20])
        if len(uniq) > 20:
            parts.append(f"<li>&hellip; and {len(uniq) - 20} more</li>")
        parts.append("</ul></div>")

    # Hidden until the script runs, so users without JS aren't shown a dead control.
    parts.append(
        "<div class='filterbar card' id='filterBar' hidden>"
        "<label for='ownerFilter'>Filter by GitHub username</label>"
        "<input id='ownerFilter' type='search' autocomplete='off' spellcheck='false'"
        " placeholder='e.g. afuller-TT'>"
        "<button type='button' id='ownerFilterClear'>Clear</button>"
        f"<span id='filterStatus'>Showing all {total} PRs.</span>"
        "</div>"
    )

    parts.append(legend_html())

    # Hidden until the script confirms the table really is wider than its
    # container, so the hint never lies on a viewport where it all fits.
    parts.append(
        "<p class='scrollhint' id='scrollHint' hidden>"
        "The table is wider than this screen &mdash; scroll it sideways to reach "
        "Author and Status. &rarr;</p>"
    )
    parts.append(
        "<div class='tablewrap' id='tableWrap'>"
        "<table id='prTable'><thead><tr>" + header_cells_html()
        + "</tr></thead><tbody>"
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
        # Lowercased, @-prefixed owner list used by the client-side filter.
        owners_attr = " ".join(
            "@" + o.lstrip("@").lower() for o in r["codeowners"]
        )
        if r["author"]:
            handle = html.escape(r["author"])
            url = r["author_url"] or f"https://github.com/{r['author']}"
            author_cell = f"<a href='{html.escape(url)}'>@{handle}</a>"
        else:
            author_cell = "<span class='badge muted'>unknown</span>"
        status_cell = "".join(badge_html(b) for b in r["badges"])
        # Sort keys travel as data-* attributes so the comparators never have to
        # re-parse rendered text. Codeowners sorts on the *count* of outstanding
        # people -- the actionable number, and the only scalar a variable-length
        # chip list really has -- with the alphabetically-first handle as the
        # tie-break so equal-length lists still land in a stable, meaningful
        # order rather than an arbitrary one.
        sort_attrs = (
            f" data-number='{r['number']}'"
            f" data-title='{html.escape(r['title'].lower())}'"
            f" data-age='{r['age_seconds']}'"
            f" data-owncount='{len(r['codeowners'])}'"
            f" data-ownfirst='{html.escape(r['codeowners'][0].lstrip('@').lower() if r['codeowners'] else '')}'"
            f" data-author='{html.escape((r['author'] or '').lower())}'"
            f" data-status='{status_score(r['badges'])}'"
        )
        parts.append(
            f"<tr data-owners='{html.escape(owners_attr)}'{sort_attrs}>"
            f"<td class='pr'><a href='{html.escape(r['url'])}'>"
            f"{SRC_REPO}#{r['number']}</a></td>"
            f"<td class='title' title='{html.escape(full_title)}'>"
            f"{html.escape(shown)}</td>"
            f"<td class='age'>{html.escape(r['age_text'])}</td>"
            f"<td>{owners}</td>"
            f"<td class='author'>{author_cell}</td>"
            f"<td class='status'>{status_cell}</td></tr>"
        )
    parts.append(
        "<tr id='noMatches' hidden><td colspan='6'>"
        "No PRs are waiting on that username. "
        "<a href='#' id='noMatchesClear'>Clear the filter</a> to see all PRs."
        "</td></tr>"
    )
    parts.append("</tbody></table></div>")
    parts.append(
        "<footer class='card'>Last refreshed "
        f"<strong>{now:%Y-%m-%d %H:%M:%S} UTC</strong>. Refreshed automatically every 3 hours. "
        "Sorted by PR number, ascending. Draft PRs excluded. "
        "<a href='https://github.com/blozano-tt/tt-metal-pr-review-requests'>Source &amp; assumptions</a>."
        "</footer>"
    )
    parts.append(f"<script>{FILTER_JS}</script>")
    parts.append(f"<script>{SORT_JS}</script>")
    parts.append(f"<script>{SCROLLHINT_JS}</script>")
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

    refresh_unknown_mergeable(token, prs)

    teams = TeamResolver(token)
    log("Resolving PRs (files, reviews, codeowners) ...")
    rows = build_rows(token, rules, teams, prs, now)

    os.makedirs(OUT_DIR, exist_ok=True)

    # Static assets (the pre-rendered Mandelbrot backdrop) live in the repo and
    # are copied verbatim; nothing about the fractal is computed at page load.
    asset_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
    if os.path.isdir(asset_dir):
        for name in sorted(os.listdir(asset_dir)):
            src = os.path.join(asset_dir, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(OUT_DIR, name))
                log(f"  copied asset {name}")
    else:
        warn("assets/ directory missing; page background will not load")

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
                        "author": r["author"],
                        "changes_requested_by": r["changes_requested_by"],
                        "mergeable": r["mergeable"],
                        "checks": r["checks"],
                        "status": r["badges"],
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
