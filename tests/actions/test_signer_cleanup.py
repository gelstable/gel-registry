"""The shared signer terminates its isolated agent on success and failure."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "failure", ["", "import", "rpm-import", "sign", "verify", "cleanup"]
)
def test_signer_cleans_agent_before_deleting_home(tmp_path: Path, failure: str) -> None:
    bins = tmp_path / "bin"
    bins.mkdir()
    tool = bins / "tool"
    tool.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "name = Path(sys.argv[0]).name\n"
        "args = sys.argv[1:]\n"
        "home = Path(os.environ['GNUPGHOME'])\n"
        "if name == 'gpgconf':\n"
        "    with open(os.environ['PROBE_LOG'], 'w') as log:\n"
        "        json.dump({'home': str(home), 'exists': home.is_dir()}, log)\n"
        "    sys.exit(1 if os.environ['PROBE_FAILURE'] == 'cleanup' else 0)\n"
        "stage = ('import' if '--import' in args else 'export') if (\n"
        "    name == 'gpg') else (\n"
        "    'sign' if name == 'rpmsign' else (\n"
        "        'rpm-import' if '--import' in args else 'verify'))\n"
        "if os.environ['PROBE_FAILURE'] == stage:\n"
        "    sys.exit(23)\n"
        "if stage == 'export':\n"
        "    print('disposable public certificate')\n"
        "if stage == 'verify':\n"
        "    print('RSA Signature: OK')\n"
    )
    tool.chmod(0o755)
    for name in ("gpg", "gpgconf", "rpmkeys", "rpmsign"):
        (bins / name).symlink_to(tool)
    packages = tmp_path / "packages"
    packages.mkdir()
    (packages / "fixture.rpm").write_bytes(b"fixture")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    log = tmp_path / "cleanup.json"
    script = Path(__file__).parents[2] / ".github/actions/sign-rpms/sign-rpms.sh"
    result = subprocess.run(
        ["bash", str(script), str(packages)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": str(bins) + os.pathsep + os.environ["PATH"],
            "TMPDIR": str(scratch),
            "PACKAGE_SIGNING_KEY": "disposable fixture",
            "PACKAGE_SIGNING_FPR": "a" * 40,
            "PROBE_LOG": str(log),
            "PROBE_FAILURE": failure,
        },
    )
    assert result.returncode == (
        23 if failure in {"import", "rpm-import", "sign", "verify"} else 0
    ), result.stderr
    assert log.exists(), "signer did not terminate its agent"
    cleanup = json.loads(log.read_text())
    assert cleanup["exists"], "agent was terminated after deleting its home"
    assert not Path(cleanup["home"]).exists()
    assert list(scratch.iterdir()) == []
