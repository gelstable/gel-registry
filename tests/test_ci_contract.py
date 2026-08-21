"""Supply-chain and privilege rules the workflows must keep.

Every third-party action is pinned to a full commit SHA, because a tag is a
mutable pointer some other account controls. And validation is read-only: it
reads a pull request and reports, so no job it runs may hold a write
permission.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

WORKFLOW_ROOT = Path(__file__).parents[1] / ".github" / "workflows"
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
    assert isinstance(loaded, dict)
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
    return {
        path.name: _load_yaml(path) for path in sorted(WORKFLOW_ROOT.glob("*.y*ml"))
    }


def test_every_third_party_action_is_pinned_to_a_full_sha(
    workflows: dict[str, dict[str, Any]],
) -> None:
    assert workflows
    for name, workflow in workflows.items():
        for key, value in _walk(workflow):
            if key != "uses":
                continue
            assert isinstance(value, str), f"{name}: uses must be a string"
            assert not value.startswith("./"), f"{name}: local actions are not allowed"
            assert FULL_SHA.fullmatch(value), f"{name}: unpinned action {value!r}"


def test_no_validation_job_holds_a_write_permission(
    workflows: dict[str, dict[str, Any]],
) -> None:
    for name, workflow in workflows.items():
        permissions = workflow.get("permissions")
        assert isinstance(permissions, dict), f"{name}: missing workflow permissions"
        assert permissions.get("contents") == "read"
        jobs = workflow["jobs"]
        assert jobs, f"{name}: missing jobs"
        for job_name, job in jobs.items():
            job_permissions = job.get("permissions")
            assert isinstance(job_permissions, dict), (
                f"{name}/{job_name}: missing job permissions"
            )
            assert job_permissions.get("contents") == "read"
            assert all(value != "write" for value in job_permissions.values()), (
                f"{name}/{job_name}: unexpected write permission"
            )
