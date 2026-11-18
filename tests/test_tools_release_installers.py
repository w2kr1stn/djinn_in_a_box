"""Exercise the just, sops, and bun release installers with offline artifacts."""

import hashlib
import os
import shlex
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from test_tools_herdr_installer import ROOT, run_command, write_executable

VERSION = "1.2.3"
ZERO_DIGEST = "0" * 64


@dataclass
class Release:
    name: str
    root: Path
    env: dict[str, str]
    version_line: str

    @property
    def binary(self) -> Path:
        return Path(self.env["TOOLS_BIN"]) / self.name

    def script(self, text: str, status: int) -> bytes:
        update_check = 'case "$*" in --version) echo "Update check";; esac\n'
        probe = update_check if self.name == "sops" else ""
        return f"#!/bin/sh\n{probe}printf '%s\\n' {shlex.quote(text)}\nexit {status}\n".encode()

    def existing(self, text: str = "older version", status: int = 0) -> bytes:
        data = self.script(text, status) + b"# existing binary\n"
        self.binary.parent.mkdir(parents=True, exist_ok=True)
        self.binary.write_bytes(data)
        self.binary.chmod(0o755)
        return data

    def package(self, text: str, status: int = 0) -> bytes:
        data = self.script(text, status)
        artifact = self.root / "artifact"
        if self.name == "just":
            payload = self.root / "just"
            payload.write_bytes(data)
            with tarfile.open(artifact, "w:gz") as archive:
                archive.add(payload, arcname="just")
        elif self.name == "bun":
            with zipfile.ZipFile(artifact, "w") as archive:
                archive.writestr("bun-linux-x64/bun", data)
        else:
            artifact.write_bytes(data)
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        asset = self.env["ASSET_URL"].rsplit("/", 1)[1]
        entry = {"just": "./", "bun": "*"}.get(self.name, "") + asset
        (self.root / "checksums").write_text(f"{digest}  {entry}\n")
        return data

    def run(self, pin: str = VERSION, *, wrapper: bool = False) -> subprocess.CompletedProcess[str]:
        script = "tools/install.sh" if wrapper else f"tools/installers/{self.name}.sh"
        result = run_command([str(ROOT / script)], self.env | {f"{self.name.upper()}_VERSION": pin})
        assert not list(self.binary.parent.glob(f".{self.name}.*"))
        return result

    def calls(self) -> list[str]:
        log = self.root / "calls"
        return log.read_text().splitlines() if log.exists() else []


@pytest.fixture(params=["just", "sops", "bun"])
def release(request: pytest.FixtureRequest, tmp_path: Path) -> Release:
    name = request.param
    tarball = f"just-{VERSION}-x86_64-unknown-linux-musl.tar.gz"
    sops = f"sops-v{VERSION}"
    repo, tag, asset, checksums = {
        "just": ("casey/just", VERSION, tarball, "SHA256SUMS"),
        "sops": ("getsops/sops", f"v{VERSION}", f"{sops}.linux.amd64", f"{sops}.checksums.txt"),
        "bun": ("oven-sh/bun", f"bun-v{VERSION}", "bun-linux-x64.zip", "SHASUMS256.txt"),
    }[name]
    base = f"https://github.com/{repo}/releases/download/{tag}"
    env = {
        "HOME": str(tmp_path),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "TOOLS_BIN": str(tmp_path / ".cache/djinn-tools/bin"),
        "TOOLS_FILE": str(tmp_path / "tools.txt"),
        "API_URL": f"https://api.github.com/repos/{repo}/releases/latest",
        "RELEASE_TAG": tag,
        "SUMS_URL": f"{base}/{checksums}",
        "ASSET_URL": f"{base}/{asset}",
    }
    write_executable(
        tmp_path / "curl",
        """#!/bin/bash
printf '%s\\n' "$*" >> "$HOME/calls"
case "${!#}" in
    "$API_URL") printf '{"tag_name":"%s"}\\n' "$RELEASE_TAG" ;;
    "$SUMS_URL") cat "$HOME/checksums" ;;
    "$ASSET_URL") cat "$HOME/artifact" ;;
    *) exit 98 ;;
esac
""",
    )
    return Release(name, tmp_path, env, VERSION if name == "bun" else f"{name} {VERSION}")


