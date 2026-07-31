# tt-metal PR review requests

A small static site that answers one question for every open pull request in
[`tenstorrent/tt-metal`](https://github.com/tenstorrent/tt-metal):

> **Whose approval is still needed to unblock this PR?**

**Live page:** <https://blozano-tt.github.io/tt-metal-pr-review-requests/>

The page is a four-column table — **PR**, **Title**, **Age**, **Codeowners** —
covering every open, non-draft PR created in the last 2 months, sorted by PR
number. A machine-readable `data.json` is published alongside it.

## How it works

`scripts/generate.py` runs on a schedule and:

1. Reads `.github/CODEOWNERS` from the tt-metal default branch (current rules;
   no historical reconstruction).
2. Lists open PRs newest-first, drops drafts, and stops at the 2-month cutoff.
3. For each PR, collects the changed files and the review history.
4. Maps each changed file to its owning CODEOWNERS line (**last matching
   pattern wins**, per GitHub's documented precedence), dedupes the distinct
   owner-groups, expands `@org/team` entries to individual members, drops any
   group that already has an approval, and unions what's left.
5. Renders `public/index.html` (+ `public/data.json`) and deploys to Pages.

### Refresh cadence

`.github/workflows/refresh.yml` runs:

- on a **cron every 3 hours** (`0 */3 * * *`),
- on **`workflow_dispatch`** (manual trigger),
- on **push to `main`** (so a change regenerates the page immediately).

The page footer shows the "last refreshed" timestamp in UTC.

## Design assumptions

These were judgement calls. They're easy to flip — say the word.

1. **`@tenstorrent/codeowner-bypass` is excluded entirely.** That team appears
   on nearly every CODEOWNERS line as a blanket bypass for certain senior/infra
   folks. Listing its members on every single PR would drown out the signal, so
   the entry is stripped from every matched owner line before resolution and its
   membership is never expanded.

2. **Only *outstanding* approvals are shown.** For each PR the tool takes each
   reviewer's most recent review state and drops any owner-group that already
   has an approval from one of its members. Groups that are already satisfied
   disappear, so the column shows only people who still need to act. A PR whose
   groups are all satisfied (or whose files match no CODEOWNERS rule) shows
   *"— no outstanding codeowners —"*.

   Refinement: `COMMENTED` and `PENDING` reviews are **not** treated as
   superseding an earlier `APPROVED` / `CHANGES_REQUESTED`, matching GitHub's
   actual behaviour — leaving a comment does not retract your approval. Only
   `APPROVED`, `CHANGES_REQUESTED` and `DISMISSED` are decisive. On a 200-PR
   sample this affected ~2% of PRs, which would otherwise have shown reviewers
   as outstanding when GitHub already counts them as approving.

3. **Sort order is PR number ascending** (oldest first, lowest number at the
   top). This reverses an earlier descending default, so the longest-waiting
   PRs surface first. Add `reverse=True` to the `rows.sort(...)` call in
   `build_rows()` to flip it back.

4. **Draft PRs are excluded.** (This reverses an earlier assumption: the
   original request only said "open PRs", so drafts were initially included.)
   GitHub's GraphQL `pullRequests` connection has no server-side draft filter,
   so drafts are dropped client-side in `fetch_prs()`, with a second check in
   `build_rows()` as a safety net. Set `INCLUDE_DRAFTS = True` to restore them.

5. **The window is the trailing 2 months.** (Also reduced from an earlier
   3 months.) Controlled by `MONTHS_BACK`; the cutoff is computed per run as
   calendar months before the run time, with the day clamped to the target
   month's length.

6. **Long titles are truncated** at `TITLE_MAX_CHARS` (110) with an ellipsis, so
   one very long title can't blow out the table layout. The full untruncated
   title is preserved as the cell's `title` tooltip and in `data.json`.

7. **Only individuals are listed, never teams.** Team handles are expanded to
   member logins. If a team cannot be read (see below) the raw `@org/team`
   handle is shown as a fallback rather than silently dropped, and a warning is
   surfaced both in the build log and in a banner on the page.

### Stat tiles link to GitHub — and are approximations

The summary tiles above the table link through to the equivalent search on
`github.com/tenstorrent/tt-metal/pulls`. All tiles share the same base filters
as this page's dataset — `is:pr is:open -is:draft created:>=<cutoff>` — plus:

| Tile | Extra qualifier |
| --- | --- |
| open PRs (non-draft) | *(none)* |
| awaiting codeowner approval | `review:required` |
| no outstanding codeowners | `review:approved` |
| distinct reviewers needed | *not linked* |

**These counts will not match GitHub's exactly, by design.** GitHub's search has
no qualifier for "this PR has at least one CODEOWNERS group with no approval
from any of its members" — which is precisely what this page computes.
`review:required` and `review:approved` are the closest supported qualifiers, so
the linked searches are a best-effort neighbourhood, not a reproduction of the
page's own numbers. Each tile's tooltip says so. "Distinct reviewers needed" has
no sensible GitHub-side equivalent at all, so it is deliberately left as plain
text rather than forced into a nonsensical link.

### CODEOWNERS pattern matching

Matching uses [`pathspec`](https://pypi.org/project/pathspec/)'s `gitwildmatch`
rather than hand-rolled `fnmatch`, with one deliberate correction: GitHub's
CODEOWNERS docs state that

```
/* @octocat
```

owns "any file in the root of your repository **but not in subdirectories**",
whereas plain gitignore semantics would let `/*` match a top-level *directory*
and therefore everything beneath it. So for anchored patterns whose final
segment is a glob and which contain no `**` (e.g. `/*`, `tt_metal/tools/*`,
`.github/workflows/models-t1*.yaml`), a depth-exact check is applied. Patterns
ending in `/`, and patterns containing `**`, keep normal recursive behaviour.

Without this correction the leading `/*` rule would have silently attributed
every otherwise-unowned file in the repo to the top-level owners.

## Optional secret: `TT_METAL_READ_TOKEN`

The workflow reads its token as:

```yaml
GH_TOKEN: ${{ secrets.TT_METAL_READ_TOKEN || secrets.GITHUB_TOKEN }}
```

**No secret has been created — this is intentional and needs a human decision.**

`TT_METAL_READ_TOKEN` is optional. If you add it, it only needs:

- **read access to `tenstorrent/tt-metal` contents and pull requests** — trivial,
  since the repo is public; and
- **read access to `tenstorrent` org team membership** (`read:org`), which is
  the only part that genuinely requires elevated rights. It is used solely to
  expand `@tenstorrent/<team>` CODEOWNERS entries into individual usernames.

Please mint a **new, minimally-scoped, read-only** token for this (a fine-grained
PAT with `Members: read` on the `tenstorrent` org, or a classic PAT with just
`read:org`). Do **not** reuse a broadly-scoped administrative token: this is a
public repository, and any token placed in its Actions secrets is exposed to
every workflow that runs here.

**Without the secret** the workflow falls back to the automatic `GITHUB_TOKEN`.
Everything still works, except that org team membership may not be readable — in
that case the script catches the 403/404, keeps the raw `@tenstorrent/<team>`
handle in the Codeowners column instead of crashing, and shows a warning banner
on the page listing which teams could not be expanded.

## Running locally

```bash
pip install -r requirements.txt
GH_TOKEN=$(gh auth token) python scripts/generate.py   # writes ./public
```

Set `OUT_DIR` to change the output directory.
