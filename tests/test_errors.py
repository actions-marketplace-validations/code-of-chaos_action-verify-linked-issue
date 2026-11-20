"""Error handling: unsupported events, API errors, rate limits, permissions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import action
from fake_github import FakeGitHub


def test_unsupported_event_fails_clearly(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    env = dict(env)
    env["GITHUB_EVENT_NAME"] = "push"

    result = action.main(env=env, transport=FakeGitHub())

    captured = capsys.readouterr()
    assert result == action.EXIT_ERROR
    assert "pull request context is required" in captured.err
    assert "pull_request" in captured.err


def test_missing_pull_request_context_fails(
    env: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = dict(env)
    payload = tmp_path / "event.json"
    payload.write_text(json.dumps({"workflow_run": {"id": 1}}), encoding="utf-8")
    env["GITHUB_EVENT_PATH"] = str(payload)
    env["GITHUB_REF"] = "refs/heads/core"

    result = action.main(env=env, transport=FakeGitHub())

    captured = capsys.readouterr()
    assert result == action.EXIT_ERROR
    assert "pull request context is required" in captured.err


def test_missing_token_fails(env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    env = dict(env)
    env["INPUT_TOKEN"] = "  "

    result = action.main(env=env, transport=FakeGitHub())

    captured = capsys.readouterr()
    assert result == action.EXIT_ERROR
    assert "token" in captured.err


def test_graphql_rate_limit_is_inconclusive_not_unlinked(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub()
    fake.graphql_error_pages["closing"] = [
        {"type": "RATE_LIMITED", "message": "API rate limit exceeded for user"}
    ]

    result = action.main(env=env, transport=fake)

    combined = capsys.readouterr()
    combined_text = combined.out + combined.err
    assert result == action.EXIT_ERROR
    assert "inconclusive" in combined_text
    assert "rate limit" in combined_text.lower()
    # An API error must never be reported as "unlinked": no comment is posted.
    assert fake.created_comments == []


def test_graphql_errors_without_data_are_inconclusive(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub()
    fake.graphql_error_pages["closing"] = [
        {"type": "FORBIDDEN", "message": "Resource not accessible"}
    ]
    fake.graphql_error_no_data.add("closing")

    result = action.main(env=env, transport=fake)

    combined = capsys.readouterr()
    combined_text = combined.out + combined.err
    assert result == action.EXIT_ERROR
    assert "inconclusive" in combined_text
    assert "permission" in combined_text.lower() or "pull-requests" in combined_text
    assert fake.created_comments == []


def test_http_403_on_graphql_reports_missing_permissions(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub()
    fake.fail(
        "POST",
        "/graphql",
        status=403,
        message="Resource not accessible by personal access token",
    )

    result = action.main(env=env, transport=fake)

    combined = capsys.readouterr()
    combined_text = combined.out + combined.err
    assert result == action.EXIT_ERROR
    assert "403" in combined_text
    assert "permission" in combined_text.lower()
    assert fake.created_comments == []


def test_http_404_when_pull_request_is_unreadable(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub()
    fake.fail("POST", "/graphql", status=404, message="Not Found")

    result = action.main(env=env, transport=fake)

    combined = capsys.readouterr()
    combined_text = combined.out + combined.err
    assert result == action.EXIT_ERROR
    assert "404" in combined_text
    assert "cannot access" in combined_text or "not found" in combined_text
    assert fake.created_comments == []


def test_missing_pull_request_on_server_fails_clearly(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub()
    fake.missing_pr = True

    result = action.main(env=env, transport=fake)

    combined = capsys.readouterr()
    combined_text = combined.out + combined.err
    assert result == action.EXIT_ERROR
    assert "was not found or the token cannot read it" in combined_text
    assert fake.created_comments == []


def test_network_error_is_inconclusive(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub()
    fake.raise_on(
        "POST",
        "/graphql",
        action.ApiError("network error while contacting https://api.github.com/graphql: refused"),
    )

    result = action.main(env=env, transport=fake)

    combined = capsys.readouterr()
    combined_text = combined.out + combined.err
    assert result == action.EXIT_ERROR
    assert "network error" in combined_text
    assert fake.created_comments == []


def test_server_error_on_comment_listing_reported_with_unlinked_result(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub()
    fake.fail("GET", "/issues/42/comments", status=503, message="Service Unavailable")

    result = action.main(env=env, transport=fake)

    combined = capsys.readouterr()
    combined_text = combined.out + combined.err
    assert result == action.EXIT_UNLINKED
    assert "no linked GitHub issue" in combined.out
    assert "503" in combined_text
    assert "also failed" in combined_text


def test_token_is_never_printed(env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    env = dict(env)
    env["INPUT_TOKEN"] = "super-secret-token-do-not-print"
    fake = FakeGitHub(
        closing_pages=[
            [{"number": 1, "repository": {"name": "example", "owner": {"login": "octo"}}}]
        ]
    )

    result = action.main(env=env, transport=fake)

    output = capsys.readouterr()
    assert result == action.EXIT_OK
    assert "super-secret-token-do-not-print" not in output.out
    assert "super-secret-token-do-not-print" not in output.err
