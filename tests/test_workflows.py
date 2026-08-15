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
    "capture.yml",
    "publish-bootstrap.yml",
    "promote.yml",
    "validate.yml",
}
FULL_SHA = re.compile(r"[^@\s]+@[0-9a-f]{40}\s*(?:#.*)?$")


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load YAML without adding a runtime dependency to the registry package.

    CI runners provide Ruby's standard YAML parser, while a developer who has
    PyYAML installed gets a direct Python parse.  Both branches return JSON
    compatible mappings so the policy assertions inspect parsed documents.
    """

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
    writer_workflows = {"capture.yml", "publish-bootstrap.yml", "promote.yml"}
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


def test_workflows_use_locked_dependencies_and_forbid_unsafe_dispatches(
    workflows: dict[str, dict[str, Any]],
) -> None:
    all_text = "\n".join(
        path.read_text() for path in sorted(WORKFLOW_ROOT.glob("*.y*ml"))
    )
    assert all(
        workflow_text.count("uv sync --locked") >= 1
        for workflow_text in (
            path.read_text() for path in sorted(WORKFLOW_ROOT.glob("*.y*ml"))
        )
    )
    assert "pull_request_target" not in all_text
    assert "force" not in all_text.lower()

    validate = workflows["validate.yml"]
    jobs = validate["jobs"]
    local = jobs["local"]
    assert isinstance(local, dict)
    local_text = json.dumps(local)
    assert "packages.geldata.com" not in local_text
    assert "verify-capture-live" not in local_text
    assert "validate-capture" in local_text
    assert "normalize --repo . --check" in local_text


def test_pr_producers_have_only_job_scoped_write_permissions(
    workflows: dict[str, dict[str, Any]],
) -> None:
    for name in ("capture.yml", "publish-bootstrap.yml", "promote.yml"):
        workflow = workflows[name]
        top_level = json.dumps(workflow["permissions"])
        assert '"write"' not in top_level
        assert "pull_request_target" not in json.dumps(workflow)


def test_triggers_match_their_operational_scope(
    workflows: dict[str, dict[str, Any]],
) -> None:
    assert set(workflows["capture.yml"]["on"]) == {"workflow_dispatch"}
    assert set(workflows["publish-bootstrap.yml"]["on"]) == {"workflow_dispatch"}
    assert set(workflows["promote.yml"]["on"]) == {
        "schedule",
        "workflow_dispatch",
    }
    assert "pull_request" in workflows["validate.yml"]["on"]


def test_validation_scopes_live_rehearsal_to_capture_and_publication_changes(
    workflows: dict[str, dict[str, Any]],
) -> None:
    scope = workflows["validate.yml"]["jobs"]["scope"]
    assert isinstance(scope, dict)
    outputs = scope.get("outputs")
    assert isinstance(outputs, dict)
    assert "capture" in outputs
    scope_text = "\n".join(
        str(step.get("run", "")) for step in scope["steps"] if isinstance(step, dict)
    )
    assert "capture_paths" in scope_text
    assert "capture_only" in scope_text
    assert '"$HEAD_REF" == "capture/legacy-2026-08-bootstrap"' in scope_text
    assert '"$HEAD_REF" == "publish/legacy-2026-08-bootstrap"' in scope_text
    assert '"$release" == false' in scope_text
    rehearsal = workflows["validate.yml"]["jobs"]["capture-rehearsal"]
    assert isinstance(rehearsal, dict)
    assert "needs.scope.outputs.rehearsal" in str(rehearsal.get("if"))


def test_publication_and_promotion_reruns_fail_closed_or_are_idempotent(
    workflows: dict[str, dict[str, Any]],
) -> None:
    publication_text = json.dumps(workflows["publish-bootstrap.yml"])
    assert "PUBLICATION_PENDING" in publication_text
    assert "already present" in publication_text
    publication_scripts = "\n".join(
        str(step.get("run", ""))
        for step in workflows["publish-bootstrap.yml"]["jobs"]["publish"]["steps"]
        if isinstance(step, dict)
    )
    assert (
        'git diff --quiet "refs/remotes/origin/$DEFAULT_BRANCH" HEAD -- pointers public'
        in publication_scripts
    )
    assert "git rev-list --count" not in publication_text
    promotion_text = json.dumps(workflows["promote.yml"])
    assert "if ! discovered_tags=" in promotion_text
    assert "could not discover stable Gel CLI releases" in promotion_text
    assert "mapfile -t tags < <(" not in promotion_text
