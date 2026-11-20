"""Shared test fixtures.

The tests never touch the network, a real repository, or credentials: all
GitHub API traffic is served by the :class:`FakeGitHub` transport double.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """A minimal `pull_request` event environment for one pull request."""
    payload = tmp_path / "event.json"
    payload.write_text(json.dumps({"pull_request": {"number": 42}}), encoding="utf-8")
    return {
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_EVENT_PATH": str(payload),
        "GITHUB_REPOSITORY": "octo/example",
        "GITHUB_REF": "refs/pull/42/merge",
        "INPUT_TOKEN": "test-token-secret-value",
        "INPUT_GITHUB_API_URL": "https://api.github.com",
        "INPUT_COMMENT_ON_FAILURE": "true",
    }
