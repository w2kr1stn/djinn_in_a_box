"""Exercise pinned OSV Scanner installation and the real cache driver offline."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "installers" / "osv-scanner.sh"
VERIFIER = "# djinn-verify: /home/dev/.tools/installers/osv-scanner.sh --verify"
VERSION_LINE = "osv-scanner version: 2.7.0"
BASE_URL = "https://github.com/google/osv-scanner/releases/download/v2.7.0"
DIGESTS = {
    "amd64": "160c6253e2922e9e0a6a9771c5c4c40a4b3c6090b34a3cdd5e80c96cc2e4218b",
    "arm64": "4061ef4c021ed3c47f203b818f337e351336cc5cd67c0274fc36bd1dfc728dd7",
}


def write_executable(path: Path, source: str) -> None:
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def payload(version: str = VERSION_LINE, status: int = 0) -> str:
    return (
        '#!/bin/sh\n[ "$1" = --version ] || exit 2\n'
        'printf "%s\\n" "$0" >> "$DJINN_TEST_VERSION_CALLS"\n'
        f'printf "%s\\n" "{version}" "osv-scalibr version: 0.0.0" '
        '"commit: fixture" "built at: fixture"\n'
        f"exit {status}\n"
    )


@pytest.fixture
def installer_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    fake_bin = tmp_path / "fake bin"
    home.mkdir()
    fake_bin.mkdir()
    payload_file = tmp_path / "payload"
    payload_file.write_text(payload(), encoding="utf-8")
    write_executable(
        fake_bin / "uname",
        """
        #!/bin/sh
        echo "$*" >> "$DJINN_TEST_UNAME_CALLS"
        [ "$1" = -m ] || exit 2
        echo "$DJINN_TEST_MACHINE"
        """,
    )
    write_executable(
        fake_bin / "curl",
        f"""
        #!{sys.executable}
        import json
        import os
        import sys
        from pathlib import Path

        args = sys.argv[1:]
        with open(os.environ["DJINN_TEST_CURL_CALLS"], "a", encoding="utf-8") as log:
            log.write(json.dumps(args) + "\\n")
        url = next(arg for arg in args if arg.startswith("https://"))
        if url != os.environ["DJINN_TEST_DOWNLOAD_URL"]:
            sys.exit(6)
        output = Path(args[args.index("-o") + 1])
        failure = os.environ.get("DJINN_TEST_DOWNLOAD_FAIL")
        if failure:
            if failure == "partial":
                output.write_bytes(b"partial download")
            print("curl: fixture download failure", file=sys.stderr)
            sys.exit(18 if failure == "partial" else 22)
        output.write_bytes(Path(os.environ["DJINN_TEST_PAYLOAD"]).read_bytes())
        """,
    )
    # Fake payloads cannot have the published digests. Record stdin and argv,
    # then simulate sha256sum with the independently pinned expected digest.
    write_executable(
        fake_bin / "sha256sum",
        f"""
        #!{sys.executable}
        import json
        import os
        import sys

        data = sys.stdin.buffer.read()
        with open(os.environ["DJINN_TEST_HASH_CALLS"], "a", encoding="utf-8") as log:
            log.write(json.dumps([sys.argv[1:], data.hex()]) + "\\n")
        print(os.environ["DJINN_TEST_DIGEST"] + "  -")
        sys.exit(int(os.environ.get("DJINN_TEST_HASH_STATUS", "0")))
        """,
    )
    write_executable(
        fake_bin / "mv",
        f"""
        #!{sys.executable}
        import json
        import os
        import sys

        with open(os.environ["DJINN_TEST_MV_CALLS"], "a", encoding="utf-8") as log:
            log.write(json.dumps(sys.argv[1:]) + "\\n")
        os.execv("/usr/bin/mv", ["mv", *sys.argv[1:]])
        """,
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("DJINN_TEST_")}
    env.update(
        {
            "HOME": str(home),
            "TOOLS_BIN": str(tmp_path / "tools\\volume" / "bin"),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DJINN_TEST_MACHINE": "x86_64",
            "DJINN_TEST_DOWNLOAD_URL": f"{BASE_URL}/osv-scanner_linux_amd64",
            "DJINN_TEST_DIGEST": DIGESTS["amd64"],
            "DJINN_TEST_PAYLOAD": str(payload_file),
            **{
                f"DJINN_TEST_{name}": str(tmp_path / name.lower())
                for name in (
                    "CURL_CALLS",
                    "HASH_CALLS",
                    "UNAME_CALLS",
                    "VERSION_CALLS",
                    "MV_CALLS",
                )
            },
        }
    )
    return env


def run_command(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        check=False,
        cwd=env["HOME"],
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=10,
    )


def log_lines(env: dict[str, str], name: str) -> list[str]:
    log = Path(env[f"DJINN_TEST_{name}"])
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def existing_binary(
    env: dict[str, str], version: str = "osv-scanner version: 2.6.0", status: int = 0
) -> bytes:
    binary = Path(env["TOOLS_BIN"]) / "osv-scanner"
    binary.parent.mkdir(parents=True, exist_ok=True)
    write_executable(binary, payload(version, status))
    return binary.read_bytes()


def assert_preserved(env: dict[str, str], original: bytes | None) -> None:
    binary = Path(env["TOOLS_BIN"]) / "osv-scanner"
    if original is None:
        assert not binary.exists()
    else:
        assert binary.read_bytes() == original
        assert binary.stat().st_mode & 0o777 == 0o755
    assert not list(binary.parent.glob(".osv-scanner.*"))


@pytest.mark.parametrize(
    "machine,arch",
    [("x86_64", "amd64"), ("amd64", "amd64"), ("aarch64", "arm64"), ("arm64", "arm64")],
)
@pytest.mark.parametrize("existing", [False, True], ids=["fresh", "existing"])
def test_pinned_release_guards_hash_staging_and_atomic_replace(
    installer_env: dict[str, str], existing: bool, machine: str, arch: str
) -> None:
    env = installer_env
    if existing:
        existing_binary(env)
    env.update(
        {
            "DJINN_TEST_MACHINE": machine,
            "DJINN_TEST_DOWNLOAD_URL": f"{BASE_URL}/osv-scanner_linux_{arch}",
            "DJINN_TEST_DIGEST": DIGESTS[arch],
            # An inherited override must not alter this fixed release.
            "OSV_SCANNER_VERSION": "99.0.0",
        }
    )
    result = run_command([str(SCRIPT)], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == VERSION_LINE + "\n"
    binary = Path(env["TOOLS_BIN"]) / "osv-scanner"
    expected_payload = Path(env["DJINN_TEST_PAYLOAD"]).read_bytes()
    assert binary.read_bytes() == expected_payload
    assert binary.stat().st_mode & 0o777 == 0o755
    calls = [json.loads(line) for line in log_lines(env, "CURL_CALLS")]
    assert len(calls) == 1
    assert env["DJINN_TEST_DOWNLOAD_URL"] in calls[0]
    staged = Path(calls[0][calls[0].index("-o") + 1])
    assert staged.parent == binary.parent
    assert staged.name.startswith(".osv-scanner.")
    assert [json.loads(line) for line in log_lines(env, "HASH_CALLS")] == [
        [[], expected_payload.hex()]
    ]
    assert log_lines(env, "VERSION_CALLS") == [str(staged)]
    assert [json.loads(line) for line in log_lines(env, "MV_CALLS")] == [
        ["-T", str(staged), str(binary)]
    ]
    assert not list(binary.parent.glob(".osv-scanner.*"))


def test_unsupported_architecture_fails_clearly_without_download(
    installer_env: dict[str, str],
) -> None:
    env = installer_env
    original = existing_binary(env)
    env["DJINN_TEST_MACHINE"] = "riscv64"
    result = run_command([str(SCRIPT)], env)
    assert result.returncode == 1
    assert "Unsupported architecture 'riscv64'" in result.stderr
    assert log_lines(env, "CURL_CALLS") == []
    assert_preserved(env, original)


@pytest.mark.parametrize("existing", [False, True], ids=["absent", "existing"])
@pytest.mark.parametrize("failure", ["404", "partial", "checksum", "hash-status"])
def test_transfer_and_hash_failures_preserve_binary(
    installer_env: dict[str, str], existing: bool, failure: str
) -> None:
    env = installer_env
    original = existing_binary(env) if existing else None
    if failure in {"404", "partial"}:
        env["DJINN_TEST_DOWNLOAD_FAIL"] = failure
    elif failure == "checksum":
        env["DJINN_TEST_DIGEST"] = "0" * 64
    else:
        env["DJINN_TEST_HASH_STATUS"] = "1"
    result = run_command([str(SCRIPT)], env)
    assert result.returncode != 0
    assert result.stdout == ""
    if failure == "checksum":
        assert f"checksum mismatch (expected '{DIGESTS['amd64']}', got '{'0' * 64}')" in (
            result.stderr
        )
    elif failure == "hash-status":
        assert "Failed to compute SHA-256" in result.stderr
    assert log_lines(env, "VERSION_CALLS") == []
    assert log_lines(env, "MV_CALLS") == []
    assert_preserved(env, original)


@pytest.mark.parametrize(
    "version,status",
    [
        ("osv-scanner version: 2.6.0", 0),
        ("osv-scanner version: 2.7.00", 0),
        ("osv-scanner version: 2.7.0-rc1", 0),
        ("prefix osv-scanner version: 2.7.0", 0),
        (VERSION_LINE, 7),
    ],
)
def test_staged_version_failure_preserves_binary(
    installer_env: dict[str, str], version: str, status: int
) -> None:
    env = installer_env
    original = existing_binary(env)
    Path(env["DJINN_TEST_PAYLOAD"]).write_text(payload(version, status), encoding="utf-8")
    result = run_command([str(SCRIPT)], env)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "Invalid binary version" in result.stderr
    assert log_lines(env, "MV_CALLS") == []
    assert_preserved(env, original)


@pytest.mark.parametrize("step", ["mktemp", "chmod", "mv"])
def test_local_failure_preserves_binary(installer_env: dict[str, str], step: str) -> None:
    env = installer_env
    original = existing_binary(env)
    fake_bin = Path(env["PATH"].split(os.pathsep)[0])
    write_executable(fake_bin / step, "#!/bin/sh\nexit 1\n")
    result = run_command([str(SCRIPT)], env)
    assert result.returncode != 0
    assert result.stdout == ""
    assert_preserved(env, original)


def test_target_directory_is_not_entered(installer_env: dict[str, str]) -> None:
    env = installer_env
    binary = Path(env["TOOLS_BIN"]) / "osv-scanner"
    binary.mkdir(parents=True)
    (binary / "keep").write_text("keep\n", encoding="utf-8")
    result = run_command([str(SCRIPT)], env)
    assert result.returncode != 0
    assert [path.name for path in binary.iterdir()] == ["keep"]
    assert not list(binary.parent.glob(".osv-scanner.*"))


@pytest.mark.parametrize("empty", [False, True], ids=["unset", "empty"])
def test_default_tools_bin_is_under_home(installer_env: dict[str, str], empty: bool) -> None:
    env = installer_env
    override = Path(env.pop("TOOLS_BIN"))
    if empty:
        env["TOOLS_BIN"] = ""
    result = run_command([str(SCRIPT)], env)
    assert result.returncode == 0, result.stdout + result.stderr
    binary = Path(env["HOME"]) / ".cache" / "djinn-tools" / "bin" / "osv-scanner"
    assert binary.read_bytes() == Path(env["DJINN_TEST_PAYLOAD"]).read_bytes()
    assert binary.stat().st_mode & 0o777 == 0o755
    assert not override.exists()


@pytest.mark.parametrize(
    "version,status,executable,valid",
    [
        (VERSION_LINE, 0, True, True),
        ("osv-scanner version: 2.6.0", 0, True, False),
        ("osv-scanner version: 2.7.00", 0, True, False),
        ("osv-scanner version: 2.7.0-rc1", 0, True, False),
        (VERSION_LINE, 7, True, False),
        (VERSION_LINE, 0, False, False),
        (None, 0, False, False),
    ],
)
def test_verify_is_exact_offline_and_side_effect_free(
    installer_env: dict[str, str],
    version: str | None,
    status: int,
    executable: bool,
    valid: bool,
) -> None:
    env = installer_env
    original = existing_binary(env, version, status) if version is not None else None
    binary = Path(env["TOOLS_BIN"]) / "osv-scanner"
    if version is not None and not executable:
        binary.chmod(0o644)
    env["DJINN_TEST_MACHINE"] = "unsupported"
    env["DJINN_TEST_DOWNLOAD_FAIL"] = "404"
    result = run_command([str(SCRIPT), "--verify"], env)
    assert result.returncode == (0 if valid else 1)
    assert result.stdout == ""
    for name in ("UNAME_CALLS", "CURL_CALLS", "HASH_CALLS", "MV_CALLS"):
        assert log_lines(env, name) == []
    assert log_lines(env, "VERSION_CALLS") == (
        [str(binary)] if version is not None and executable else []
    )
    if original is None:
        assert not binary.parent.exists()
    else:
        assert binary.read_bytes() == original
        assert binary.stat().st_mode & 0o777 == (0o755 if executable else 0o644)


def cache_command(env: dict[str, str], tmp_path: Path) -> list[str]:
    installers = tmp_path / "installers"
    installers.mkdir()
    copy = installers / "osv-scanner.sh"
    source = SCRIPT.read_text(encoding="utf-8")
    assert VERIFIER in source.splitlines()
    # The real image copies installers here. Only relocate that image path in
    # this isolated copy; the production header must keep its literal command.
    image_path = "/home/dev/.tools/installers/osv-scanner.sh"
    write_executable(copy, source.replace(image_path, str(copy)))
    cache = tmp_path / "integration cache"
    env.update(
        {
            "CACHE_DIR": str(cache),
            "TOOLS_BIN": str(cache / "bin"),
            "INSTALLERS_DIR": str(installers),
            "TOOLS_FILE": str(tmp_path / "tools.txt"),
            "OUTPUT_LIB": str(tmp_path / "missing-output-lib.sh"),
            "NO_COLOR": "1",
        }
    )
    Path(env["TOOLS_FILE"]).write_text("osv-scanner\n", encoding="utf-8")
    return ["bash", str(ROOT / "tools" / "install.sh")]


def test_real_cache_marker_and_offline_warm_skip(
    installer_env: dict[str, str], tmp_path: Path
) -> None:
    env = installer_env
    command = cache_command(env, tmp_path)
    result = run_command(command, env)
    assert result.returncode == 0, result.stdout + result.stderr
    marker = Path(env["CACHE_DIR"]) / "osv-scanner.installed"
    assert marker.read_text(encoding="utf-8") == VERSION_LINE + "\n"
    original = (Path(env["TOOLS_BIN"]) / "osv-scanner").read_bytes()
    Path(env["DJINN_TEST_CURL_CALLS"]).unlink()
    env["DJINN_TEST_DOWNLOAD_FAIL"] = "404"
    result = run_command(command, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 tool(s) already installed (cached)" in result.stderr
    assert log_lines(env, "CURL_CALLS") == []
    assert_preserved(env, original)


def test_real_cache_reinstalls_invalid_version(
    installer_env: dict[str, str], tmp_path: Path
) -> None:
    env = installer_env
    command = cache_command(env, tmp_path)
    first = run_command(command, env)
    assert first.returncode == 0, first.stdout + first.stderr
    existing_binary(env, "osv-scanner version: 2.7.00")
    Path(env["DJINN_TEST_CURL_CALLS"]).unlink()
    result = run_command(command, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Cache check failed for osv-scanner" in result.stderr
    assert len(log_lines(env, "CURL_CALLS")) == 1
    assert (Path(env["TOOLS_BIN"]) / "osv-scanner").read_bytes() == Path(
        env["DJINN_TEST_PAYLOAD"]
    ).read_bytes()
    marker = Path(env["CACHE_DIR"]) / "osv-scanner.installed"
    assert marker.read_text(encoding="utf-8") == VERSION_LINE + "\n"


@pytest.mark.parametrize("failure", ["download", "checksum", "version"])
def test_real_cache_failure_writes_no_marker(
    installer_env: dict[str, str], tmp_path: Path, failure: str
) -> None:
    env = installer_env
    command = cache_command(env, tmp_path)
    first = run_command(command, env)
    assert first.returncode == 0, first.stdout + first.stderr
    original = existing_binary(env)
    if failure == "download":
        env["DJINN_TEST_DOWNLOAD_FAIL"] = "partial"
    elif failure == "checksum":
        env["DJINN_TEST_DIGEST"] = "0" * 64
    else:
        Path(env["DJINN_TEST_PAYLOAD"]).write_text(
            payload("osv-scanner version: 2.7.0-rc1"), encoding="utf-8"
        )
    result = run_command(command, env)
    # The cache driver reports per-tool failures without failing startup itself.
    assert result.returncode == 0
    assert "Failed to install osv-scanner" in result.stderr
    assert not (Path(env["CACHE_DIR"]) / "osv-scanner.installed").exists()
    assert_preserved(env, original)


def test_example_keeps_scanner_opt_in() -> None:
    lines = (ROOT / "tools" / "tools.txt.example").read_text(encoding="utf-8").splitlines()
    entries = [line for line in lines if line.lstrip("# ").split()[:1] == ["osv-scanner"]]
    assert len(entries) == 1
    assert entries[0].startswith("# osv-scanner")
