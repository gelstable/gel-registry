"""Production acceptance uses committed trust and exercises populated channels."""

import json
import os
import subprocess
import tempfile
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.fixture
def key_home() -> Iterator[Path]:
    # GPG-agent sockets must fit even when the platform's pytest paths are long.
    with tempfile.TemporaryDirectory(prefix="native-key-", dir="/tmp") as directory:
        home = Path(directory)
        yield home
        subprocess.run(
            ["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"], check=True
        )


@pytest.mark.parametrize("client", ["apt", "dnf"])
@pytest.mark.parametrize(
    "damage", [None, "wrong-pin", "extra-key", "missing-pin", "malformed-key"]
)
def test_production_acceptance_pins_one_primary(
    tmp_path: Path, key_home: Path, client: str, damage: str | None
) -> None:
    certificate = tmp_path / "certificate.asc"
    certificate.write_bytes((ROOT / "public/keys/gelstable.asc").read_bytes())
    fingerprint = (ROOT / "public/keys/gelstable.fingerprint").read_text().strip()
    env = {
        **os.environ,
        "GNUPGHOME": str(key_home),
        "CLIENT": client,
        "CERTIFICATE": str(certificate),
    }
    if damage == "extra-key":
        subprocess.run(
            [
                "gpg",
                "--batch",
                "--passphrase",
                "",
                "--quick-generate-key",
                "Untrusted <untrusted@example.invalid>",
                "ed25519",
                "sign",
                "0",
            ],
            env=env,
            check=True,
            capture_output=True,
        )
        certificate.write_bytes(
            certificate.read_bytes()
            + subprocess.check_output(
                ["gpg", "--armor", "--export"],
                env=env,
            )
        )
    if damage == "wrong-pin":
        fingerprint = "0" * 40
    if damage == "malformed-key":
        certificate.write_text("not a certificate")
    if damage != "missing-pin":
        env["GELSTABLE_EXPECTED_FPR"] = fingerprint
    else:
        env.pop("GELSTABLE_EXPECTED_FPR", None)
    result = subprocess.run(
        [
            "bash",
            "-c",
            """source "$1" stable
            apt-get() { :; }
            dnf() { :; }
            install() { :; }
            rpmkeys() { :; }
            command() {
              if test "$1 ${2:-}" = '-v apt-get'; then
                test "$CLIENT" = apt
              else
                builtin command "$@"
              fi
            }
            curl() {
              if [[ "${@: -1}" != */keys/gelstable.asc ]]; then
                echo repository-config-requested
                exit 0
              fi
            }
            gpg() {
              local args=("$@")
              args[${#args[@]}-1]=$CERTIFICATE
              builtin command gpg "${args[@]}"
            }
            configure""",
            "probe",
            str(ROOT / "tests/clients/smoke.sh"),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    if damage is None:
        assert result.returncode == 0, result.stderr
        assert result.stdout == "repository-config-requested\n"
    else:
        assert result.returncode != 0
        assert "repository-config-requested" not in result.stdout


def run_acceptance_step(
    tmp_path: Path,
    job: str,
    packages: list[dict[str, str]],
    machine: str,
    image: str,
    channel: str = "testing",
) -> list[str] | None:
    """Run the workflow's shell step with only the Docker boundary replaced."""
    workflow = (ROOT / ".github/workflows/native-smoke.yml").read_text()
    section = workflow.split(f"  {job}:\n", 1)[1].split("  report:\n", 1)[0]
    if job == "smoke":
        section = section.split("  migrate:\n", 1)[0]
    run = section.split("        run: ", 1)[1]
    script = textwrap.dedent(run[2:]) if run.startswith("|\n") else run.strip()
    (tmp_path / "native").mkdir()
    (tmp_path / "native/packages.lock.json").write_text(json.dumps(packages))
    (tmp_path / "public/keys").mkdir(parents=True)
    (tmp_path / "public/keys/gelstable.fingerprint").write_text("B" * 40 + "\n")
    bins = tmp_path / "bin"
    bins.mkdir()
    uname = bins / "uname"
    uname.write_text(f"#!/bin/sh\nprintf '%s\\n' {machine}\n")
    uname.chmod(0o755)
    docker = bins / "docker"
    docker.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$RUNNER_TEMP/docker-args"\n')
    docker.chmod(0o755)
    subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "PATH": str(bins) + os.pathsep + os.environ["PATH"],
            "RUNNER_TEMP": str(tmp_path),
            "IMAGE": image,
            "CHANNEL": channel,
        },
    )
    log = tmp_path / "docker-args"
    return log.read_text().splitlines() if log.exists() else None


@pytest.mark.parametrize(
    "format,arch,machine,image",
    [
        ("deb", "amd64", "x86_64", "debian:12"),
        ("deb", "arm64", "aarch64", "debian:12"),
        ("rpm", "x86_64", "x86_64", "rockylinux:9"),
        ("rpm", "aarch64", "aarch64", "rockylinux:9"),
    ],
)
def test_testing_smoke_runs_for_stable_only_repository(
    tmp_path: Path,
    format: str,
    arch: str,
    machine: str,
    image: str,
) -> None:
    args = run_acceptance_step(
        tmp_path,
        "smoke",
        [{"channel": "stable", "name": "gel-7", "format": format, "arch": arch}],
        machine,
        image,
    )
    assert args is not None, "populated testing repository was skipped"
    assert "/tests/smoke.sh" in args
    assert str(tmp_path / "public") + ":/expected:ro" in args
    assert "GELSTABLE_EXPECTED_FPR=" + "B" * 40 in args


@pytest.mark.parametrize(
    "packages,channel",
    [
        ([], "testing"),
        (
            [{"channel": "testing", "name": "gel-7", "format": "deb", "arch": "amd64"}],
            "stable",
        ),
        (
            [{"channel": "stable", "name": "gel-7", "format": "deb", "arch": "arm64"}],
            "testing",
        ),
    ],
)
def test_unpopulated_smoke_is_skipped(
    tmp_path: Path,
    packages: list[dict[str, str]],
    channel: str,
) -> None:
    assert (
        run_acceptance_step(tmp_path, "smoke", packages, "x86_64", "debian:12", channel)
        is None
    )


def test_migration_passes_committed_fingerprint(tmp_path: Path) -> None:
    args = run_acceptance_step(tmp_path, "migrate", [], "x86_64", "debian:12")
    assert args is not None
    assert "/tests/migrate.sh" in args
    assert str(tmp_path / "public") + ":/expected:ro" in args
    assert "GELSTABLE_EXPECTED_FPR=" + "B" * 40 in args
