"""Regression tests for the optional tools installer cache checks."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "install.sh"


def write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def write_install_fixture(tmp_path: Path, tools: list[str]) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    cache_dir = tmp_path / "cache"
    installers_dir = tmp_path / "installers"
    tools_file = tmp_path / "tools.txt"

    home.mkdir()
    (cache_dir / "bin").mkdir(parents=True)
    (cache_dir / "lib").mkdir()
    installers_dir.mkdir()
    tools_file.write_text("\n".join(tools) + "\n", encoding="utf-8")

    build_timestamp = "test-build\n"
    (home / ".build-timestamp").write_text(build_timestamp, encoding="utf-8")
    (cache_dir / ".build-timestamp").write_text(build_timestamp, encoding="utf-8")

    return home, cache_dir, installers_dir


def run_install(
    home: Path,
    cache_dir: Path,
    installers_dir: Path,
    tools_file: Path,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "HOME": str(home),
        "TOOLS_FILE": str(tools_file),
        "CACHE_DIR": str(cache_dir),
        "INSTALLERS_DIR": str(installers_dir),
        "OUTPUT_LIB": str(home / "missing-output-lib.sh"),
        "NO_COLOR": "1",
    }
    env.pop("DJINN_FORCE_UI_COLOR", None)

    return subprocess.run(
        ["bash", str(SCRIPT)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


def test_verify_override_skips_cached_tool_with_nonmatching_binary_name(
    tmp_path: Path,
) -> None:
    tools_file = tmp_path / "tools.txt"
    home, cache_dir, installers_dir = write_install_fixture(tmp_path, ["azure-cli"])
    (cache_dir / "azure-cli.installed").write_text("cached\n", encoding="utf-8")

    write_executable(
        cache_dir / "bin" / "az",
        """
        #!/bin/bash
        if [[ "${1:-}" == "version" ]]; then
            echo "azure-cli 1.0"
            exit 0
        fi
        exit 2
        """,
    )
    write_executable(
        installers_dir / "azure-cli.sh",
        """
        #!/bin/bash
        # djinn-verify: az version
        touch "$TOOLS_DIR/azure-cli-reinstalled"
        echo "azure-cli installed"
        """,
    )

    result = run_install(home, cache_dir, installers_dir, tools_file)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "[ok] [tools] 1 tool(s) already installed (cached)" in result.stderr
    assert "[tools] Installing azure-cli" not in result.stderr
    assert not (cache_dir / "azure-cli-reinstalled").exists()


def test_fallback_verify_skips_cached_conforming_tool(tmp_path: Path) -> None:
    tools_file = tmp_path / "tools.txt"
    home, cache_dir, installers_dir = write_install_fixture(tmp_path, ["bun"])
    (cache_dir / "bun.installed").write_text("cached\n", encoding="utf-8")

    write_executable(
        cache_dir / "bin" / "bun",
        """
        #!/bin/bash
        if [[ "${1:-}" == "--version" ]]; then
            echo "bun 1.0"
            exit 0
        fi
        exit 2
        """,
    )
    write_executable(
        installers_dir / "bun.sh",
        """
        #!/bin/bash
        touch "$TOOLS_DIR/bun-reinstalled"
        echo "bun installed"
        """,
    )

    result = run_install(home, cache_dir, installers_dir, tools_file)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "[ok] [tools] 1 tool(s) already installed (cached)" in result.stderr
    assert "[tools] Installing bun" not in result.stderr
    assert not (cache_dir / "bun-reinstalled").exists()


def test_failed_verify_reinstalls_and_warns(tmp_path: Path) -> None:
    tools_file = tmp_path / "tools.txt"
    home, cache_dir, installers_dir = write_install_fixture(tmp_path, ["broken-tool"])
    (cache_dir / "broken-tool.installed").write_text("cached\n", encoding="utf-8")

    write_executable(
        installers_dir / "broken-tool.sh",
        """
        #!/bin/bash
        cat > "$TOOLS_BIN/broken-tool" <<'EOF'
        #!/bin/bash
        echo "broken-tool 1.0"
        EOF
        chmod +x "$TOOLS_BIN/broken-tool"
        touch "$TOOLS_DIR/broken-tool-reinstalled"
        echo "broken-tool 1.0"
        """,
    )

    result = run_install(home, cache_dir, installers_dir, tools_file)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "[warn] [tools] Cache check failed for broken-tool" in result.stderr
    assert "verify command failed:" in result.stderr
    assert "broken-tool --version" in result.stderr
    assert "[info] [tools] Installing broken-tool" in result.stderr
    assert "[ok] [tools] broken-tool installed (broken-tool 1.0)" in result.stderr
    assert (cache_dir / "broken-tool-reinstalled").exists()
