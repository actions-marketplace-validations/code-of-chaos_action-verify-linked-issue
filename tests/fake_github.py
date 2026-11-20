"""Transport-level fake of the GitHub REST and GraphQL APIs for the tests."""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping
from typing import Any

from action import HttpResponse

ISSUE_COMMENTS_PATH = re.compile(r"^/repos/[^/]+/[^/]+/issues/\d+/comments$")
COMMENT_PATH = re.compile(r"^/repos/[^/]+/[^/]+/issues/comments/\d+$")


def json_response(status: int, payload: Any) -> HttpResponse:
    return HttpResponse(status=status, headers={}, body=json.dumps(payload))


def issue_node(
    owner: str, repo: str, number: int, *, state: str = "OPEN", url: str | None = None
) -> dict[str, Any]:
    """A GraphQL ``Issue`` node as returned by ``closingIssuesReferences``."""
    return {
        "__typename": "Issue",
        "number": number,
        "state": state,
        "url": url or f"https://github.com/{owner}/{repo}/issues/{number}",
        "repository": {"name": repo, "owner": {"login": owner}},
    }


def connected_issue(owner: str, repo: str, number: int, *, state: str = "OPEN") -> dict[str, Any]:
    return {"__typename": "ConnectedEvent", "subject": issue_node(owner, repo, number, state=state)}


def disconnected_issue(owner: str, repo: str, number: int) -> dict[str, Any]:
    return {
        "__typename": "DisconnectedEvent",
        "subject": {
            "__typename": "Issue",
            "number": number,
            "repository": {"name": repo, "owner": {"login": owner}},
        },
    }


def connected_pull_request(number: int) -> dict[str, Any]:
    """A connection whose subject is a pull request, not an issue."""
    return {
        "__typename": "ConnectedEvent",
        "subject": {"__typename": "PullRequest", "number": number},
    }


def connected_unknown() -> dict[str, Any]:
    """A connection whose subject cannot be read (deleted or inaccessible issue)."""
    return {"__typename": "ConnectedEvent", "subject": None}


class FakeGitHub:
    """A ``Transport`` that serves canned GitHub API responses.

    * ``closing_pages``: pages of ``closingIssuesReferences`` nodes.
    * ``timeline_pages``: pages of ``ConnectedEvent``/``DisconnectedEvent`` nodes.
    * ``comments``: existing comments on the pull request.
    * ``graphql_error_pages`` / ``graphql_error_no_data``: inject GraphQL errors.
    * ``failures``: inject HTTP error responses or exceptions for a route.
    """

    def __init__(
        self,
        *,
        closing_pages: list[list[dict[str, Any]]] | None = None,
        timeline_pages: list[list[dict[str, Any]]] | None = None,
        comments: list[dict[str, Any]] | None = None,
        login: str = "github-actions[bot]",
    ) -> None:
        self.closing_pages: list[list[dict[str, Any]]] = closing_pages or [[]]
        self.timeline_pages: list[list[dict[str, Any]]] = timeline_pages or [[]]
        self.comments: list[dict[str, Any]] = comments or []
        self.login = login
        self.missing_pr = False
        self.graphql_error_pages: dict[str, list[dict[str, Any]]] = {}
        self.graphql_error_no_data: set[str] = set()
        self.failures: list[tuple[str, str, HttpResponse | BaseException]] = []
        self.rest_calls: list[tuple[str, str]] = []
        self.graphql_calls: list[dict[str, Any]] = []
        self.created_comments: list[str] = []
        self.updated_comments: list[tuple[int, str]] = []

    # -- test helpers -----------------------------------------------------
    def fail(
        self,
        method: str,
        url_substring: str,
        *,
        status: int = 500,
        message: str = "boom",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Respond with an HTTP error for matching requests."""
        body = json.dumps({"message": message})
        self.failures.append(
            (method, url_substring, HttpResponse(status, dict(headers or {}), body))
        )

    def raise_on(self, method: str, url_substring: str, exc: BaseException) -> None:
        """Raise an exception (e.g. a transport error) for matching requests."""
        self.failures.append((method, url_substring, exc))

    # -- transport interface ----------------------------------------------
    def request(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> HttpResponse:
        for failure_method, substring, action in self.failures:
            if failure_method == method and substring in url:
                if isinstance(action, BaseException):
                    raise action
                return action
        if url.endswith("/graphql"):
            return self._handle_graphql(body)
        return self._handle_rest(method, url, body)

    # -- internals ---------------------------------------------------------
    def _handle_graphql(self, body: bytes | None) -> HttpResponse:
        assert body is not None
        payload = json.loads(body.decode("utf-8"))
        query = payload.get("query", "")
        variables = payload.get("variables") or {}
        self.graphql_calls.append({"query": query, "variables": variables})

        if "closingIssuesReferences" in query:
            kind = "closing"
            pages = self.closing_pages
        elif "timelineItems" in query:
            kind = "timeline"
            pages = self.timeline_pages
        else:
            raise AssertionError(f"unexpected GraphQL query: {query}")

        cursor = variables.get("cursor")
        index = 0 if cursor is None else int(cursor)
        nodes = pages[index] if index < len(pages) else []
        has_next = index + 1 < len(pages)
        page_info = {"hasNextPage": has_next, "endCursor": str(index + 1) if has_next else None}

        if self.missing_pr:
            data: dict[str, Any] = {"data": {"repository": {"pullRequest": None}}}
        elif kind == "closing":
            data = {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "closingIssuesReferences": {"pageInfo": page_info, "nodes": nodes}
                        }
                    }
                }
            }
        else:
            data = {
                "data": {
                    "repository": {
                        "pullRequest": {"timelineItems": {"pageInfo": page_info, "nodes": nodes}}
                    }
                }
            }

        errors = self.graphql_error_pages.get(kind)
        if errors:
            if kind in self.graphql_error_no_data:
                return json_response(200, {"errors": errors})
            data["errors"] = errors
        return json_response(200, data)

    def _handle_rest(self, method: str, url: str, body: bytes | None) -> HttpResponse:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        self.rest_calls.append((method, path))

        if method == "GET" and path == "/user":
            return json_response(200, {"login": self.login})

        if method in ("GET", "POST") and ISSUE_COMMENTS_PATH.match(path):
            if method == "GET":
                page = int(query.get("page", ["1"])[0])
                per_page = int(query.get("per_page", ["30"])[0])
                start = (page - 1) * per_page
                return json_response(200, self.comments[start : start + per_page])
            assert body is not None
            payload = json.loads(body.decode("utf-8"))
            self.created_comments.append(payload["body"])
            return json_response(201, {"id": 1001, "body": payload["body"]})

        if method == "PATCH" and COMMENT_PATH.match(path):
            assert body is not None
            payload = json.loads(body.decode("utf-8"))
            comment_id = int(path.rsplit("/", 1)[-1])
            self.updated_comments.append((comment_id, payload["body"]))
            return json_response(200, {"id": comment_id, "body": payload["body"]})

        raise AssertionError(f"unexpected REST call: {method} {url}")
