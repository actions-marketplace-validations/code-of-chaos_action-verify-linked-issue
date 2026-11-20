# Verify Linked Issue

A production-ready, reusable GitHub Action that **requires every pull request to be linked to an
existing GitHub issue**. If the pull request is not linked, the action comments on the PR and
fails, so it can be used as a required status check.

The action runs entirely from the event context and the GitHub API — it does not need a
checkout of your repository and never executes pull request code.

## What counts as a valid issue link

Only relationships that **GitHub itself records** as a pull-request-to-issue relationship count.
Two link types are supported:

1. **Closing-keyword links** — for example `Closes #123`, `Fixes #45`, `Resolves #67`, including
   cross-repository references such as `other/repo#8`. GitHub records these as a structured
   relationship and exposes them through the GraphQL field `PullRequest.closingIssuesReferences`.
2. **Manually connected issues** — issues linked through the pull request's **Development**
   section (“Link an issue”). GitHub records these as `connected` / `disconnected` timeline
   events and exposes them through the pull request's GraphQL `timelineItems`
   (`CONNECTED_EVENT` / `DisconnectedEvent`).

Additional rules:

- **Open and closed issues both count**, as long as the issue still exists.
- **Deleted or nonexistent issues do not count.**
- **The pull request itself never counts.**
- **Cross-repository issues count** when GitHub exposes the relationship *and* the provided
  token can read the linked issue.
- **Plain-text mentions do not count**: a `#123` in the title, description or a comment — even
  though GitHub renders it as a cross-reference — is not an actual PR-to-issue relationship and
  is deliberately ignored. The action uses structured API data only; it never parses the PR body.

## How detection works (GitHub API behavior and limitations)

Both relationships are read from GitHub's structured API data:

| Link type | API surface used | Notes |
|-----------|------------------|-------|
| Closing keywords (and, per GitHub's schema, manual links too) | GraphQL `PullRequest.closingIssuesReferences` | An `IssueConnection`, fetched with cursor pagination (100 items per page). Its `userLinkedOnly` / `excludeUserLinked` arguments show that GitHub returns keyword *and* manually linked issues here by default. |
| Manual “Development” connections | GraphQL `PullRequest.timelineItems(itemTypes: [CONNECTED_EVENT, DISCONNECTED_EVENT])` | Fetched with cursor pagination. An issue counts while it has more `connected` than `disconnected` events (a link that was removed does not count). The REST equivalents are the `connected` / `disconnected` issue timeline events documented under [Issue event types](https://docs.github.com/en/rest/using-the-rest-api/issue-event-types). |

Known limitations, handled explicitly:

- **GitHub API gaps.** GitHub has occasionally failed to populate
  `closingIssuesReferences` even though the UI shows the link (see
  [community discussion #209162](https://github.com/orgs/community/discussions/209162)). The
  timeline path provides a second, independent source; if *both* sources miss a relationship,
  a linked PR could still fail. If that happens, additionally connect the issue through the
  **Development** section, which always creates a `connected` event.
- **Inconclusive results are never reported as “unlinked”.** API errors, rate limits, missing
  permissions, missing tokens and connection events whose subject issue cannot be read (deleted
  or inaccessible issues) cause the action to fail with an actionable error message and a
  distinct exit code — the failure comment is *not* posted in that case.
- **Cross-repository links** are counted when the token can read the other repository. If it
  cannot, the result is reported as inconclusive rather than as “no linked issue”.
- Both queries are paginated, with a safety cap of 100 pages.

## Usage

```yaml
name: Verify linked issue
on:
  pull_request:

permissions:
  pull-requests: write   # see “Token and permissions” below

jobs:
  verify-linked-issue:
    name: verify-linked-issue
    runs-on: ubuntu-latest
    steps:
      - uses: Code-Of-Chaos/action-verify-linked-issue@v1
        with:
          token: ${{ secrets.GITHUB_TOKEN }}
```

No `actions/checkout` step is required: the action reads the pull request number from the
event payload and talks to the GitHub API directly.

### Inputs

| Input | Default | Description |
|-------|---------|-------------|
| `token` | `${{ github.token }}` | Token used to read the pull request and its linked issues and to create/update the failure comment. Must have `pull-requests: write` to be able to comment. The token is never logged. |
| `github-api-url` | `${{ github.api_url }}` | Base URL of the GitHub REST/GraphQL API. Set automatically; override it for GitHub Enterprise Server. |
| `comment-on-failure` | `'true'` | When `true`, comment on the pull request when no linked issue is found. The comment is updated in place on later runs instead of posted again (stable marker `<!-- verify-linked-issue -->`, only comments authored by the token's user are matched). |

### Outputs

| Output | Description |
|--------|-------------|
| `linked-issues` | Comma-separated list of linked issues (`owner/repo#number`); empty when none were found. |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | At least one linked issue exists — success. |
| `1` | No linked issue exists — the required-link comment was posted/updated (if enabled) and the check fails. |
| `2` | The check could not be performed: unsupported event, missing context, API error, rate limit, missing permission or inaccessible issue. The comment API is not touched. |

If posting or updating the comment fails (for example a read-only token), the action still
fails with exit code `1` and reports **both** the missing-link result and the comment error.

## Token and permissions

Minimum workflow permissions (verified against the current
[REST permission tables](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)):

- **Reading a pull request:** `pull-requests: read` (or `contents: read`).
- **Listing, creating and updating pull request comments:** `pull-requests: write`
  (or `issues: write`).

Since a write permission implies read, the practical minimum for the workflow is:

```yaml
permissions:
  pull-requests: write
```

If the action reports that linked issues cannot be read in a private repository, additionally
grant `issues: read`. The default `secrets.GITHUB_TOKEN` works; any token that can read the
repository and comment on the PR also works (for example a fine-grained PAT or a GitHub App
installation token).

## Supported events

The action supports **`pull_request`** and **`pull_request_target`** events. On any other event
it fails with exit code `2` and a clear message that a pull request context is required.

## Forked pull requests and `pull_request_target` safety

On `pull_request` workflows triggered by a **fork** pull request, GitHub gives
`secrets.GITHUB_TOKEN` **read-only** permissions. In that case:

- link detection still works (it only reads data), and
- **commenting fails with `403`** — the action still fails (so the check works), and it reports
  the comment error alongside the missing-link result.

If you want the explanatory comment on fork PRs too, run the action on `pull_request_target`.
**Warning:** `pull_request_target` runs with a writable token in the context of the *base*
repository. Never check out or execute the pull request's code in that workflow — a malicious
PR could otherwise steal your token. Keep the workflow limited to this trusted action and
pull request metadata/comment API operations:

```yaml
name: Verify linked issue
on:
  pull_request_target:

permissions:
  pull-requests: write   # read the PR + linked issues, post/update the comment

jobs:
  verify-linked-issue:
    name: verify-linked-issue
    runs-on: ubuntu-latest
    steps:
      # Trusted action only: no checkout, no code from the pull request.
      - uses: Code-Of-Chaos/action-verify-linked-issue@v1
        with:
          token: ${{ secrets.GITHUB_TOKEN }}
```

## Using the action as a required status check

1. Add the job with a stable name (for example `verify-linked-issue`, as in the examples above).
2. Make sure the workflow runs on `pull_request` for pull requests targeting your default
   branch (or on `pull_request_target` — see the safety note above).
3. In the repository settings, open **Branches → branch protection rule (or Ruleset) → Require
   status checks to pass before merging** and add `verify-linked-issue` to the required checks.
4. From then on, a pull request without a linked issue fails the check (red) and cannot be
   merged; the PR also receives the explanatory comment.

## Python runtime version

The action's logic is Python and runs on **Python 3.15.2** — the latest stable version
available from the [`actions/python-versions` manifest](https://raw.githubusercontent.com/actions/python-versions/main/versions-manifest.json)
(the authoritative source used by `actions/setup-python`) at implementation time. It is pinned
exactly (no `3.x` ranges, no prereleases) in two places, which a test keeps in sync:

- `action.yml` → `runs.steps[].with.python-version`
- `.github/workflows/ci.yml` → `with.python-version`

### Updating the pinned Python version

1. Open the `versions-manifest.json` link above and find the highest entry with
   `"stable": true` (or check the installed Python versions in
   [actions/runner-images](https://github.com/actions/runner-images)). Never pick an entry
   whose version contains `-rc`/`-beta` or whose `stable` flag is `false`.
2. Update `python-version` in **both** `action.yml` and `.github/workflows/ci.yml`.
3. Run the checks (see below) and open a pull request.

The action itself has **no third-party runtime dependencies** (Python standard library only);
`actions/setup-python` is pinned to a full commit SHA with a `# vX.Y.Z` comment.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # Windows: .venv\Scripts\pip
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
.venv/bin/mypy src tests
.venv/bin/pytest
```

All development dependencies are pinned exactly in `requirements-dev.txt`. The tests need no
network, credentials or real repository: the GitHub API is replaced by an in-memory fake.

CI (`.github/workflows/ci.yml`) runs, on every push and pull request to `core`:

- `ruff check` + `ruff format --check` (lint/format),
- `mypy --strict` (type checking),
- `pytest` (unit tests, including action metadata validation),
- `actionlint` 1.7.12, installed from its official release with a checksum verified against
  the published checksums file (workflow validation).

## Releases and version tags

Releases are produced by `.github/workflows/release.yml` using
[`code-of-chaos/action-version-bumper`](https://github.com/code-of-chaos/action-version-bumper)
(pinned to a full commit SHA). The current version lives in the `VERSION` file (semver).

The bumper **maintains the floating tags itself** — its `floating_major_version` and
`floating_minor_version` inputs (verified against its README and `action.yml`) — so each
release produces:

- an **exact tag** such as `v1.2.3`,
- a **floating minor tag** `v1.2` pointing at the latest `v1.2.x` release,
- a **floating major tag** `v1` pointing at the latest compatible `v1.x.x` release.

No other tags are touched.

### How maintainers trigger a release

1. Merge a green change into `core`.
2. Open the **Actions → Release** workflow, click **Run workflow**, and choose the bump type:

   | Bump | Use for | Example (from `1.2.3`) |
   |------|---------|------------------------|
   | `patch` | fixes, docs, internal changes | `1.2.4` |
   | `minor` | new backwards-compatible behavior | `1.3.0` |
   | `major` | breaking changes (inputs, behavior, exit codes) | `2.0.0` |

3. The workflow commits the new `VERSION`, creates the exact tag, and moves the `vX.Y` and `vX`
   floating tags.

Notes:

- **No workflow loop:** the release workflow is `workflow_dispatch`-only, so the commit and
  tags pushed by the bumper cannot re-trigger it (CI runs on push, but CI never releases).
- **Permissions:** the job has only `contents: write`. If branch protection rejects pushes
  made with `GITHUB_TOKEN`, run the workflow with a token that is allowed to push to the
  protected branch (for example a fine-grained PAT in `secrets.RELEASE_TOKEN`, passed to
  `actions/checkout`'s `token` input).
- **Reruns:** every run performs exactly one bump. Re-running a run that already pushed
  produces the *next* version — it never rewrites existing exact tags or unrelated branches;
  only the two floating tags of the released line are moved.
- **Bootstrap:** the repository starts with `VERSION = 1.0.0`. After the first release the
  `v1`, `v1.0` and exact tags exist; if you want `v1.0.0` tagged before the first automated
  release, create it once manually:

  ```bash
  git tag v1.0.0 <commit> && git push origin v1.0.0 v1.0 v1
  ```

### How consumers should reference the action

```yaml
- uses: Code-Of-Chaos/action-verify-linked-issue@v1      # floating major (latest v1.x.x)
- uses: Code-Of-Chaos/action-verify-linked-issue@v1.2    # floating minor (latest v1.2.x)
- uses: Code-Of-Chaos/action-verify-linked-issue@v1.2.3  # exact release
```

Use `@v1` for automatic compatible updates, `@v1.2` to stay within a minor line, or an exact
tag (or full commit SHA, for strict supply-chain pinning) for maximum reproducibility.

## License

[GPL-3.0](LICENSE)
