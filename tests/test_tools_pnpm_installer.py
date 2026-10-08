"""Exercise the pnpm installer and generated wrappers without network access."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "installers" / "pnpm.sh"
VERSION = "pnpm-fake 11.26.0"
NPM_MESSAGE = "added 1 package in 1s"


def write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def installer_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    node_bin = tmp_path / "node installation" / "bin"
    home.mkdir()
    node_bin.mkdir(parents=True)
    write_executable(
        node_bin / "npm",
        """
        #!/bin/sh
        printf '%s\\n' "$@" > "$DJINN_TEST_NPM_ARGS"
        usage() {
            echo 'npm error code EUSAGE' >&2
            echo 'npm error Usage: npm install --global --prefix <dir> corepack@latest' >&2
            exit 1
        }
        [ "${1:-}" = install ] || usage
        shift
        global= package= prefix=
        while [ "$#" -gt 0 ]; do
            case "$1" in
                --global) global=yes ;;
                --prefix)
                    [ "$#" -ge 2 ] || usage
                    prefix=$2
                    shift
                    ;;
                corepack@latest) package=yes ;;
                --no-fund|--no-audit) ;;
                *) usage ;;
            esac
            shift
        done
        [ "$global" = yes ] && [ "$package" = yes ] && [ -n "$prefix" ] || usage
        if [ "${DJINN_TEST_NPM_FAIL:-}" = yes ]; then
            echo 'npm error code E404' >&2
            echo 'npm error 404 Not Found - GET https://registry.npmjs.org/corepack - Not found' >&2
            exit 1
        fi
        mkdir -p "$prefix/bin" "$prefix/lib/node_modules/corepack/dist"
        cat > "$prefix/lib/node_modules/corepack/dist/corepack.js" <<'EOF'
        #!/bin/sh
        describe() {
            printf 'ARGS='
            for arg do
                printf '<%s> ' "$arg"
            done
            printf 'HOME_DIR=%s\\n' "$COREPACK_HOME"
        }
        describe "$@" >> "$DJINN_TEST_COREPACK_CALLS"
        if [ "${2:-}" = pid ]; then
            printf 'PID=%s\\n' "$$"
            exit 0
        fi
        if [ "$#" -eq 2 ] && [ "$1" = pnpm ] && [ "$2" = --version ]; then
            echo 'pnpm-fake 11.26.0'
        else
            describe "$@"
        fi
        EOF
        chmod +x "$prefix/lib/node_modules/corepack/dist/corepack.js"
        ln -sf ../lib/node_modules/corepack/dist/corepack.js "$prefix/bin/corepack"
        echo 'added 1 package in 1s'
        """,
    )
    write_executable(
        node_bin / "corepack",
        """
        #!/bin/sh
        echo DECOY
        touch "$DJINN_TEST_DECOY_CALLED"
        exit 3
        """,
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "TOOLS_DIR": str(tmp_path / "tools volume"),
        "TOOLS_BIN": str(tmp_path / "wrappers bin"),
        "PATH": f"{node_bin}:{os.environ['PATH']}",
        "DJINN_TEST_NPM_ARGS": str(tmp_path / "npm-args"),
        "DJINN_TEST_COREPACK_CALLS": str(tmp_path / "corepack-calls"),
        "DJINN_TEST_DECOY_CALLED": str(tmp_path / "decoy-called"),
    }
    env.pop("COREPACK_HOME", None)
    env.pop("DJINN_TEST_NPM_FAIL", None)
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
    )


def install(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = run_command([str(SCRIPT)], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not Path(env["DJINN_TEST_DECOY_CALLED"]).exists(), result.stdout
    return result


def run_wrapper(
    env: dict[str, str], name: str, *args: str
) -> subprocess.CompletedProcess[str]:
    return run_command([str(Path(env["TOOLS_BIN"]) / name), *args], env)


def test_wrappers_are_executable_and_independent_of_node(installer_env: dict[str, str]) -> None:
    install(installer_env)
    for name in ("pnpm", "pnpx"):
        wrapper = Path(installer_env["TOOLS_BIN"]) / name
        assert wrapper.is_file()
        assert os.access(wrapper, os.X_OK)
        content = wrapper.read_text(encoding="utf-8")
        assert content.splitlines()[0] == "#!/bin/sh"
        assert str(Path(installer_env["PATH"].split(os.pathsep)[0])) not in content
    corepack = Path(installer_env["TOOLS_DIR"]) / "corepack" / "bin" / "corepack"
    assert corepack.is_symlink()
    assert (Path(installer_env["TOOLS_DIR"]) / "corepack-home").is_dir()
    assert os.access(SCRIPT, os.X_OK)
    assert "# djinn-verify:" not in SCRIPT.read_text(encoding="utf-8")


@pytest.mark.parametrize("empty", [False, True], ids=["unset", "empty"])
def test_corepack_home_defaults_to_volume(installer_env: dict[str, str], empty: bool) -> None:
    if empty:
        installer_env["COREPACK_HOME"] = ""
    install(installer_env)
    result = run_wrapper(installer_env, "pnpm", "help")
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        f"ARGS=<pnpm> <help> HOME_DIR={installer_env['TOOLS_DIR']}/corepack-home\n"
    )


def test_corepack_home_override_wins(installer_env: dict[str, str], tmp_path: Path) -> None:
    install(installer_env)
    installer_env["COREPACK_HOME"] = str(tmp_path / "custom cache")
    result = run_wrapper(installer_env, "pnpm", "help")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"ARGS=<pnpm> <help> HOME_DIR={installer_env['COREPACK_HOME']}\n"


def test_pnpx_dispatch(installer_env: dict[str, str]) -> None:
    install(installer_env)
    result = run_wrapper(installer_env, "pnpx", "example-package")
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        f"ARGS=<pnpx> <example-package> HOME_DIR={installer_env['TOOLS_DIR']}/corepack-home\n"
    )


def test_wrapper_preserves_spaced_arguments(installer_env: dict[str, str]) -> None:
    install(installer_env)
    result = run_wrapper(installer_env, "pnpm", "run", "two words", "", "*", "last arg")
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "ARGS=<pnpm> <run> <two words> <> <*> <last arg> "
        f"HOME_DIR={installer_env['TOOLS_DIR']}/corepack-home\n"
    )


def test_installer_output_contract(installer_env: dict[str, str]) -> None:
    result = install(installer_env)
    assert result.stdout.splitlines()[-1:] == [VERSION]
    assert NPM_MESSAGE in result.stderr
    assert NPM_MESSAGE not in result.stdout
    assert Path(installer_env["DJINN_TEST_COREPACK_CALLS"]).read_text(encoding="utf-8") == (
        f"ARGS=<pnpm> <--version> HOME_DIR={installer_env['TOOLS_DIR']}/corepack-home\n"
    )


def test_wrapper_replaces_process(installer_env: dict[str, str]) -> None:
    install(installer_env)
    with subprocess.Popen(
        [str(Path(installer_env["TOOLS_BIN"]) / "pnpm"), "pid"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=installer_env,
        stdin=subprocess.DEVNULL,
        text=True,
    ) as process:
        stdout, stderr = process.communicate()
        assert process.returncode == 0, stderr
        assert stdout == f"PID={process.pid}\n"


def test_wrappers_are_staged_in_destination(installer_env: dict[str, str]) -> None:
    node_bin = Path(installer_env["PATH"].split(os.pathsep)[0])
    templates = node_bin / "mktemp-templates"
    installer_env["DJINN_TEST_MKTEMP_TEMPLATES"] = str(templates)
    write_executable(
        node_bin / "mktemp",
        """
        #!/bin/sh
        printf '%s\\n' "$@" >> "$DJINN_TEST_MKTEMP_TEMPLATES"
        exec /usr/bin/mktemp "$@"
        """,
    )
    install(installer_env)
    staged_templates = templates.read_text(encoding="utf-8").splitlines()
    assert len(staged_templates) == 2
    assert all(
        Path(template).parent == Path(installer_env["TOOLS_BIN"])
        for template in staged_templates
    )
    assert not list(Path(installer_env["TOOLS_BIN"]).glob(".*.??????"))


def test_default_tools_dir_is_under_home(installer_env: dict[str, str]) -> None:
    installer_env.pop("TOOLS_DIR")
    install(installer_env)
    corepack = Path(installer_env["HOME"]) / ".cache/djinn-tools/corepack/bin/corepack"
    assert corepack.is_symlink()
    result = run_wrapper(installer_env, "pnpm", "help")
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        f"ARGS=<pnpm> <help> HOME_DIR={installer_env['HOME']}/.cache/djinn-tools/corepack-home\n"
    )


def test_default_tools_bin_is_under_tools_dir(installer_env: dict[str, str]) -> None:
    installer_env.pop("TOOLS_BIN")
    install(installer_env)
    installer_env["TOOLS_BIN"] = f"{installer_env['TOOLS_DIR']}/bin"
    result = run_wrapper(installer_env, "pnpx", "help")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("ARGS=<pnpx> <help> ")


@pytest.mark.parametrize("existing", [False, True], ids=["absent", "existing"])
def test_npm_failure_preserves_wrappers(installer_env: dict[str, str], existing: bool) -> None:
    wrappers = Path(installer_env["TOOLS_BIN"])
    if existing:
        wrappers.mkdir()
        for name in ("pnpm", "pnpx"):
            write_executable(wrappers / name, "#!/bin/sh\necho existing-wrapper\n")
    installer_env["DJINN_TEST_NPM_FAIL"] = "yes"
    result = run_command([str(SCRIPT)], installer_env)
    assert result.returncode != 0
    assert "npm error code E404" in result.stderr
    assert (
        "npm error 404 Not Found - GET https://registry.npmjs.org/corepack - Not found"
        in result.stderr
    )
    for name in ("pnpm", "pnpx"):
        wrapper = wrappers / name
        if existing:
            assert wrapper.read_text(encoding="utf-8") == "#!/bin/sh\necho existing-wrapper\n"
            assert os.access(wrapper, os.X_OK)
        else:
            assert not wrapper.exists()


def test_staging_failure_preserves_existing_wrappers(installer_env: dict[str, str]) -> None:
    wrappers = Path(installer_env["TOOLS_BIN"])
    wrappers.mkdir()
    for name in ("pnpm", "pnpx"):
        write_executable(wrappers / name, "#!/bin/sh\necho existing-wrapper\n")
    node_bin = Path(installer_env["PATH"].split(os.pathsep)[0])
    write_executable(
        node_bin / "chmod",
        """
        #!/bin/sh
        case "$2" in
            */.pnpm.*)
                echo "chmod: changing permissions of '$2': Operation not permitted" >&2
                exit 1
                ;;
        esac
        exec /bin/chmod "$@"
        """,
    )
    result = run_command([str(SCRIPT)], installer_env)
    assert result.returncode != 0
    assert "Operation not permitted" in result.stderr
    for name in ("pnpm", "pnpx"):
        wrapper = wrappers / name
        assert wrapper.read_text(encoding="utf-8") == "#!/bin/sh\necho existing-wrapper\n"
        assert os.access(wrapper, os.X_OK)
