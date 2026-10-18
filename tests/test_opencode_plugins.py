"""Exercise the opt-in plugin definitions against the v2 context contract."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ROOT / "templates/opencode/plugins"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is needed to exercise plugins")


def run_js(source: str, *args: str) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    return subprocess.run(
        [NODE, "--input-type=module", "-e", source, *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize(
    "command",
    [
        "sudo id",
        "git -C /tmp push --force-with-lease",
        "git reset --hard",
        "rm -rf ./",
        "chmod -R 777 project",
    ],
)
def test_security_uses_v2_tool_input_to_block_commands(command: str) -> None:
    result = run_js(
        """
        import assert from 'node:assert/strict';
        import { pathToFileURL } from 'node:url';
        const { default: plugin } = await import(pathToFileURL(process.argv[1]));
        assert.equal(typeof plugin.id, 'string');
        let before;
        await plugin.setup({ tool: { hook: async (name, callback) => {
          assert.equal(name, 'execute.before'); before = callback;
        } } });
        await assert.rejects(() => before({
          tool: 'shell', input: { command: process.argv[2] },
        }));
        await before({tool: 'shell', input: {command: 'git status --short'}});
        """,
        str(PLUGINS / "security-reminder.js"),
        command,
    )
    assert result.returncode == 0, result.stderr


def test_v2_editing_inputs_reach_existing_reminder_policy() -> None:
    result = run_js(
        r"""
        import assert from 'node:assert/strict';
        import { readFile } from 'node:fs/promises';
        import { pathToFileURL } from 'node:url';
        const { default: plugin } = await import(pathToFileURL(process.argv[1]));
        const source = await readFile(process.argv[1], 'utf8');
        const content = JSON.parse(source.match(/snippets: (\[[^\]]+\])/)[1])[0];
        let before;
        await plugin.setup({tool: {hook: async (_, callback) => {before = callback}}});
        const warnings = [];
        console.warn = message => warnings.push(message);
        await before({tool: 'write', input: {path: 'test.js', content}});
        await before({tool: 'edit', input: {path: 'test.js', newString: content}});
        await before({tool: 'patch', input: {patchText: content}});
        assert.equal(warnings.length, 3);
        """,
        str(PLUGINS / "security-reminder.js"),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("dirty", [False, True])
def test_session_subscription_checks_repo_and_cleans_up(tmp_path: Path, dirty: bool) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    if dirty:
        (tmp_path / "pending.txt").write_text("uncommitted\n")
    result = run_js(
        """
        import assert from 'node:assert/strict';
        import { pathToFileURL } from 'node:url';
        const { default: plugin } = await import(pathToFileURL(process.argv[1]));
        let signal, finished;
        const done = new Promise(resolve => {finished = resolve});
        const warnings = [];
        console.warn = message => warnings.push(message);
        const cleanup = plugin.setup({
          location: {directory: '/does-not-exist'},
          event: {async *subscribe(options) {
            signal = options.signal;
            yield {type: 'irrelevant'};
            yield {type: 'session.created', data: {location: {directory: process.argv[2]}}};
            finished();
          }},
        });
        await done;
        await cleanup();
        assert.equal(signal.aborted, true);
        assert.equal(warnings.length, Number(process.argv[3]));
        """,
        str(PLUGINS / "session-start-status.js"),
        str(tmp_path),
        str(int(dirty)),
    )
    assert result.returncode == 0, result.stderr
