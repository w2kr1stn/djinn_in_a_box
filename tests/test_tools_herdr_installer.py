"""Exercise the herdr installer and real tools cache flow without network access."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "installers" / "herdr.sh"
API_URL = "https://api.github.com/repos/herdrdev/herdr/releases/latest"
DOWNLOAD_URL = "https://github.com/herdrdev/herdr/releases/download/v0.9.3/herdr-linux-x86_64"
FOREIGN_URL = "https://foreign.invalid/herdr-linux-x86_64"
GUARDS = [
    "-fsSL",
    "--proto",
    "=https",
    "--tlsv1.2",
    "--connect-timeout",
    "10",
    "--retry",
    "4",
    "--retry-delay",
    "3",
    "--speed-limit",
    "2048",
    "--speed-time",
    "30",
]


def write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def payload(version: str = "herdr 0.9.3", status: int = 0) -> str:
    return f'#!/bin/sh\n[ "$1" = --version ] || exit 2\necho "{version}"\nexit {status}\n'


@pytest.fixture
def installer_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    fake_bin = tmp_path / "fake bin"
    home.mkdir()
    fake_bin.mkdir()
    payload_file = tmp_path / "payload"
    payload_file.write_text(payload(), encoding="utf-8")
    # Captured releases/latest shape: the target is the second of five assets.
    assets = [
        {
            "name": name,
            "digest": "sha256:"
            + (
                hashlib.sha256(payload_file.read_bytes()).hexdigest()
                if name == "herdr-linux-x86_64"
                else "0" * 64
            ),
            "browser_download_url": FOREIGN_URL,
        }
        for name in (
            "herdr-linux-aarch64",
            "herdr-linux-x86_64",
            "herdr-macos-aarch64",
            "herdr-macos-x86_64",
            "herdr-windows-x86_64.zip",
        )
    ]
    release_file = tmp_path / "release.json"
    release_file.write_text(json.dumps({"tag_name": "v0.9.3", "assets": assets}), encoding="utf-8")
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
        output = Path(args[args.index("-o") + 1]) if "-o" in args else None
        if url == {API_URL!r}:
            if os.environ.get("DJINN_TEST_API_FAIL"):
                print("curl: (6) Could not resolve host: api.github.com", file=sys.stderr)
                sys.exit(6)
            data = Path(os.environ["DJINN_TEST_RELEASE"]).read_bytes()
        elif url == os.environ["DJINN_TEST_DOWNLOAD_URL"]:
            failure = os.environ.get("DJINN_TEST_DOWNLOAD_FAIL")
            if failure == "404":
                print("curl: (22) The requested URL returned error: 404", file=sys.stderr)
                sys.exit(22)
            if failure == "partial":
                if output is not None:
                    output.write_bytes(b"partial transfer")
                print(
                    "curl: (18) transfer closed with 1000 bytes remaining to read",
                    file=sys.stderr,
                )
                sys.exit(18)
            data = Path(os.environ["DJINN_TEST_PAYLOAD"]).read_bytes()
        else:
            host = url.split("/")[2]
            print("curl: (6) Could not resolve host: " + host, file=sys.stderr)
            sys.exit(6)
        if output is not None:
            output.write_bytes(data)
        else:
            sys.stdout.buffer.write(data)
        """,
    )
    write_executable(
        fake_bin / "mktemp",
        """
        #!/bin/sh
        printf '%s\\n' "$@" >> "$DJINN_TEST_MKTEMP_TEMPLATES"
        exec /usr/bin/mktemp "$@"
        """,
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("DJINN_TEST_")}
    env.update(
        {
            "HOME": str(home),
            "TOOLS_BIN": str(tmp_path / "tools volume" / "bin"),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DJINN_TEST_CURL_CALLS": str(tmp_path / "curl-calls"),
            "DJINN_TEST_MKTEMP_TEMPLATES": str(tmp_path / "mktemp-templates"),
            "DJINN_TEST_RELEASE": str(release_file),
            "DJINN_TEST_PAYLOAD": str(payload_file),
            "DJINN_TEST_DOWNLOAD_URL": DOWNLOAD_URL,
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


def curl_calls(env: dict[str, str]) -> list[list[str]]:
    log = Path(env["DJINN_TEST_CURL_CALLS"])
    return (
        [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )


def existing_binary(env: dict[str, str], version: str = "herdr 0.9.2", status: int = 0) -> bytes:
    binary = Path(env["TOOLS_BIN"]) / "herdr"
    binary.parent.mkdir(parents=True, exist_ok=True)
    write_executable(binary, payload(version, status))
    return binary.read_bytes()


def assert_preserved(env: dict[str, str], original: bytes | None) -> None:
    binary = Path(env["TOOLS_BIN"]) / "herdr"
    if original is None:
        assert not binary.exists()
    else:
        assert binary.read_bytes() == original
        assert binary.stat().st_mode & 0o111 == 0o111
    assert not list(binary.parent.glob(".herdr.*"))


def set_payload(env: dict[str, str], version: str, status: int = 0) -> None:
    file = Path(env["DJINN_TEST_PAYLOAD"])
    file.write_text(payload(version, status), encoding="utf-8")
    release_file = Path(env["DJINN_TEST_RELEASE"])
    release = json.loads(release_file.read_text(encoding="utf-8"))
    release["assets"][1]["digest"] = "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest()
    release_file.write_text(json.dumps(release), encoding="utf-8")


def test_success_guards_origin_staging_and_output(installer_env: dict[str, str]) -> None:
    env = installer_env
    result = run_command([str(SCRIPT)], env)
    assert result.returncode == 0, result.stdout + result.stderr
    binary = Path(env["TOOLS_BIN"]) / "herdr"
    assert binary.read_bytes() == Path(env["DJINN_TEST_PAYLOAD"]).read_bytes()
    assert binary.stat().st_mode & 0o111 == 0o111
    assert result.stdout == "herdr 0.9.3\n"
    assert result.stderr == "herdr: Installing version 0.9.3\n"
    calls = curl_calls(env)
    assert len(calls) == 2
    assert calls[0] == [*GUARDS, "--max-time", "30", API_URL]
    assert calls[1][:-1] == [*GUARDS, DOWNLOAD_URL, "-o"]
    staged = Path(calls[1][-1])
    assert staged.parent == binary.parent
    assert staged.name.startswith(".herdr.")
    assert FOREIGN_URL not in [arg for call in calls for arg in call]
    assert Path(env["DJINN_TEST_MKTEMP_TEMPLATES"]).read_text(encoding="utf-8").splitlines() == [
        str(binary.parent / ".herdr.XXXXXX"),
    ]
    assert not list(binary.parent.glob(".herdr.*"))


@pytest.mark.parametrize(
    "case",
    [
        "api-unreachable",
        "malformed-json",
        "tag-latest",
        "tag-no-v",
        "tag-missing",
        "tag-prerelease",
        "tag-short",
        "tag-trailing",
        "asset-missing",
        "asset-duplicate",
        "digest-missing",
        "digest-sha1",
        "digest-nonhex",
        "digest-uppercase",
        "digest-short",
    ],
)
def test_metadata_refused_before_download(installer_env: dict[str, str], case: str) -> None:
    env = installer_env
    original = existing_binary(env, "herdr 0.9.3")
    release_file = Path(env["DJINN_TEST_RELEASE"])
    release = json.loads(release_file.read_text(encoding="utf-8"))
    expected = "herdr: Invalid SHA-256 digest for release 'v0.9.3', skipping"
    if case == "api-unreachable":
        env["DJINN_TEST_API_FAIL"] = "1"
        expected = f"herdr: Failed to fetch release metadata from {API_URL}, skipping"
    elif case == "malformed-json":
        expected = f"herdr: Invalid release JSON from {API_URL}, skipping"
    elif case.startswith("tag-"):
        tag = {
            "tag-latest": "latest",
            "tag-no-v": "0.9.3",
            "tag-missing": "",
            "tag-prerelease": "v0.9.3-rc1",
            "tag-short": "v0.9",
            "tag-trailing": "v0.9.3x",
        }[case]
        if case == "tag-missing":
            del release["tag_name"]
        else:
            release["tag_name"] = tag
        expected = f"herdr: Invalid release tag '{tag}', skipping"
    elif case == "asset-missing":
        del release["assets"][1]
    elif case == "asset-duplicate":
        release["assets"].append(release["assets"][1].copy())
    elif case == "digest-missing":
        del release["assets"][1]["digest"]
    else:
        release["assets"][1]["digest"] = {
            "digest-sha1": "sha1:" + "a" * 64,
            "digest-nonhex": "sha256:" + "z" * 64,
            "digest-uppercase": "sha256:" + "A" * 64,
            "digest-short": "sha256:" + "a" * 63,
        }[case]
    release_file.write_text(
        "{broken" if case == "malformed-json" else json.dumps(release), encoding="utf-8"
    )
    result = run_command([str(SCRIPT)], env)
    assert result.returncode == 1
    assert result.stdout == ""
    assert [line for line in result.stderr.splitlines() if line.startswith("herdr:")] == [expected]
    if case == "api-unreachable":
        assert result.stderr.splitlines()[0] == "curl: (6) Could not resolve host: api.github.com"
    assert curl_calls(env) == [[*GUARDS, "--max-time", "30", API_URL]]
    assert_preserved(env, original)


@pytest.mark.parametrize("existing", [False, True], ids=["absent", "existing"])
@pytest.mark.parametrize(
    "failure,status,message",
    [
        ("404", 22, "curl: (22) The requested URL returned error: 404"),
        ("partial", 18, "curl: (18) transfer closed with 1000 bytes remaining to read"),
    ],
)
def test_download_failure_preserves_binary(
    installer_env: dict[str, str],
    existing: bool,
    failure: str,
    status: int,
    message: str,
) -> None:
    env = installer_env
    original = existing_binary(env) if existing else None
    env["DJINN_TEST_DOWNLOAD_FAIL"] = failure
    result = run_command([str(SCRIPT)], env)
    assert result.returncode == status
    assert result.stdout == ""
    assert result.stderr.splitlines()[-1] == message
    assert len(curl_calls(env)) == 2
    assert_preserved(env, original)


def test_checksum_mismatch_preserves_binary(installer_env: dict[str, str]) -> None:
    env = installer_env
    original = existing_binary(env)
    env["DJINN_TEST_UNVERIFIED_EXECUTION"] = str(Path(env["HOME"]) / "unverified-execution")
    file = Path(env["DJINN_TEST_PAYLOAD"])
    file.write_text(
        payload().replace("#!/bin/sh\n", '#!/bin/sh\ntouch "$DJINN_TEST_UNVERIFIED_EXECUTION"\n'),
        encoding="utf-8",
    )
    release_file = Path(env["DJINN_TEST_RELEASE"])
    release = json.loads(release_file.read_text(encoding="utf-8"))
    release["assets"][1]["digest"] = "sha256:" + "0" * 64
    release_file.write_text(json.dumps(release), encoding="utf-8")
    actual = hashlib.sha256(Path(env["DJINN_TEST_PAYLOAD"]).read_bytes()).hexdigest()
    result = run_command([str(SCRIPT)], env)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.splitlines()[-1] == (
        f"herdr: checksum mismatch (expected '{'0' * 64}', got '{actual}'), skipping"
    )
    assert_preserved(env, original)
    assert not Path(env["DJINN_TEST_UNVERIFIED_EXECUTION"]).exists()


@pytest.mark.parametrize("version,status", [("herdr 0.9.3", 1), ("herdr 0.9.2", 0), ("0.9.3", 0)])
def test_staged_version_refused_before_replace(
    installer_env: dict[str, str],
    version: str,
    status: int,
) -> None:
    env = installer_env
    # Differs from every payload, so a replacement cannot pass as preservation.
    original = existing_binary(env, "herdr 0.9.1")
    set_payload(env, version, status)
    result = run_command([str(SCRIPT)], env)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.splitlines()[-1] == (
        f"herdr: Invalid binary version (expected 'herdr 0.9.3', got '{version}'), skipping"
    )
    assert_preserved(env, original)


@pytest.mark.parametrize("step", ["chmod", "mktemp", "mv", "sha256sum", "sha256sum-empty"])
def test_local_failure_preserves_binary(installer_env: dict[str, str], step: str) -> None:
    env = installer_env
    original = existing_binary(env)
    fake_bin = Path(env["PATH"].split(os.pathsep)[0])
    if step == "chmod":
        fake = """
        #!/bin/sh
        echo "chmod: changing permissions of '$2': Operation not permitted" >&2
        exit 1
        """
    elif step == "mktemp":
        fake = """
        #!/bin/sh
        echo "mktemp: failed to create file via template '$1': Permission denied" >&2
        exit 1
        """
    elif step == "mv":
        fake = """
        #!/bin/sh
        [ "$1" = -T ] && shift
        echo "mv: cannot move '$1' to '$2': Permission denied" >&2
        exit 1
        """
    elif step == "sha256sum":
        expected = hashlib.sha256(Path(env["DJINN_TEST_PAYLOAD"]).read_bytes()).hexdigest()
        fake = f'#!/bin/sh\necho "{expected}  $1"\nexit 1\n'
    else:
        fake = "#!/bin/sh\nexit 0\n"
    write_executable(fake_bin / ("sha256sum" if step.startswith("sha256sum") else step), fake)
    result = run_command([str(SCRIPT)], env)
    assert result.returncode == 1
    assert result.stdout == ""
    if step == "sha256sum":
        assert (
            result.stderr.splitlines()[-1]
            == "herdr: Failed to compute SHA-256 for release 'v0.9.3', skipping"
        )
    elif step == "sha256sum-empty":
        expected = hashlib.sha256(Path(env["DJINN_TEST_PAYLOAD"]).read_bytes()).hexdigest()
        assert result.stderr.splitlines()[-1] == (
            f"herdr: checksum mismatch (expected '{expected}', got ''), skipping"
        )
    elif step == "mktemp":
        template = Path(env["TOOLS_BIN"]) / ".herdr.XXXXXX"
        assert result.stderr == (
            f"mktemp: failed to create file via template '{template}': Permission denied\n"
        )
    else:
        staged = curl_calls(env)[1][-1]
        binary = Path(env["TOOLS_BIN"]) / "herdr"
        expected_error = (
            f"chmod: changing permissions of '{staged}': Operation not permitted"
            if step == "chmod"
            else f"mv: cannot move '{staged}' to '{binary}': Permission denied"
        )
        assert result.stderr.splitlines()[-1] == expected_error
    assert_preserved(env, original)


def test_directory_at_target_is_not_entered(installer_env: dict[str, str]) -> None:
    env = installer_env
    target = Path(env["TOOLS_BIN"]) / "herdr"
    target.mkdir(parents=True)
    (target / "keep").write_text("keep\n", encoding="utf-8")
    result = run_command([str(SCRIPT)], env)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.splitlines()[-1] == (
        f"mv: cannot overwrite directory '{target}' with non-directory"
    )
    assert [path.name for path in target.iterdir()] == ["keep"]
    assert not list(target.parent.glob(".herdr.*"))


def test_backslash_in_tools_bin_still_matches_checksum(
    installer_env: dict[str, str], tmp_path: Path
) -> None:
    env = installer_env
    # sha256sum escapes such file names and prefixes the hash with a backslash.
    env["TOOLS_BIN"] = str(tmp_path / "tools\\volume" / "bin")
    result = run_command([str(SCRIPT)], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "herdr 0.9.3\n"
    assert (Path(env["TOOLS_BIN"]) / "herdr").read_bytes() == Path(
        env["DJINN_TEST_PAYLOAD"]
    ).read_bytes()


@pytest.mark.parametrize(
    "version,status,skip",
    [
        ("herdr 0.9.3", 0, True),
        ("herdr 0.9.3", 7, False),
        ("herdr 0.9.2", 0, False),
        ("0.9.3", 0, False),
    ],
)
def test_version_skip_requires_exact_output_and_success(
    installer_env: dict[str, str],
    version: str,
    status: int,
    skip: bool,
) -> None:
    env = installer_env
    original = existing_binary(env, version, status)
    result = run_command([str(SCRIPT)], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "herdr 0.9.3\n"
    if skip:
        assert curl_calls(env) == [[*GUARDS, "--max-time", "30", API_URL]]
        assert result.stderr == "herdr: Version 0.9.3 already installed, skipping download\n"
        assert_preserved(env, original)
    else:
        assert len(curl_calls(env)) == 2
        assert (Path(env["TOOLS_BIN"]) / "herdr").read_bytes() == Path(
            env["DJINN_TEST_PAYLOAD"]
        ).read_bytes()
        assert not list(Path(env["TOOLS_BIN"]).glob(".herdr.*"))


@pytest.mark.parametrize("empty", [False, True], ids=["unset", "empty"])
def test_default_tools_bin_is_under_home(installer_env: dict[str, str], empty: bool) -> None:
    env = installer_env
    override = Path(env.pop("TOOLS_BIN"))
    if empty:
        env["TOOLS_BIN"] = ""
    result = run_command([str(SCRIPT)], env)
    assert result.returncode == 0, result.stdout + result.stderr
    binary = Path(env["HOME"]) / ".cache" / "djinn-tools" / "bin" / "herdr"
    assert binary.read_bytes() == Path(env["DJINN_TEST_PAYLOAD"]).read_bytes()
    assert binary.stat().st_mode & 0o111 == 0o111
    assert not override.exists()


def test_real_install_cache_and_rebuild_skip(installer_env: dict[str, str], tmp_path: Path) -> None:
    env = installer_env
    cache = tmp_path / "integration cache"
    env.update(
        {
            "CACHE_DIR": str(cache),
            "INSTALLERS_DIR": str(SCRIPT.parent),
            "TOOLS_FILE": str(tmp_path / "tools.txt"),
            "OUTPUT_LIB": str(tmp_path / "missing-output-lib.sh"),
            "NO_COLOR": "1",
        }
    )
    Path(env["TOOLS_FILE"]).write_text("herdr\n", encoding="utf-8")
    timestamp = Path(env["HOME"]) / ".build-timestamp"
    timestamp.write_text("build-one\n", encoding="utf-8")
    command = ["bash", str(ROOT / "tools" / "install.sh")]
    first = run_command(command, env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "[ok] [tools] herdr installed (herdr 0.9.3)" in first.stderr
    marker = cache / "herdr.installed"
    assert marker.read_text(encoding="utf-8") == "herdr 0.9.3\n"
    binary = cache / "bin" / "herdr"
    original = binary.read_bytes()
    assert len(curl_calls(env)) == 2
    Path(env["DJINN_TEST_CURL_CALLS"]).unlink()
    second = run_command(command, env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "[ok] [tools] 1 tool(s) already installed (cached)" in second.stderr
    assert curl_calls(env) == []
    assert binary.read_bytes() == original
    # A different marker proves that rebuild invalidation rewrites it.
    marker.write_text("old marker\n", encoding="utf-8")
    timestamp.write_text("build-two\n", encoding="utf-8")
    third = run_command(command, env)
    assert third.returncode == 0, third.stdout + third.stderr
    assert "[info] [tools] Installing herdr..." in third.stderr
    assert "[ok] [tools] herdr installed (herdr 0.9.3)" in third.stderr
    assert curl_calls(env) == [[*GUARDS, "--max-time", "30", API_URL]]
    assert marker.read_text(encoding="utf-8") == "herdr 0.9.3\n"
    assert (cache / ".build-timestamp").read_text(encoding="utf-8") == "build-two\n"
    assert binary.read_bytes() == original
    assert not list(binary.parent.glob(".herdr.*"))


def test_latest_stable_version_is_used(installer_env: dict[str, str]) -> None:
    env = installer_env
    set_payload(env, "herdr 12.34.56")
    release_file = Path(env["DJINN_TEST_RELEASE"])
    release = json.loads(release_file.read_text(encoding="utf-8"))
    release["tag_name"] = "v12.34.56"
    release_file.write_text(json.dumps(release), encoding="utf-8")
    env["DJINN_TEST_DOWNLOAD_URL"] = (
        "https://github.com/herdrdev/herdr/releases/download/v12.34.56/herdr-linux-x86_64"
    )
    result = run_command([str(SCRIPT)], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "herdr 12.34.56\n"
    assert curl_calls(env)[1][:-1] == [*GUARDS, env["DJINN_TEST_DOWNLOAD_URL"], "-o"]
    assert (Path(env["TOOLS_BIN"]) / "herdr").read_bytes() == Path(
        env["DJINN_TEST_PAYLOAD"]
    ).read_bytes()


@pytest.mark.parametrize("probe", ["installed", "staged"])
def test_version_probes_have_null_stdin(installer_env: dict[str, str], probe: str) -> None:
    env = installer_env
    guarded = payload().replace("#!/bin/sh\n", '#!/bin/sh\n[ -z "$(cat)" ] || exit 7\n')
    if probe == "installed":
        existing_binary(env, "herdr 0.9.3")
        write_executable(Path(env["TOOLS_BIN"]) / "herdr", guarded)
    else:
        file = Path(env["DJINN_TEST_PAYLOAD"])
        file.write_text(guarded, encoding="utf-8")
        release_file = Path(env["DJINN_TEST_RELEASE"])
        release = json.loads(release_file.read_text(encoding="utf-8"))
        release["assets"][1]["digest"] = "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest()
        release_file.write_text(json.dumps(release), encoding="utf-8")
    # The parent keeps DEVNULL; the shell supplies input to detect missing redirects.
    result = run_command(["bash", "-c", 'printf unexpected-input | "$1"', "_", str(SCRIPT)], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "herdr 0.9.3\n"
    assert len(curl_calls(env)) == (1 if probe == "installed" else 2)
    assert not list(Path(env["TOOLS_BIN"]).glob(".herdr.*"))


@pytest.mark.parametrize("field", ["tag", "digest"])
def test_jq_failure_rejects_even_valid_stdout(installer_env: dict[str, str], field: str) -> None:
    env = installer_env
    original = existing_binary(env, "herdr 0.9.3")
    fake_bin = Path(env["PATH"].split(os.pathsep)[0])
    expected_hash = hashlib.sha256(Path(env["DJINN_TEST_PAYLOAD"]).read_bytes()).hexdigest()
    env["DJINN_TEST_JQ_FIELD"] = field
    write_executable(
        fake_bin / "jq",
        f"""
        #!/bin/sh
        case "$2" in
            *tag_name*) field=tag; value=v0.9.3 ;;
            *) field=digest; value=sha256:{expected_hash} ;;
        esac
        if [ "$field" = "$DJINN_TEST_JQ_FIELD" ]; then
            cat >/dev/null
            echo "$value"
            exit 1
        fi
        exec /usr/bin/jq "$@"
        """,
    )
    result = run_command([str(SCRIPT)], env)
    assert result.returncode == 1
    assert result.stdout == ""
    expected = (
        f"herdr: Invalid release JSON from {API_URL}, skipping"
        if field == "tag"
        else "herdr: Invalid SHA-256 digest for release 'v0.9.3', skipping"
    )
    assert result.stderr.splitlines()[-1] == expected
    assert curl_calls(env) == [[*GUARDS, "--max-time", "30", API_URL]]
    assert_preserved(env, original)


def test_installer_mode_and_default_verifier() -> None:
    assert SCRIPT.stat().st_mode & 0o777 == 0o755
    assert "# djinn-verify:" not in SCRIPT.read_text(encoding="utf-8")
