# tt-metal PR review requests

A small static site that answers one question for every open pull request in
[`tenstorrent/tt-metal`](https://github.com/tenstorrent/tt-metal):

> **Whose approval is still needed to unblock this PR?**

**Live page:** <https://blozano-tt.github.io/tt-metal-pr-review-requests/>

The page is a six-column table — **PR**, **Title**, **Age**, **Codeowners**,
**Author**, **Status** — covering every open, non-draft PR created in the last
2 months, sorted by PR number. A machine-readable `data.json` is published
alongside it.

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

### The Status column

A PR can be stuck for several unrelated reasons at once — approved but
conflicting, or unapproved *and* failing CI — so **Status is three independent
axes rather than one collapsed verdict**. Each cell carries one badge per axis
(occasionally two on the review axis), and an on-page legend above the table
spells all of them out. Colour is a redundant cue only: every badge also has an
emoji and a text label, so it survives monochrome and colour-blind viewing.

| Axis | Badges | Source |
| --- | --- | --- |
| Review | ✅ approved · 👀 awaiting review · ✋ changes requested | this page's own CODEOWNERS computation |
| Conflicts | 🔀 no conflicts · ⚠️ conflicts · ❔ merge unknown | GraphQL `PullRequest.mergeable` |
| CI checks | 🟢 checks pass · 🔴 checks fail · 🟡 checks pending · ⚪ no checks | `commits(last:1) … statusCheckRollup.state` |

Notes on the review axis:

- **"Approved" is exactly "the Codeowners column is empty"** — the same
  outstanding-codeowners set described in assumption 2, not GitHub's own
  `reviewDecision`. The two can legitimately disagree (this page strips the
  bypass team, and resolves groups itself), and a Status badge contradicting
  the cell next to it would be worse than either answer alone.
- **"Changes requested"** uses the same most-recent-decisive-state-per-reviewer
  map (`latest_review_states()`) that feeds the approval check, so `COMMENTED`
  does not clear it either.
- The two are **additive, not a precedence chain**. "Every codeowner group has
  an approval *and* somebody has requested changes" is a real, genuinely
  blocking combination (2 of 449 PRs at the time of writing), so such a row
  shows both ✅ and ✋ rather than silently dropping one.

**`mergeable` needs polling, not a single read.** GitHub computes it lazily: the
first request only *enqueues* a background merge test and returns `UNKNOWN`, and
every cached answer for the repo is invalidated whenever the base branch moves.
On a repo as busy as tt-metal a build can easily catch most PRs mid-computation
— an early build landed with **401 of 445 `UNKNOWN`**, which left the whole
Conflicts axis showing nothing but the fallback badge. `refresh_unknown_mergeable()`
therefore re-polls just the unknown ones (up to 3 rounds, 8s apart, `number` +
`mergeable` only, ~8 shallow requests per round). A typical run now settles
286 → 3 in a single round. Anything still unknown renders ❔, and a build where
more than 20% remain unknown raises a page warning.

A missing `statusCheckRollup` (a PR with no CI at all) renders ⚪ *no checks*
rather than a false "failing".

Both fields are cheap scalar/shallow additions to the existing batched query;
API cost is still dominated by the `files` and `reviews` connections.

### Background image

The page sits on a pre-rendered Mandelbrot set (`assets/mandelbrot.jpg`, 1920x1080,
~112 KB) in the classic escape-time palette: black interior, deep blue field,
magenta/pink/orange/gold boundary glow.

It is generated **once, offline** by `scripts/make_background.py` (numpy + Pillow)
and committed as a repo asset — nothing is computed in the browser, and the site
build has no numpy/Pillow dependency. `generate.py` just copies `assets/` into the
published output.

**Layout approach — "cards floating on the fractal".** The first attempt used a
heavy scrim (80–86%) over a nearly edge-to-edge content column, which washed the
image out to the point of invisibility. The fix inverts the trade:

- the **scrim is light** (34% dark / 46% light), so wherever the image shows, it
  shows *vividly* rather than as a grey ghost;
- every text-bearing block — header, stat tiles, filter bar, table, footer — is a
  **near-opaque card** (95–96%) with a drop shadow, so contrast is a fixed,
  checkable number that does not depend on which fractal pixel is behind it;
- the page has **generous negative space** for the image to occupy: a 128px hero
  band, a 132px footer gap, ~110px side gutters (1120px content column), and
  visible gaps between the cards;
- the sticky table header is **fully opaque**, or rows ghost through it as they scroll.

Net effect: roughly **44% of the viewport is bare fractal** at the top of the
page and **~17%** once scrolled deep into the table (the side gutters).

Contrast is verified against the image's *extreme* pixels (pure black interior
and brightest gold) composited through both the scrim and the card:

| | worst-case ratio | |
|---|---|---|
| Dark mode | **7.30:1** | AAA across the board |
| Light mode | **6.30:1** | comfortably above the 4.5:1 AA bar |

Counter-intuitively this is *better* than the heavy-scrim version, which bottomed
out at 4.02:1 (a fail) because header text sat directly on the scrim. Putting the
text on cards decoupled readability from the background entirely.

JPEG rather than PNG — this is a smooth photographic gradient where PNG runs to
several megabytes.

### Mobile layout

The six-column table's min-content width is ~957px, so below roughly 1040px it
cannot fit the content column. Before this was handled, the **whole page**
scrolled sideways — up to 630px at 375px wide, dragging the cards half out of
view. The table now sits in a `.tablewrap` container that becomes
`overflow-x: auto` under `@media (max-width: 1040px)`, so only the table slides.
A JS-gated hint line ("scroll it sideways to reach Author and Status →") appears
only when the table really is wider than its container, and disappears once you
scroll it.

**The wrapper is deliberately `overflow: visible` on desktop.** Any scrolling
overflow value makes it the sticky header's scrollport, and since it never
scrolls vertically the header stops pinning — the same class of bug as the
`overflow: hidden` one fixed in e66c28b. There is no CSS that gives both a
horizontal scroller and a viewport-sticky header (`overflow-y: visible`
computes to `auto` as soon as `overflow-x` scrolls), so the header does not pin
below 1040px. A page that scrolls sideways is the worse failure.

Two further phone-only adjustments under `@media (max-width: 700px)`:

- **Padding** drops from `128px 30px 132px` to `72px 16px 76px`. The generous
  hero band and gutters exist so the fractal shows around a 1120px column; on a
  375px screen they were just eating the table's width.
- **`background-attachment` switches to `scroll`** (also under
  `(hover: none) and (pointer: coarse)`). The layer is already `position: fixed`
  so this renders identically, but it avoids iOS Safari's long-standing
  unreliability with fixed attachment. The width query is the one that can
  actually be verified — headless Chromium reports a *fine* pointer even under
  device emulation, so the pointer query alone would have been untestable.
- **`background-position` moves to `18% center`.** `cover` on a tall narrow
  viewport zooms ~3x into the set's black interior, which renders as a
  featureless grey field; framing off-centre keeps the antenna and boundary
  filigree in shot.

### Filtering by username

The page has a **"Filter by GitHub username"** box that narrows the table to the
PRs waiting on one person. It is entirely client-side — a small inline script
matching the typed text against a `data-owners` attribute on each row. There is
no backend, no auth, and no network call.

Deliberately **not** GitHub OAuth: this is a static Pages site with nowhere to
run a token exchange, and the device flow is blocked by CORS from browser JS.
Typing your own handle takes one second and needs no permissions.

Behaviour: case-insensitive (GitHub logins are), a leading `@` is optional,
substring matches work so the list narrows as you type, `Esc` or the **Clear**
button resets, and the status line reads
*"Filter active — showing N of M PRs awaiting <name>."* The last value is kept
in `localStorage` so it survives a reload; storage failures are caught, so the
filter still works in private-browsing mode.

The stat tiles keep showing whole-dataset totals even while a filter is active,
because they link out to GitHub searches over the whole dataset — changing their
numbers would contradict where they point. The status line is the authoritative
filtered count.

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

   Title is also the column that gives ground when the table gets crowded: it
   carries `overflow-wrap: anywhere` plus an explicit `28%` width. The wrap mode
   is what lets the six-column table shrink to its card (a single long
   unbreakable word in one title otherwise pushed it 68px past the card edge);
   the explicit width is what stops the Codeowners chips from bidding that space
   away, which is why an earlier `anywhere` attempt had to be reverted.

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

## Optional secret: `TT_METAL_TOKEN`

The workflow passes both tokens to the generator:

```yaml
GH_TOKEN: ${{ secrets.TT_METAL_TOKEN }}
GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

`get_token()` probes each in turn against `/rate_limit` (which costs no quota)
and uses the first that authenticates, preferring `GH_TOKEN`. So a **missing,
revoked or expired** `TT_METAL_TOKEN` degrades to the automatic token with
a warning on the page instead of failing the build.

That fallback is not hypothetical: an earlier secret was added whose value
returned `401 Bad credentials`, and because the workflow then selected it with
`${{ secrets.A || secrets.B }}` — a *presence* test, not a validity test — every
run failed outright until the probe-and-fall-back logic replaced it.

> **Status:** the generator currently authenticates with the automatic
> `GITHUB_TOKEN`, so `@tenstorrent/<team>` entries are **not** expanded to
> individuals and appear as raw team handles on the page. Adding a valid
> `TT_METAL_TOKEN` fixes that. The build log line `Authenticated with $...`
> reports which token was actually used on any given run.

`TT_METAL_TOKEN` is optional. If you add it, it only needs:

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
