#!/usr/bin/env python3
"""Verify that the pull request in the current GitHub event is linked to an issue.

A pull request counts as linked when GitHub's structured API data shows at least
one of the following relationships to an existing issue:

* a closing-keyword reference, read from ``PullRequest.closingIssuesReferences``
  (for example ``Closes #123``), and
* a manual connection made through the pull request's "Development" section,
  derived from the ``ConnectedEvent``/``DisconnectedEvent`` timeline items of
  the pull request.

Plain-text mentions, titles and comments are intentionally not accepted: only
relationships that GitHub itself records are counted.

Exit codes:
    0: at least one linked issue exists.
    1: no linked issue exists (the required-link comment is posted or updated).
    2: the check could not be performed (unsupported event, missing context,
       API error, rate limit, missing permission, ...).

The GitHub token is read from the environment and is never logged.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

API_VERSION = "2026-03-10"
COMMENT_MARKER = "<!-- verify-linked-issue -->"
SUPPORTED_EVENTS = frozenset({"pull_request", "pull_request_target"})
PAGE_SIZE = 100
MAX_PAGES = 100

EXIT_OK = 0
EXIT_UNLINKED = 1
EXIT_ERROR = 2

COMMENT_BODY = """\
## Linked issue required

This pull request is **not linked to any existing GitHub issue**, so it cannot be accepted.

To fix this, do one of the following:

1. Add a closing keyword to the pull request description, for example `Closes #123`
   (`Fixes` and `Resolves` work too, as do cross-repository references such as
   `owner/repo#123`).
2. Link an issue manually: open this pull request's **Development** section and choose
   **Link an issue**, so GitHub records the connection.

Open or closed issues both count, as long as the issue still exists.

This comment is managed by the *Verify Linked Issue* action: subsequent runs update it
instead of posting a new one.

