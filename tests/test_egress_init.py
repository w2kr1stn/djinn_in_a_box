"""Tests for the allowlist run's container side: the root wrapper and the entrypoint guard.

The wrapper (``scripts/egress-init.sh``) and the entrypoint run here as temporary
copies in which every privileged path is substituted; the fake ``iptables``,
``setpriv`` and ``sudo`` record what they are asked to do. The allowlist mode of
``init-firewall.sh`` itself is covered in ``test_init_firewall.py``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_init_firewall import (
    ROOT,
    SYSTEM_PATH,
    Fakes,
    firewall_copy,
    substitute,
)
from test_output_lib import _entrypoint_environment

WRAPPER = ROOT / "scripts" / "egress-init.sh"
ENTRYPOINT = ROOT / "scripts" / "entrypoint.sh"
TOKENS = "10.0.0.0/8 203.0.113.10/32:443"
CAPBND_CLEARED = "00000000a80405fb"

requires_zsh = pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")

SHIM = """#!{python}
import json, os, sys
with open({log!r}, "a") as handle:
    handle.write(json.dumps({{"tool": {tool!r}, "argv": sys.argv[1:]}}) + "\\n")
os.execv({real!r}, [{real!r}, *sys.argv[1:]])
"""

ENV_KEYS = ("HOME", "USER", "LOGNAME", "PATH", "DJINN_EGRESS_ALLOW")

SETPRIV = """#!{python}
import json, os, sys
mode = {mode!r}
record = {{
    "argv": sys.argv[1:],
    "pid": os.getpid(),
    "env": {{key: os.environ.get(key) for key in {keys!r}}},
}}
with open({log!r}, "a") as handle:
    handle.write(json.dumps(record) + "\\n")
if mode == "fail":
    sys.exit(7)
if mode == "exec":
    rest = sys.argv[sys.argv.index("--") + 1 :]
    os.execv(rest[0], rest)
