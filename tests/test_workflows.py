"""Policy checks for the repository's GitHub Actions workflows."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

WORKFLOW_ROOT = Path(__file__).parents[1] / ".github" / "workflows"
WORKFLOW_NAMES = {
    "publish-bootstrap.yml",
    "promote.yml",
    "validate.yml",
}
FULL_SHA = re.compile(r"[^@\s]+@[0-9a-f]{40}\s*(?:#.*)?$")


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ModuleNotFoundError:
        result = subprocess.run(
            [
                "ruby",
                "-ryaml",
                "-rjson",
                "-e",
                "puts JSON.generate(YAML.load_file(ARGV.fetch(0)))",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        loaded = json.loads(result.stdout)
    else:
        loaded = yaml.safe_load(path.read_text())
    if not isinstance(loaded, dict):
        raise TypeError(f"{path} does not contain a YAML mapping")
    return loaded


def _walk(value: object) -> list[tuple[str, object]]:
    pairs: list[tuple[str, object]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            pairs.append((str(key), child))
            pairs.extend(_walk(child))
    elif isinstance(value, list):
        for child in value:
            pairs.extend(_walk(child))
    return pairs


@pytest.fixture(scope="module")
def workflows() -> dict[str, dict[str, Any]]:
    paths = sorted(WORKFLOW_ROOT.glob("*.y*ml"))
    assert {path.name for path in paths} == WORKFLOW_NAMES
    return {path.name: _load_yaml(path) for path in paths}


def test_every_third_party_action_is_pinned_to_a_full_sha(
    workflows: dict[str, dict[str, Any]],
) -> None:
    for name, workflow in workflows.items():
        for key, value in _walk(workflow):
            if key != "uses":
                continue
            assert isinstance(value, str), f"{name}: uses must be a string"
            assert not value.startswith("./"), f"{name}: local actions are not allowed"
            assert FULL_SHA.fullmatch(value), f"{name}: unpinned action {value!r}"


def test_workflows_have_explicit_least_privilege_permissions(
    workflows: dict[str, dict[str, Any]],
) -> None:
    writer_workflows = {"publish-bootstrap.yml", "promote.yml"}
    for name, workflow in workflows.items():
        permissions = workflow.get("permissions")
        assert isinstance(permissions, dict), f"{name}: missing workflow permissions"
        assert permissions.get("contents") == "read", (
            f"{name}: workflow contents permission must be read-only"
        )
        jobs = workflow.get("jobs")
        assert isinstance(jobs, dict) and jobs, f"{name}: missing jobs"
        for job_name, job in jobs.items():
            assert isinstance(job, dict), f"{name}/{job_name}: malformed job"
            job_permissions = job.get("permissions")
            assert isinstance(job_permissions, dict), (
                f"{name}/{job_name}: missing job permissions"
            )
            if name in writer_workflows:
                assert job_permissions.get("contents") == "write"
                assert job_permissions.get("pull-requests") == "write"
            else:
                assert job_permissions.get("contents") == "read"
                assert all(value != "write" for value in job_permissions.values()), (
                    f"{name}/{job_name}: unexpected write permission"
                )


def test_triggers_match_their_operational_scope(
    workflows: dict[str, dict[str, Any]],
) -> None:
    assert set(workflows["publish-bootstrap.yml"]["on"]) == {"workflow_dispatch"}
    assert set(workflows["promote.yml"]["on"]) == {
        "schedule",
        "workflow_dispatch",
    }
    assert "pull_request" in workflows["validate.yml"]["on"]


def test_validation_scopes_remote_checks_to_release_changes(
    workflows: dict[str, dict[str, Any]],
) -> None:
    scope = workflows["validate.yml"]["jobs"]["scope"]
    assert isinstance(scope, dict)
    outputs = scope.get("outputs")
    assert isinstance(outputs, dict)
    assert set(outputs) == {"registry_data", "release"}
    assert "capture-rehearsal" not in workflows["validate.yml"]["jobs"]


def test_retained_writer_branches_reject_unrelated_committed_paths(
    workflows: dict[str, dict[str, Any]],
) -> None:
    expected = {
        "publish-bootstrap.yml": r"!~ /^(pointers|public)\//",
        "promote.yml": r"!~ /^(releases\/gel-cli|pointers|public)\//",
    }
    for name, pattern in expected.items():
        workflow = workflows[name]
        text = "\n".join(
            str(step.get("run", ""))
            for job in workflow["jobs"].values()
            if isinstance(job, dict)
            for step in job.get("steps", [])
            if isinstance(step, dict)
        )
        assert 'git diff --name-only "refs/remotes/origin/$DEFAULT_BRANCH"' in text
        assert "unexpected_branch_paths" in text
        assert pattern in text