<!-- verify-linked-issue -->\
"""

CLOSING_QUERY = """
query VerifyLinkedIssueClosing($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      closingIssuesReferences(first: 100, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          number
          state
          url
          repository {
            name
            owner {
              login
            }
          }
        }
      }
    }
  }
}
"""

TIMELINE_QUERY = """
query VerifyLinkedIssueTimeline($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      timelineItems(itemTypes: [CONNECTED_EVENT, DISCONNECTED_EVENT], first: 100, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          __typename
          ... on ConnectedEvent {
            subject {
              __typename
              ... on Issue {
                number
                state
                url
                repository {
                  name
                  owner {
                    login
                  }
                }
              }
            }
          }
          ... on DisconnectedEvent {
            subject {
              __typename
              ... on Issue {
                number
                repository {
                  name
                  owner {
                    login
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


class ActionError(Exception):
    """Fatal error that prevents the check from completing (exit code 2)."""


class EventError(ActionError):
    """The event context is unusable for this check."""


class ApiError(ActionError):
    """A GitHub API call failed or returned an inconclusive result."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: str


class Transport(Protocol):
    """Minimal HTTP transport used by :class:`GitHubClient`."""

    def request(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> HttpResponse: ...


class UrllibTransport:
    """Standard-library HTTP transport. Never logs request headers or bodies."""

    def request(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return HttpResponse(
                    status=response.status,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=response.read().decode("utf-8", "replace"),
                )
        except urllib.error.HTTPError as exc:
            header_items = exc.headers.items() if exc.headers is not None else []
            return HttpResponse(
                status=exc.code,
                headers={str(key).lower(): str(value) for key, value in header_items},
                body=exc.read().decode("utf-8", "replace"),
            )
        except urllib.error.URLError as exc:
            raise ApiError(f"network error while contacting {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ApiError(f"timed out while contacting {url}") from exc


class GitHubClient:
    """Thin GitHub REST + GraphQL client built on a swappable transport."""

    def __init__(self, api_url: str, token: str, transport: Transport | None = None) -> None:
        self.api_url = api_url.rstrip("/")
        self._token = token
        self._transport: Transport = transport or UrllibTransport()

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
        }

    def rest(self, method: str, path: str, body: Any | None = None) -> Any:
        """Perform a REST request and return the parsed JSON body."""
        headers = self._headers()
        headers["X-GitHub-Api-Version"] = API_VERSION
        payload: bytes | None = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        response = self._transport.request(method, f"{self.api_url}{path}", headers, payload)
        if response.status >= 400:
            raise self._http_error(method, path, response)
        if not response.body.strip():
            return None
        try:
            return json.loads(response.body)
        except json.JSONDecodeError as exc:
            raise ApiError(
                f"{method} {path} returned a non-JSON response (HTTP {response.status})"
            ) from exc

    def graphql(self, query: str, variables: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Perform a GraphQL request.

        Returns ``(data, errors)``. GraphQL can return partial data together with
        errors, so errors are reported to the caller instead of raising immediately;
        callers must treat a result with errors and no linked issues as inconclusive.
        """
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        payload = json.dumps({"query": query, "variables": dict(variables)}).encode("utf-8")
        response = self._transport.request("POST", f"{self.api_url}/graphql", headers, payload)
        if response.status >= 400:
            raise self._http_error("POST", "/graphql", response)
        try:
            body = json.loads(response.body)
        except json.JSONDecodeError as exc:
            raise ApiError("GitHub returned a non-JSON response from the GraphQL API") from exc
        if not isinstance(body, dict):
            raise ApiError("GitHub returned an unexpected GraphQL response")
        data = body.get("data")
        data_dict: dict[str, Any] = data if isinstance(data, dict) else {}
        errors: list[str] = []
        raw_errors = body.get("errors")
        if isinstance(raw_errors, list):
            for entry in raw_errors:
                if isinstance(entry, dict):
                    error_type = entry.get("type") or "ERROR"
                    error_message = entry.get("message") or ""
                    errors.append(f"{error_type}: {error_message}".rstrip(": "))
                else:
                    errors.append(str(entry))
        return data_dict, errors

    @staticmethod
    def _http_error(method: str, path: str, response: HttpResponse) -> ApiError:
        """Translate an HTTP error response into an actionable ApiError."""
        message = ""
        try:
            parsed = json.loads(response.body)
            if isinstance(parsed, dict) and isinstance(parsed.get("message"), str):
                message = parsed["message"]
        except json.JSONDecodeError:
            pass

        status = response.status
        detail = f"{method} {path} failed (HTTP {status})"
        if message:
            detail += f": {message}"

        if status == 401:
            detail += (
                ". The token was rejected; check that the `token` input is valid and not expired."
            )
        elif status == 403:
            remaining = response.headers.get("x-ratelimit-remaining")
            if remaining == "0" or "rate limit" in message.lower():
                detail += ". GitHub API rate limit exceeded"
                reset = response.headers.get("x-ratelimit-reset")
                if reset and reset.isdigit():
                    reset_at = datetime.fromtimestamp(int(reset), tz=UTC).isoformat()
                    detail += f" (resets at {reset_at})"
                detail += "; wait for it to reset and re-run the job."
            else:
                detail += (
                    ". The token lacks permission for this operation; grant the workflow "
                    "`pull-requests: write` (see the action README) and re-run."
                )
        elif status == 404:
            detail += (
                ". The resource was not found or the token cannot access it "
                "(cross-repository links require a token that can read the other repository)."
            )
        elif status == 429:
            detail += ". GitHub is rate limiting requests; wait and re-run the job."
        elif status >= 500:
            detail += ". GitHub reported a server error; this is temporary, re-run the job."
        return ApiError(detail, status=status)


@dataclass(frozen=True)
class IssueRef:
    """A linked issue, identified by repository and number."""

    owner: str
    repo: str
    number: int
    state: str | None = None
    url: str | None = None

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.owner.lower(), self.repo.lower(), self.number)


def _issue_from_node(node: Any) -> IssueRef | None:
    """Build an IssueRef from a GraphQL node, or None if it is not a usable issue."""
    if not isinstance(node, dict):
        return None
    number = node.get("number")
    repository = node.get("repository")
    if not isinstance(number, int) or not isinstance(repository, dict):
        return None
    owner_node = repository.get("owner")
    repo_name = repository.get("name")
    if not isinstance(owner_node, dict) or not isinstance(repo_name, str):
        return None
    login = owner_node.get("login")
    if not isinstance(login, str):
        return None
    state = node.get("state")
    url = node.get("url")
    return IssueRef(
        owner=login,
        repo=repo_name,
        number=number,
        state=state if isinstance(state, str) else None,
        url=url if isinstance(url, str) else None,
    )


def _pull_request_from(data: Any) -> Any | None:
    if not isinstance(data, dict):
        return None
    repository = data.get("repository")
    if not isinstance(repository, dict):
        return None
    return repository.get("pullRequest")


def _require_pull_request(
    data: Any, errors: Sequence[str], owner: str, name: str, number: int
) -> Any:
    pull_request = _pull_request_from(data)
    if pull_request is None:
        if errors:
            raise ApiError(_inconclusive_message(errors))
        raise ApiError(
            f"pull request {owner}/{name}#{number} was not found or the token cannot read it; "
            "cannot verify linked issues (this is not proof that no issue is linked)."
        )
    return pull_request


def _inconclusive_message(errors: Sequence[str]) -> str:
    message = (
        "GitHub API returned errors while checking for linked issues; the result is inconclusive "
        "(this is not proof that no issue is linked): " + "; ".join(errors)
    )
    upper = " ".join(errors).upper()
    if "RATE_LIMITED" in upper or "RATE LIMIT" in upper:
        message += (
            " Hint: the GitHub API rate limit was reached; wait for it to reset and re-run the job."
        )
    elif "FORBIDDEN" in upper or "ACCESS" in upper or "PERMISSION" in upper:
        message += (
            " Hint: the token may lack permission to read the pull request or its issues; "
            "grant the workflow `pull-requests: write` (and `issues: read` if needed) and re-run."
        )
    return message


def fetch_closing_references(
    client: GitHubClient, owner: str, name: str, number: int
) -> tuple[list[IssueRef], list[str]]:
    """Fetch closing-keyword references (and, per GitHub's schema, manual links too).

    ``PullRequest.closingIssuesReferences`` is an ``IssueConnection`` and must be
    paginated with cursors. Returns ``(issues, graphql_errors)``.
    """
    issues: dict[tuple[str, str, int], IssueRef] = {}
    errors: list[str] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        data, page_errors = client.graphql(
            CLOSING_QUERY,
            {"owner": owner, "name": name, "number": number, "cursor": cursor},
        )
        errors.extend(page_errors)
        pull_request = _require_pull_request(data, errors, owner, name, number)
        connection = pull_request.get("closingIssuesReferences") or {}
        for node in connection.get("nodes") or []:
            ref = _issue_from_node(node)
            if ref is not None:
                issues.setdefault(ref.key, ref)
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not isinstance(cursor, str) or not cursor:
            raise ApiError("GitHub returned a pagination cursor that could not be used")
    else:
        raise ApiError(f"exceeded {MAX_PAGES} pages of closing references; aborting")
    return list(issues.values()), errors


def fetch_timeline_connections(
    client: GitHubClient, owner: str, name: str, number: int
) -> tuple[dict[tuple[str, str, int], IssueRef], int, list[str]]:
    """Fetch manual Development-section connections from the PR timeline.

    A manual link shows up as a ``ConnectedEvent``; unlinking adds a
    ``DisconnectedEvent``. An issue counts as connected while it has more
    connect events than disconnect events. Returns
    ``(linked_issues, unreadable_subjects, graphql_errors)``.
    """
    counts: dict[tuple[str, str, int], tuple[IssueRef, int, int]] = {}
    unknown = 0
    errors: list[str] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        data, page_errors = client.graphql(
            TIMELINE_QUERY,
            {"owner": owner, "name": name, "number": number, "cursor": cursor},
        )
        errors.extend(page_errors)
        pull_request = _require_pull_request(data, errors, owner, name, number)
        connection = pull_request.get("timelineItems") or {}
        for node in connection.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            event_type = node.get("__typename")
            if event_type not in ("ConnectedEvent", "DisconnectedEvent"):
                continue
            subject = node.get("subject")
            if not isinstance(subject, dict) or subject.get("__typename") is None:
                # The subject issue was deleted or cannot be read by this token.
                unknown += 1
                continue
            if subject.get("__typename") != "Issue":
                # The subject is a pull request (or another non-issue resource).
                continue
            ref = _issue_from_node(subject)
            if ref is None:
                unknown += 1
                continue
            connects, disconnects = counts.get(ref.key, (ref, 0, 0))[1:]
            if event_type == "ConnectedEvent":
                connects += 1
            else:
                disconnects += 1
            counts[ref.key] = (ref, connects, disconnects)
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not isinstance(cursor, str) or not cursor:
            raise ApiError("GitHub returned a pagination cursor that could not be used")
    else:
        raise ApiError(f"exceeded {MAX_PAGES} pages of timeline events; aborting")
    linked = {
        key: ref for key, (ref, connects, disconnects) in counts.items() if connects > disconnects
    }
    return linked, unknown, errors


def collect_linked_issues(
    client: GitHubClient, owner: str, name: str, number: int
) -> list[IssueRef]:
    """Return the deduplicated set of linked issues, or raise if inconclusive.

    Raises :class:`ApiError` when the API could not answer reliably, so an API
    error is never mistaken for "no linked issue".
    """
    closing, closing_errors = fetch_closing_references(client, owner, name, number)
    timeline, unknown, timeline_errors = fetch_timeline_connections(client, owner, name, number)
    errors = [*closing_errors, *timeline_errors]

    merged: dict[tuple[str, str, int], IssueRef] = {}
    for ref in [*closing, *timeline.values()]:
        merged.setdefault(ref.key, ref)

    if merged:
        return sorted(merged.values(), key=lambda ref: ref.key)
    if errors:
        raise ApiError(_inconclusive_message(errors))
    if unknown:
        raise ApiError(
            f"found {unknown} connection event(s) whose issue could not be read (deleted issue or "
            "insufficient token permissions). The check is inconclusive; this is not proof that "
            "no issue is linked. Grant the token read access to the repository "
            "(`pull-requests: write`) and re-run."
        )
    return []


def get_authenticated_login(client: GitHubClient) -> str:
    user = client.rest("GET", "/user")
    login = user.get("login") if isinstance(user, dict) else None
    if not isinstance(login, str) or not login:
        raise ApiError(
            "could not determine the authenticated user (GET /user returned no login); "
            "cannot update the existing comment idempotently"
        )
    return login


def find_existing_comment(
    client: GitHubClient, owner: str, repo: str, number: int, login: str
) -> int | None:
    """Find this action's previous comment on the PR, if any.

    Only comments carrying the stable marker *and* authored by the authenticated
    user are considered, so other people's comments are never modified.
    """
    base_path = f"/repos/{owner}/{repo}/issues/{number}/comments"
    for page in range(1, MAX_PAGES + 1):
        comments = client.rest("GET", f"{base_path}?per_page={PAGE_SIZE}&page={page}")
        if comments is None:
            break
        if not isinstance(comments, list):
            raise ApiError("GitHub returned an unexpected response while listing PR comments")
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            body = comment.get("body")
            user = comment.get("user")
            comment_login = user.get("login") if isinstance(user, dict) else None
            comment_id = comment.get("id")
            if (
                isinstance(body, str)
                and COMMENT_MARKER in body
                and isinstance(comment_login, str)
                and comment_login.lower() == login.lower()
                and isinstance(comment_id, int)
            ):
                return comment_id
        if len(comments) < PAGE_SIZE:
            break
    else:
        raise ApiError(f"exceeded {MAX_PAGES} pages of PR comments; aborting")
    return None


def upsert_failure_comment(client: GitHubClient, owner: str, repo: str, number: int) -> str:
    """Create or update the required-link comment. Returns ``created`` or ``updated``."""
    login = get_authenticated_login(client)
    body = COMMENT_BODY
    existing_id = find_existing_comment(client, owner, repo, number, login)
    if existing_id is not None:
        client.rest("PATCH", f"/repos/{owner}/{repo}/issues/comments/{existing_id}", {"body": body})
        return "updated"
    client.rest("POST", f"/repos/{owner}/{repo}/issues/{number}/comments", {"body": body})
    return "created"


def _pull_request_number(env: Mapping[str, str]) -> int:
    event_path = env.get("GITHUB_EVENT_PATH", "")
    if event_path:
        if not os.path.isfile(event_path):
            raise EventError(
                f"the event payload at {event_path} does not exist; "
                "a pull request context is required"
            )
        try:
            with open(event_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise EventError(f"could not read the event payload at {event_path}: {exc}") from exc
        pull_request = payload.get("pull_request") if isinstance(payload, dict) else None
        number = pull_request.get("number") if isinstance(pull_request, dict) else None
        if isinstance(number, int):
            return number
    match = re.match(r"refs/pull/(\d+)/", env.get("GITHUB_REF", ""))
    if match:
        return int(match.group(1))
    raise EventError(
        "could not determine the pull request number from the event context; "
        "a pull request context is required (run this action on `pull_request` or "
        "`pull_request_target` events)"
    )


def _comment_enabled(env: Mapping[str, str]) -> bool:
    value = (env.get("INPUT_COMMENT_ON_FAILURE") or "true").strip().lower()
    return value not in ("false", "0", "no")


def _write_action_output(env: Mapping[str, str], name: str, value: str) -> None:
    output_file = env.get("GITHUB_OUTPUT")
    if not output_file:
        return
    try:
        with open(output_file, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    except OSError:
        # Never fail the check because the output file could not be written.
        pass


def run_check(env: Mapping[str, str], transport: Transport | None = None) -> int:
    """Run the verification and return the process exit code."""
    event_name = env.get("GITHUB_EVENT_NAME", "")
    if event_name not in SUPPORTED_EVENTS:
        raise EventError(
            f"a pull request context is required, but this action ran on the {event_name!r} event. "
            "Trigger the workflow on `pull_request` or `pull_request_target`."
        )

    repository = env.get("GITHUB_REPOSITORY", "")
    if "/" not in repository:
        raise EventError("GITHUB_REPOSITORY is not set; cannot determine the repository.")
    owner, name = repository.split("/", 1)

    number = _pull_request_number(env)

    token = (env.get("INPUT_TOKEN") or "").strip()
    if not token:
        raise EventError(
            "no token provided; set the `token` input (it defaults to the workflow's github.token)."
        )

    api_url = (
        env.get("INPUT_GITHUB_API_URL") or env.get("GITHUB_API_URL") or "https://api.github.com"
    )
    client = GitHubClient(api_url, token, transport)

    issues = collect_linked_issues(client, owner, name, number)
    _write_action_output(env, "linked-issues", ",".join(ref.slug for ref in issues))

    if issues:
        summary = ", ".join(f"{ref.slug}{f' ({ref.state})' if ref.state else ''}" for ref in issues)
        print(
            f"verify-linked-issue: pull request {owner}/{name}#{number} is linked to "
            f"{len(issues)} existing issue(s): {summary}"
        )
        return EXIT_OK

    print(
        f"verify-linked-issue: pull request {owner}/{name}#{number} has no linked GitHub issue; "
        "only pull requests linked to an issue are accepted."
    )
    if _comment_enabled(env):
        try:
            action = upsert_failure_comment(client, owner, name, number)
            print(
                f"verify-linked-issue: {action} the 'linked issue required' comment on "
                f"pull request #{number}."
            )
        except ActionError as exc:
            print(
                "verify-linked-issue: ERROR: the pull request has no linked issue, and posting or "
                f"updating the required comment also failed: {exc}",
                file=sys.stderr,
            )
    return EXIT_UNLINKED


def main(env: Mapping[str, str] | None = None, transport: Transport | None = None) -> int:
    """Entry point. Returns the process exit code."""
    if env is None:
        env = os.environ
    try:
        return run_check(env, transport)
    except ActionError as exc:
        print(f"verify-linked-issue: ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