@pytest.mark.parametrize("pin", ["", VERSION], ids=["latest", "pinned-backslash"])
def test_verified_install(release: Release, pin: str) -> None:
    if pin:
        release.env["TOOLS_BIN"] = str(release.root / "tools\\volume/bin")
    expected = release.package(release.version_line)
    result = release.run(pin=pin)
    assert (result.returncode, result.stdout) == (0, release.version_line + "\n"), result.stderr
    assert release.binary.read_bytes() == expected and os.access(release.binary, os.X_OK)
    calls = release.calls()
    urls = ("SUMS_URL", "ASSET_URL") if pin else ("API_URL", "SUMS_URL", "ASSET_URL")
    assert [call.rsplit(" ", 1)[-1] for call in calls] == [release.env[key] for key in urls]
    assert all("--proto =https --proto-redir =https" in call for call in calls)
    assert all("--max-time 30 " in call for call in calls[:-1])


@pytest.mark.parametrize("pin", ["", "1.2.3-rc.1"])
def test_invalid_version_keeps_existing(release: Release, pin: str) -> None:
    original = release.existing()
    release.env["RELEASE_TAG"] = "bad"
    assert release.run(pin=pin).returncode != 0
    assert len(release.calls()) == (0 if pin else 1)
    assert release.binary.read_bytes() == original


@pytest.mark.parametrize(
    "digests",
    [None, [], ["invalid"], [ZERO_DIGEST] * 2, [ZERO_DIGEST]],
    ids=["missing-file", "missing-entry", "bad-hash", "duplicate", "mismatch"],
)
def test_checksum_failure_keeps_existing(release: Release, digests: list[str] | None) -> None:
    original = release.existing()
    release.package(release.version_line)
    checksums = release.root / "checksums"
    filename = checksums.read_text().split()[1]
    if digests is None:
        checksums.unlink()
    else:
        checksums.write_text("".join(f"{digest}  {filename}\n" for digest in digests))
    result = release.run()
    mismatch = digests == [ZERO_DIGEST]
    assert result.returncode != 0
    assert ("checksum mismatch" if mismatch else "release checksum") in result.stderr
    assert len(release.calls()) == (2 if mismatch else 1)
    assert release.binary.read_bytes() == original


@pytest.mark.parametrize(("suffix", "status"), [("", 7), ("x", 0), ("\nextra output", 0)])
def test_staged_version_failure_keeps_existing(release: Release, suffix: str, status: int) -> None:
    original = release.existing()
    release.package(release.version_line + suffix, status)
    result = release.run()
    assert result.returncode != 0 and "Invalid binary version" in result.stderr
    assert release.binary.read_bytes() == original and os.access(release.binary, os.X_OK)


@pytest.mark.parametrize(("suffix", "status"), [("", 0), ("x", 0), ("\nextra output", 0), ("", 7)])
def test_installed_version_skip(release: Release, suffix: str, status: int) -> None:
    original = release.existing(release.version_line + suffix, status)
    expected = release.package(release.version_line)
    result = release.run()
    assert (result.returncode, result.stdout) == (0, release.version_line + "\n"), result.stderr
    assert len(release.calls()) == (2 if suffix or status else 0)
    assert release.binary.read_bytes() == (expected if suffix or status else original)


def test_directory_at_target(release: Release) -> None:
    release.binary.mkdir(parents=True)
    (release.binary / "keep").write_text("sentinel")
    release.package(release.version_line)
    assert release.run().returncode != 0
    assert [path.name for path in release.binary.iterdir()] == ["keep"]


def test_wrapper_cache(release: Release) -> None:
    Path(release.env["TOOLS_FILE"]).write_text(release.name + "\n")
    release.package(release.version_line)
    assert release.run(wrapper=True).returncode == 0
    marker = release.binary.parent.parent / f"{release.name}.installed"
    assert marker.read_text() == release.version_line + "\n"
    calls = release.calls()
    result = release.run(pin="9.9.9", wrapper=True)
    assert result.returncode == 0 and "already installed (cached)" in result.stderr
    assert release.calls() == calls
