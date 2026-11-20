"""Validation of action metadata, workflow pinning, and release configuration.

These tests keep the repository's packaging honest: the action metadata must
match the implemented behaviour, third-party actions must be pinned to full
commit SHAs with a version comment, and the release workflow must maintain the
exact + floating minor + major tags through ``action-version-bumper``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ACTION_YML = REPO_ROOT / "action.yml"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
EXACT_PYTHON_RE = re.compile(r"^\d+\.\d+\.\d+$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
PINNED_REQUIREMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+==[A-Za-z0-9_.+-]+$")


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for job in (workflow.get("jobs") or {}).values():
        steps.extend(job.get("steps") or [])
    return steps


def _uses(step: dict[str, Any]) -> str | None:
    uses = step.get("uses")
    return uses if isinstance(uses, str) else None


# -- action.yml -------------------------------------------------------------


def test_action_metadata_basics() -> None:
    action = _load(ACTION_YML)
    assert action["name"]
    assert action["description"]
    # GitHub rejects action descriptions of 125 characters or more when the
    # action is published (e.g. to the Marketplace).
    assert len(action["description"]) < 125, (
        f"description must be < 125 characters, got {len(action['description'])}"
    )
    assert action["author"]
    assert action["branding"]["icon"]
    assert action["runs"]["using"] == "composite"


def test_action_inputs_and_defaults() -> None:
    inputs = _load(ACTION_YML)["inputs"]
    assert inputs["token"]["default"] == "${{ github.token }}"
    assert inputs["token"]["required"] is False
    assert inputs["github-api-url"]["default"] == "${{ github.api_url }}"
    assert inputs["comment-on-failure"]["default"] == "true"
    for spec in inputs.values():
        assert spec["description"], "every input must be documented"


def test_action_output_is_wired() -> None:
    outputs = _load(ACTION_YML)["outputs"]
    assert outputs["linked-issues"]["value"] == "${{ steps.verify.outputs.linked-issues }}"
    assert outputs["linked-issues"]["description"]


def test_setup_python_is_sha_pinned_with_version_comment() -> None:
    text = ACTION_YML.read_text(encoding="utf-8")
    line = next(line for line in text.splitlines() if "uses: actions/setup-python@" in line)
    pin = line.split("uses: actions/setup-python@", 1)[1].split()[0]
    assert SHA_RE.fullmatch(pin), f"setup-python must be pinned to a full SHA, got {pin!r}"
    assert re.search(r"#\s*v\d+", line), "the pin must carry a `# vX.Y.Z` version comment"


def test_python_version_is_exactly_pinned() -> None:
    action = _load(ACTION_YML)
    setup_step = next(
        step
        for step in action["runs"]["steps"]
        if _uses(step) == f"actions/setup-python@{_setup_python_pin()}"
    )
    version = setup_step["with"]["python-version"]
    assert EXACT_PYTHON_RE.fullmatch(version), (
        f"python-version must be an exact X.Y.Z pin (no ranges, no '3.x'), got {version!r}"
    )


def _setup_python_pin() -> str:
    line = next(
        line
        for line in ACTION_YML.read_text(encoding="utf-8").splitlines()
        if "uses: actions/setup-python@" in line
    )
    return line.split("uses: actions/setup-python@", 1)[1].split()[0]


def test_composite_runs_the_python_script() -> None:
    action = _load(ACTION_YML)
    steps = action["runs"]["steps"]
    verify_step = next(step for step in steps if step.get("id") == "verify")
    assert "src/action.py" in verify_step["run"]
    assert (REPO_ROOT / "src" / "action.py").is_file()
    assert verify_step["shell"] == "bash"
    assert verify_step["env"]["INPUT_TOKEN"] == "${{ inputs.token }}"
    assert verify_step["env"]["INPUT_GITHUB_API_URL"] == "${{ inputs.github-api-url }}"
    assert verify_step["env"]["INPUT_COMMENT_ON_FAILURE"] == "${{ inputs.comment-on-failure }}"
    for step in steps:
        if "run" in step:
            assert step.get("shell"), "composite run steps must declare a shell"


# -- workflows --------------------------------------------------------------


def test_all_workflow_actions_are_sha_pinned() -> None:
    for workflow_path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        workflow = _load(workflow_path)
        for step in _steps(workflow):
            uses = _uses(step)
            if uses is None or uses.startswith("./"):
                continue
            assert "@" in uses, f"{workflow_path.name}: {uses!r} must pin a ref"
            repo, _, ref = uses.rpartition("@")
            assert SHA_RE.fullmatch(ref), (
                f"{workflow_path.name}: {repo}@{ref} must be pinned to a full 40-character "
                "commit SHA"
            )
            text = workflow_path.read_text(encoding="utf-8")
            line = next(line for line in text.splitlines() if uses in line)
            assert re.search(r"#\s*v\d+", line), (
                f"{workflow_path.name}: {uses} must carry a `# vX.Y.Z` version comment"
            )


def test_python_version_is_consistent_everywhere() -> None:
    action_version = next(
        step["with"]["python-version"]
        for step in _load(ACTION_YML)["runs"]["steps"]
        if str(_uses(step) or "").startswith("actions/setup-python@")
    )
    ci = _load(WORKFLOWS_DIR / "ci.yml")
    ci_version = next(
        step["with"]["python-version"]
        for step in _steps(ci)
        if str(_uses(step) or "").startswith("actions/setup-python@")
    )
    assert action_version == ci_version
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert action_version in readme, "README must document the pinned Python version"


# -- release workflow -------------------------------------------------------


def test_release_workflow_uses_version_bumper_with_floating_tags() -> None:
    release = _load(WORKFLOWS_DIR / "release.yml")
    steps = _steps(release)
    bumper = next(
        step
        for step in steps
        if str(_uses(step) or "").startswith("code-of-chaos/action-version-bumper@")
    )
    inputs = bumper["with"]
    assert inputs["version_file"] == "VERSION"
    assert inputs["commit"] == "true"
    assert inputs["tag"] == "true"
    assert inputs["push"] == "true"
    # The bumper itself maintains the floating major and minor tags.
    assert inputs["floating_major_version"] == "true"
    assert inputs["floating_minor_version"] == "true"
    assert inputs["bump"] == "${{ inputs.bump }}"


def test_release_workflow_is_dispatch_only_with_minimal_permissions() -> None:
    release = _load(WORKFLOWS_DIR / "release.yml")
    trigger = release.get("on", release.get(True))
    assert set(trigger) == {"workflow_dispatch"}, (
        "the release workflow must be workflow_dispatch-only so version-bump pushes "
        "cannot re-trigger it (no workflow loop)"
    )
    assert trigger["workflow_dispatch"]["inputs"]["bump"]["type"] == "choice"
    assert release["permissions"] == {"contents": "write"}


def test_release_workflow_checks_out_full_history() -> None:
    release = _load(WORKFLOWS_DIR / "release.yml")
    checkout = next(
        step for step in _steps(release) if str(_uses(step) or "").startswith("actions/checkout@")
    )
    assert checkout["with"]["fetch-depth"] == 0


# -- repository files -------------------------------------------------------


def test_version_file_is_semver() -> None:
    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert VERSION_RE.fullmatch(version)


def test_dev_dependencies_are_exactly_pinned() -> None:
    lines = [
        line.strip()
        for line in (REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines, "requirements-dev.txt must not be empty"
    for line in lines:
        assert PINNED_REQUIREMENT_RE.fullmatch(line), f"dependency must be pinned: {line!r}"


def test_ci_runs_tests_lint_and_workflow_validation() -> None:
    ci_text = (WORKFLOWS_DIR / "ci.yml").read_text(encoding="utf-8")
    assert "pytest" in ci_text
    assert "ruff check" in ci_text
    assert "mypy" in ci_text
    assert "actionlint" in ci_text


def test_readme_documents_required_concepts() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8").lower()
    for concept in [
        "pull_request_target",
        "required status check",
        "closing keyword",
        "development",
        "floating",
        "action-version-bumper",
        "fork",
    ]:
        assert concept in readme, f"README must document {concept!r}"
