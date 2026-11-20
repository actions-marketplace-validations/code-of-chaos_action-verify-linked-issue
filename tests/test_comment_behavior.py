"""Failure comment behaviour: posting, idempotent updates, and error reporting."""

from __future__ import annotations

from pathlib import Path

import pytest

import action
from action import COMMENT_MARKER
from fake_github import FakeGitHub, issue_node


def test_unlinked_posts_required_comment_and_fails(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub()

    result = action.main(env=env, transport=fake)

    captured = capsys.readouterr()
    assert result == action.EXIT_UNLINKED
    assert len(fake.created_comments) == 1
    comment = fake.created_comments[0]
    assert COMMENT_MARKER in comment
    assert "not linked" in comment.lower()
    assert "Development" in comment
    assert fake.updated_comments == []
    assert "no linked GitHub issue" in captured.out


def test_second_run_updates_existing_comment_instead_of_duplicating(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(
        comments=[
            {
                "id": 77,
                "body": f"old text\n{COMMENT_MARKER}",
                "user": {"login": "github-actions[bot]"},
            }
        ]
    )

    result = action.main(env=env, transport=fake)

    captured = capsys.readouterr()
    assert result == action.EXIT_UNLINKED
    assert fake.created_comments == []
    assert len(fake.updated_comments) == 1
    comment_id, body = fake.updated_comments[0]
    assert comment_id == 77
    assert COMMENT_MARKER in body
    assert "updated the" in captured.out


def test_comment_by_different_user_is_not_reused(env: dict[str, str]) -> None:
    # Someone else's comment carrying our marker must not be overwritten:
    # the action posts its own comment instead.
    fake = FakeGitHub(
        comments=[
            {
                "id": 88,
                "body": f"not ours\n{COMMENT_MARKER}",
                "user": {"login": "someone-else"},
            }
        ]
    )

    result = action.main(env=env, transport=fake)

    assert result == action.EXIT_UNLINKED
    assert fake.updated_comments == []
    assert len(fake.created_comments) == 1


def test_comment_failure_still_fails_and_reports_both_results(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub()
    fake.fail(
        "POST",
        "/issues/42/comments",
        status=403,
        message="Resource not accessible by personal access token",
    )

    result = action.main(env=env, transport=fake)

    output = capsys.readouterr()
    combined = output.out + output.err
    # The missing-link result is reported...
    assert result == action.EXIT_UNLINKED
    assert "no linked GitHub issue" in output.out
    # ...and so is the comment/API error.
    assert "403" in combined
    assert "also failed" in combined
    assert fake.created_comments == []


def test_comment_update_failure_still_fails_and_reports_both_results(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(
        comments=[
            {
                "id": 77,
                "body": f"old\n{COMMENT_MARKER}",
                "user": {"login": "github-actions[bot]"},
            }
        ]
    )
    fake.fail("PATCH", "/issues/comments/77", status=500, message="Server Error")

    result = action.main(env=env, transport=fake)

    output = capsys.readouterr()
    combined = output.out + output.err
    assert result == action.EXIT_UNLINKED
    assert "no linked GitHub issue" in output.out
    assert "500" in combined
    assert "also failed" in combined


def test_comment_can_be_disabled(env: dict[str, str]) -> None:
    env = dict(env)
    env["INPUT_COMMENT_ON_FAILURE"] = "false"
    fake = FakeGitHub()

    result = action.main(env=env, transport=fake)

    assert result == action.EXIT_UNLINKED
    # No comment-related REST calls at all (reads are GraphQL-only).
    assert fake.rest_calls == []
    assert fake.created_comments == [] and fake.updated_comments == []


def test_successful_check_never_comments(env: dict[str, str]) -> None:
    fake = FakeGitHub(closing_pages=[[issue_node("octo", "example", 1)]])

    result = action.main(env=env, transport=fake)

    assert result == action.EXIT_OK
    assert fake.rest_calls == []
    assert fake.created_comments == [] and fake.updated_comments == []


def test_linked_issues_written_to_github_output(env: dict[str, str], tmp_path: Path) -> None:
    output_file = tmp_path / "github_output"
    output_file.write_text("", encoding="utf-8")
    env = dict(env)
    env["GITHUB_OUTPUT"] = str(output_file)
    fake = FakeGitHub(
        closing_pages=[[issue_node("octo", "example", 1), issue_node("other", "repo", 2)]]
    )

    result = action.main(env=env, transport=fake)

    assert result == action.EXIT_OK
    assert output_file.read_text(encoding="utf-8") == "linked-issues=octo/example#1,other/repo#2\n"