"""


def wrapper_assignments(source: str) -> None:
    assert source.startswith("#!/bin/bash -p\n")
    for name, value in {
        "root_uid": "0",
        "env_bin": "/usr/bin/env",
        "bash_bin": "/bin/bash",
        "firewall_script": "/usr/local/bin/init-firewall.sh",
        "setpriv_bin": "/usr/bin/setpriv",
        "entrypoint_path": "/home/dev/entrypoint.sh",
        "dev_home": "/home/dev",
    }.items():
        assert source.count(f"{name}='{value}'") == 1, name
    statements = [
        line
        for line in source.splitlines()[1:]
        if line.strip() and not line.lstrip().startswith("#")
    ]
    # PATH is replaced before anything else can run.
    assert statements[0] == "saved_path=${PATH-}"
    assert statements[1] == f"PATH='{SYSTEM_PATH}'"


def wrapper_command_text(source: str) -> str:
    lines = []
    for line in source.splitlines()[1:]:
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        stripped = re.sub(r'"\$\w+_bin"', "BIN", stripped)
        lines.append(re.sub(r'"[^"]*"|\'[^\']*\'', '""', stripped))
    return "\n".join(lines)


def assert_wrapper_dispatch(source: str) -> None:
    wrapper_assignments(source)
    text = wrapper_command_text(source)
    assert not re.search(r"(?<![\w$])(env|bash|setpriv|id|iptables|ip6tables|sudo)\b", text)
    assert not re.search(r"(^|[\s;&|(])(source|eval)\s", text)
    assert not re.search(r"(^|[;&|(]\s*)\.\s", text)


class Rig:
    """A wrapper copy with recording stand-ins for env, bash, the firewall and setpriv."""

    def __init__(
        self, base: Path, *, setpriv_mode: str = "record", firewall_body: str = "exit 0"
    ) -> None:
        self.base = base
        base.mkdir(parents=True, exist_ok=True)
        self.shim_log = base / "shim.log"
        self.setpriv_log = base / "setpriv.log"
        self.env_dump = base / "firewall-env.txt"
        self.firewall_args = base / "firewall-args.txt"
        self.entrypoint_marker = base / "entrypoint-ran"
        self.env_shim = self._shim("env", "/usr/bin/env")
        self.bash_shim = self._shim("bash", "/bin/bash")
        self.firewall = base / "firewall.sh"
        self.firewall.write_text(
            f"printf '%s\\n' \"$@\" > {shlex.quote(str(self.firewall_args))}\n"
            f"/usr/bin/env > {shlex.quote(str(self.env_dump))}\n{firewall_body}\n"
        )
        self.setpriv = base / "setpriv"
        self.setpriv.write_text(
            SETPRIV.format(
                python=sys.executable, mode=setpriv_mode, log=str(self.setpriv_log), keys=ENV_KEYS
            )
        )
        self.setpriv.chmod(0o755)
        self.entrypoint = base / "entrypoint.sh"
        self.entrypoint.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(self.entrypoint_marker))}\n")
        self.entrypoint.chmod(0o755)

    def _shim(self, tool: str, real: str) -> Path:
        path = self.base / f"{tool}-shim"
        path.write_text(
            SHIM.format(python=sys.executable, log=str(self.shim_log), tool=tool, real=real)
        )
        path.chmod(0o755)
        return path

    def copy(self, *, root_uid: str | None = None, dev_home: Path | None = None) -> Path:
        source = WRAPPER.read_text()
        assert_wrapper_dispatch(source)
        source = substitute(source, "root_uid", "0", root_uid or str(os.geteuid()))
        source = substitute(source, "env_bin", "/usr/bin/env", self.env_shim)
        source = substitute(source, "bash_bin", "/bin/bash", self.bash_shim)
        source = substitute(
            source, "firewall_script", "/usr/local/bin/init-firewall.sh", self.firewall
        )
        source = substitute(source, "setpriv_bin", "/usr/bin/setpriv", self.setpriv)
        source = substitute(source, "entrypoint_path", "/home/dev/entrypoint.sh", self.entrypoint)
        source = substitute(source, "dev_home", "/home/dev", dev_home or "/home/dev")
        script = self.base / "djinn-egress-init"
        script.write_text(source)
        script.chmod(0o755)
        return script

    def shim_calls(self) -> list[dict[str, object]]:
        if not self.shim_log.exists():
            return []
        return [json.loads(line) for line in self.shim_log.read_text().splitlines()]

    def setpriv_calls(self) -> list[dict[str, object]]:
        if not self.setpriv_log.exists():
            return []
        return [json.loads(line) for line in self.setpriv_log.read_text().splitlines()]


def run_wrapper(
    script: Path, args: list[str], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), *args],
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
        env=env,
    )


def base_env(**extra: str) -> dict[str, str]:
    return {"PATH": "/original/bin:/usr/bin:/bin", "DJINN_EGRESS_ALLOW": TOKENS, **extra}


def test_wrapper_is_a_bash_privileged_mode_script_with_fixed_paths() -> None:
    assert_wrapper_dispatch(WRAPPER.read_text())
    assert os.access(WRAPPER, os.X_OK)


def test_wrapper_applies_firewall_then_execs_setpriv_with_exact_argv(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    result = run_wrapper(
        rig.copy(dev_home=Path("/home/dev")),
        ["-c", "echo 'a b'", "", "--x"],
        base_env(HOME="/root", USER="root", LOGNAME="root", EXTRA="kept"),
    )

    assert result.returncode == 0, result.stderr
    (call,) = rig.setpriv_calls()
    assert call["argv"] == [
        "--bounding-set=-net_admin,-net_raw",
        "--inh-caps=-all",
        "--reuid=1000",
        "--regid=1000",
        "--init-groups",
        "--",
        str(rig.entrypoint),
        "-c",
        "echo 'a b'",
        "",
        "--x",
    ]
    assert call["env"] == {
        "HOME": "/home/dev",
        "USER": "dev",
        "LOGNAME": "dev",
        "PATH": "/original/bin:/usr/bin:/bin",
        "DJINN_EGRESS_ALLOW": TOKENS,
    }
    assert rig.firewall_args.read_text().splitlines() == ["--allow", TOKENS]


def test_wrapper_starts_firewall_in_clean_env_under_privileged_bash(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    result = run_wrapper(
        rig.copy(),
        [],
        base_env(HOME="/root", LD_PRELOAD="/nonexistent.so", BASH_ENV="/nonexistent", FOO="bar"),
    )

    assert result.returncode == 0, result.stderr
    env_call, bash_call = rig.shim_calls()
    assert env_call == {
        "tool": "env",
        "argv": [
            "-i",
            f"PATH={SYSTEM_PATH}",
            str(rig.bash_shim),
            "-p",
            str(rig.firewall),
            "--allow",
            TOKENS,
        ],
    }
    assert bash_call == {"tool": "bash", "argv": ["-p", str(rig.firewall), "--allow", TOKENS]}
    child_env = dict(
        line.split("=", 1) for line in rig.env_dump.read_text().splitlines() if "=" in line
    )
    assert child_env["PATH"] == SYSTEM_PATH
    assert not {"LD_PRELOAD", "BASH_ENV", "FOO", "HOME", "DJINN_EGRESS_ALLOW"} & child_env.keys()


@pytest.mark.parametrize("missing", ["unset", "empty"])
def test_wrapper_refuses_without_the_allowlist_variable(tmp_path: Path, missing: str) -> None:
    rig = Rig(tmp_path)
    env = base_env()
    del env["DJINN_EGRESS_ALLOW"]
    if missing == "empty":
        env["DJINN_EGRESS_ALLOW"] = ""
    result = run_wrapper(rig.copy(), [], env)

    assert result.returncode != 0
    assert "DJINN_EGRESS_ALLOW" in result.stderr
    assert rig.shim_calls() == []
    assert rig.setpriv_calls() == []


def test_wrapper_refuses_when_not_root(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    result = run_wrapper(rig.copy(root_uid="99999"), [], base_env())

    assert result.returncode != 0
    assert "root is required" in result.stderr
    assert rig.shim_calls() == []
    assert rig.setpriv_calls() == []


def test_wrapper_does_not_start_when_the_firewall_fails(tmp_path: Path) -> None:
    rig = Rig(tmp_path, firewall_body="exit 3")
    result = run_wrapper(rig.copy(), [], base_env())

    assert result.returncode != 0
    assert "firewall setup failed" in result.stderr
    assert rig.setpriv_calls() == []
    assert not rig.entrypoint_marker.exists()


def test_wrapper_propagates_a_setpriv_failure(tmp_path: Path) -> None:
    rig = Rig(tmp_path, setpriv_mode="fail")
    result = run_wrapper(rig.copy(), [], base_env())

    assert result.returncode == 7
    assert len(rig.setpriv_calls()) == 1


# --- shadowing and start-up hooks: the root phase reads only image content ---


def test_shadowed_tools_and_startup_hooks_never_run_in_the_privileged_phase(tmp_path: Path) -> None:
    fakes = Fakes(tmp_path / "fakes")
    rig = Rig(tmp_path / "rig", setpriv_mode="record")
    (tmp_path / "fw").mkdir()
    firewall, library_marker, _ = firewall_copy(tmp_path / "fw", fakes)
    source = rig.copy().read_text()
    source = source.replace(shlex.quote(str(rig.firewall)), shlex.quote(str(firewall)))
    wrapper = tmp_path / "wrapper"
    wrapper.write_text(source)
    wrapper.chmod(0o755)

    marker = tmp_path / "shadow-ran"
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    for name in ("iptables", "ip6tables", "setpriv", "env", "bash", "getent", "id", "sudo"):
        fake = shadow / name
        fake.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 0\n")
        fake.chmod(0o755)
    hook = tmp_path / "hook.sh"
    hook.write_text(f"touch {shlex.quote(str(marker))}\n")
    exported = tmp_path / "exported-function"
    result = run_wrapper(
        wrapper,
        [],
        {
            "PATH": f"{shadow}:/usr/bin:/bin",
            "DJINN_EGRESS_ALLOW": TOKENS,
            "BASH_ENV": str(hook),
            "ENV": str(hook),
            "BASH_FUNC_touch%%": f"() {{ command touch {exported}; }}",
        },
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert not exported.exists()
    assert not library_marker.exists()
    assert len(fakes.calls("iptables")) == 8  # the substituted absolute fakes did the work
    (call,) = rig.setpriv_calls()
    assert call["env"]["PATH"] == f"{shadow}:/usr/bin:/bin"  # type: ignore[index]


# --- entrypoint guard ---


def status_file(base: Path, capbnd: str | None, name: str = "status") -> Path:
    path = base / name
    lines = ["Name:\tzsh", "Umask:\t0022", "CapInh:\t0000000000000000"]
    if capbnd is not None:
        lines.append(f"CapBnd:\t{capbnd}")
    lines.append("CapAmb:\t0000000000000000")
    path.write_text("\n".join(lines) + "\n")
    return path


def entrypoint_copy(base: Path, status: Path) -> Path:
    source = ENTRYPOINT.read_text()
    old = "egress_status_path='/proc/self/status'"
    assert source.count(old) == 1
    script = base / "entrypoint.sh"
    script.write_text(source.replace(old, f"egress_status_path={shlex.quote(str(status))}"))
    script.chmod(0o755)
    return script


POISON_LIB = """print -r -- POISONED-LIBRARY-RAN >&2
touch {marker}
source {real}
"""


def guard_env(tmp_path: Path, **extra: str) -> tuple[dict[str, str], Path, Path]:
    env = _entrypoint_environment(tmp_path)
    sudo_log = tmp_path / "sudo.log"
    bindir = tmp_path / "guardbin"
    bindir.mkdir()
    sudo = bindir / "sudo"
    sudo.write_text(f'#!/bin/sh\necho "$@" >> {shlex.quote(str(sudo_log))}\n')
    sudo.chmod(0o755)
    marker = tmp_path / "library-ran"
    library = tmp_path / "poison-lib.sh"
    library.write_text(
        POISON_LIB.format(
            marker=shlex.quote(str(marker)), real=shlex.quote(str(ROOT / "scripts/output-lib.sh"))
        )
    )
    env.update(
        {
            "PATH": f"{bindir}:{env['PATH']}",
            "ENABLE_FIREWALL": "true",
            "OUTPUT_LIB": str(library),
            "DJINN_EGRESS_ALLOW": TOKENS,
            **extra,
        }
    )
    return env, sudo_log, marker


def run_entrypoint(
    script: Path, env: dict[str, str], agent_marker: Path
) -> subprocess.CompletedProcess[str]:
    zsh = shutil.which("zsh")
    assert zsh is not None
    return subprocess.run(
        [zsh, str(script), "-c", f"touch {shlex.quote(str(agent_marker))}"],
        check=False,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        timeout=60,
        env=env,
        text=True,
    )


@requires_zsh
def test_entrypoint_locks_skips_default_firewall_and_reports_summary(tmp_path: Path) -> None:
    env, sudo_log, library_marker = guard_env(tmp_path)
    agent_marker = tmp_path / "agent-ran"
    script = entrypoint_copy(tmp_path, status_file(tmp_path, CAPBND_CLEARED))
    result = run_entrypoint(script, env, agent_marker)

    assert result.returncode == 0, result.stderr
    lines = result.stderr.splitlines()
    assert lines[0] == "djinn-egress: locked 2"
    assert [line for line in lines if line.startswith("djinn-egress:")] == [
        "djinn-egress: locked 2"
    ]
    assert "Initializing firewall" not in result.stderr
    assert not sudo_log.exists() or "init-firewall" not in sudo_log.read_text()
    assert "Firewall:     Allowlist (locked)" in result.stderr
    assert "Firewall:     Enabled" not in result.stderr
    assert agent_marker.exists()
    # The guard ran before the library; the library still loads afterwards.
    assert library_marker.exists()
    assert lines.index("POISONED-LIBRARY-RAN") > 0


@requires_zsh
@pytest.mark.parametrize("count", [1, 3])
def test_entrypoint_reports_the_token_count(tmp_path: Path, count: int) -> None:
    tokens = " ".join(f"10.0.0.{index}/32" for index in range(count))
    env, _, _ = guard_env(tmp_path, DJINN_EGRESS_ALLOW=tokens)
    script = entrypoint_copy(tmp_path, status_file(tmp_path, CAPBND_CLEARED))
    result = run_entrypoint(script, env, tmp_path / "agent-ran")

    assert result.returncode == 0, result.stderr
    assert result.stderr.splitlines()[0] == f"djinn-egress: locked {count}"


@requires_zsh
@pytest.mark.parametrize(
    "capbnd",
    [
        "0000000000001000",
        "0000000000002000",
        "0000000000003000",
        "000001ffffffffff",
        "ffffffffffffffff",
        None,
        "",
        "zzzzzzzzzzzzzzzz",
        "00000000a80405f",
        "00000000a80405fb0",
        "0x00000000a80405fb",
    ],
)
def test_entrypoint_refuses_a_bounding_set_that_keeps_net_caps_or_is_unreadable(
    tmp_path: Path, capbnd: str | None
) -> None:
    env, sudo_log, library_marker = guard_env(tmp_path)
    agent_marker = tmp_path / "agent-ran"
    script = entrypoint_copy(tmp_path, status_file(tmp_path, capbnd))
    result = run_entrypoint(script, env, agent_marker)

    assert result.returncode == 1
    assert "djinn-egress:" not in result.stderr
    assert "[err] egress allowlist" in result.stderr
    assert "POISONED-LIBRARY-RAN" not in result.stderr
    assert not library_marker.exists()
    assert not agent_marker.exists()
    assert not sudo_log.exists()


@requires_zsh
def test_entrypoint_refuses_when_the_status_file_is_missing(tmp_path: Path) -> None:
    env, _, library_marker = guard_env(tmp_path)
    agent_marker = tmp_path / "agent-ran"
    script = entrypoint_copy(tmp_path, tmp_path / "does-not-exist")
    result = run_entrypoint(script, env, agent_marker)

    assert result.returncode == 1
    assert "djinn-egress:" not in result.stderr
    assert not agent_marker.exists()
    assert not library_marker.exists()


@requires_zsh
def test_entrypoint_without_allowlist_keeps_the_default_firewall_step(tmp_path: Path) -> None:
    env, sudo_log, _ = guard_env(tmp_path)
    del env["DJINN_EGRESS_ALLOW"]
    # The status file is not even consulted without the allowlist variable.
    script = entrypoint_copy(tmp_path, tmp_path / "does-not-exist")
    result = run_entrypoint(script, env, tmp_path / "agent-ran")

    assert result.returncode == 0, result.stderr
    assert "djinn-egress:" not in result.stderr
    assert "/usr/local/bin/init-firewall.sh" in sudo_log.read_text()
    assert "Firewall:     Enabled" in result.stderr
    assert "Allowlist" not in result.stderr


# --- composition ---


@requires_zsh
def test_composition_wrapper_firewall_setpriv_entrypoint(tmp_path: Path) -> None:
    env, sudo_log, _ = guard_env(tmp_path)
    fakes = Fakes(tmp_path / "fakes")
    (tmp_path / "fw").mkdir()
    firewall, _, _ = firewall_copy(tmp_path / "fw", fakes)
    rig = Rig(tmp_path / "rig", setpriv_mode="exec")
    entrypoint = entrypoint_copy(tmp_path, status_file(tmp_path, CAPBND_CLEARED))
    source = rig.copy(dev_home=Path(env["HOME"])).read_text()
    source = source.replace(shlex.quote(str(rig.firewall)), shlex.quote(str(firewall)))
    source = source.replace(shlex.quote(str(rig.entrypoint)), shlex.quote(str(entrypoint)))
    wrapper = tmp_path / "wrapper"
    wrapper.write_text(source)
    wrapper.chmod(0o755)
    agent_marker = tmp_path / "agent-ran"
    env["PATH"] = f"{tmp_path / 'guardbin'}:{os.environ['PATH']}"
    result = run_wrapper(wrapper, ["-c", f"touch {shlex.quote(str(agent_marker))}"], env)

    assert result.returncode == 0, result.stderr
    assert result.stderr.splitlines()[:3] == [
        "djinn-egress: allow 10.0.0.0/8 any",
        "djinn-egress: allow 203.0.113.10/32 tcp/443",
        "djinn-egress: locked 2",
    ]
    assert agent_marker.exists()
    assert not sudo_log.exists() or "init-firewall" not in sudo_log.read_text()
    (call,) = rig.setpriv_calls()
    assert call["argv"][-3:] == [str(entrypoint), "-c", f"touch {shlex.quote(str(agent_marker))}"]  # type: ignore[index]


@requires_zsh
def test_entrypoint_stays_the_process_that_handles_term(tmp_path: Path) -> None:
    rig = Rig(tmp_path, setpriv_mode="exec")
    pid_file = tmp_path / "entrypoint.pid"
    rig.entrypoint.write_text(
        f"#!/bin/sh\necho $$ > {shlex.quote(str(pid_file))}\nexec /bin/sleep 30\n"
    )
    process = subprocess.Popen(
        [str(rig.copy()), "x"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=base_env(),
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_file.exists()
        (call,) = rig.setpriv_calls()
        # wrapper -> setpriv -> entrypoint is one process: exec, not a child.
        assert int(call["pid"]) == process.pid  # type: ignore[arg-type]
        assert int(pid_file.read_text()) == process.pid
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=20)


def test_dockerfile_installs_the_wrapper_root_owned_before_the_user_switch() -> None:
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    copy = "COPY scripts/egress-init.sh /usr/local/bin/djinn-egress-init"
    chmod = "RUN chmod 0755 /usr/local/bin/djinn-egress-init"

    assert lines.count(copy) == 1  # no --chown: the image file stays root-owned
    assert lines.count(chmod) == 1
    user = next(index for index, line in enumerate(lines) if line.startswith("USER "))
    assert lines.index(copy) < lines.index(chmod) < user
