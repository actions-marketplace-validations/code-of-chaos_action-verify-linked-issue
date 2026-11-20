"""Link detection: closing keywords, manual Development links, pagination, edge cases."""

from __future__ import annotations

import pytest

import action
from fake_github import (
    FakeGitHub,
    connected_issue,
    connected_pull_request,
    connected_unknown,
    disconnected_issue,
    issue_node,
)


def test_closing_keyword_link_succeeds(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(closing_pages=[[issue_node("octo", "example", 123, state="CLOSED")]])
    result = action.main(env=env, transport=fake)

    assert result == action.EXIT_OK
    assert "octo/example#123" in capsys.readouterr().out
    assert fake.created_comments == [] and fake.updated_comments == []


def test_manual_development_link_succeeds(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(timeline_pages=[[connected_issue("octo", "example", 7)]])

    result = action.main(env=env, transport=fake)

    assert result == action.EXIT_OK
    assert "octo/example#7" in capsys.readouterr().out
    assert fake.created_comments == []


def test_same_issue_from_both_sources_counted_once(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(
        closing_pages=[[issue_node("octo", "example", 123)]],
        timeline_pages=[[connected_issue("octo", "example", 123)]],
    )

    result = action.main(env=env, transport=fake)

    assert result == action.EXIT_OK
    assert "1 existing issue(s)" in capsys.readouterr().out


def test_open_and_closed_issues_accepted(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(
        closing_pages=[
            [
                issue_node("octo", "example", 10, state="OPEN"),
                issue_node("octo", "example", 11, state="CLOSED"),
            ]
        ]
    )

    result = action.main(env=env, transport=fake)

    assert result == action.EXIT_OK
    output = capsys.readouterr().out
    assert "octo/example#10 (OPEN)" in output
    assert "octo/example#11 (CLOSED)" in output


def test_cross_repository_issue_counted(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(closing_pages=[[issue_node("other", "repo", 5, state="OPEN")]])

    result = action.main(env=env, transport=fake)

    assert result == action.EXIT_OK
    assert "other/repo#5" in capsys.readouterr().out


def test_pull_request_itself_not_counted(env: dict[str, str]) -> None:
    # A connection whose subject is a pull request is not an issue link.
    fake = FakeGitHub(timeline_pages=[[connected_pull_request(42)]])

    result = action.main(env=env, transport=fake)

    assert result == action.EXIT_UNLINKED


def test_disconnected_manual_link_not_counted(env: dict[str, str]) -> None:
    fake = FakeGitHub(
        timeline_pages=[
            [connected_issue("octo", "example", 9), disconnected_issue("octo", "example", 9)]
        ]
    )

    result = action.main(env=env, transport=fake)

    assert result == action.EXIT_UNLINKED
    assert len(fake.created_comments) == 1


def test_deleted_issue_connection_is_inconclusive_not_unlinked(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    # A connection event whose subject cannot be read must not be treated as
    # "no linked issue": the action fails with an inconclusive error instead
    # of posting the required-link comment.
    fake = FakeGitHub(timeline_pages=[[connected_unknown()]])

    result = action.main(env=env, transport=fake)

    captured = capsys.readouterr()
    assert result == action.EXIT_ERROR
    assert "inconclusive" in (captured.out + captured.err)
    assert fake.created_comments == [] and fake.updated_comments == []


def test_closing_references_pagination(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(
        closing_pages=[
            [issue_node("octo", "example", 1)],
            [issue_node("octo", "example", 2)],
            [issue_node("octo", "example", 3)],
        ]
    )

    result = action.main(env=env, transport=fake)

    assert result == action.EXIT_OK
    closing_calls = [
        call for call in fake.graphql_calls if "closingIssuesReferences" in call["query"]
    ]
    assert len(closing_calls) == 3
    assert closing_calls[1]["variables"]["cursor"] == "1"
    assert "3 existing issue(s)" in capsys.readouterr().out


def test_timeline_pagination(env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeGitHub(timeline_pages=[[], [connected_issue("octo", "example", 5)]])

    result = action.main(env=env, transport=fake)

    assert result == action.EXIT_OK
    timeline_calls = [call for call in fake.graphql_calls if "timelineItems" in call["query"]]
    assert len(timeline_calls) == 2
    assert "octo/example#5" in capsys.readouterr().out


def test_issue_linked_after_reconnect_still_counts(env: dict[str, str]) -> None:
    # connect -> disconnect -> connect: more connects than disconnects, linked.
    fake = FakeGitHub(
        timeline_pages=[
            [
                connected_issue("octo", "example", 3),
                disconnected_issue("octo", "example", 3),
                connected_issue("octo", "example", 3),
            ]
        ]
    )

    result = action.main(env=env, transport=fake)

    assert result == action.EXIT_OK
