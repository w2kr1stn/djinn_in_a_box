#!/usr/bin/env python3
"""Apply the container's Codex daemon policy before interactive startup."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

VERSION_TIMEOUT = 30
UPDATE_TIMEOUT = 300
TERMINATE_GRACE = 0.25
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def object_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"non-finite JSON number: {value}")
    return number


def reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def read_object(path: Path) -> dict[str, object]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=object_members,
        parse_constant=reject_constant,
        parse_float=finite_float,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def admit_directory(path: Path) -> bool:
    """Shared gate for settings and native update's early PID/lock writes."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(mode):
        raise ValueError(f"{path}: expected a real directory, refusing symlinks")
    return True


def admit_file(path: Path) -> os.stat_result | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{path}: expected a regular file, refusing symlinks")
    return metadata


def updater_off(home: Path) -> None:
    directory = home / "app-server-daemon"
    if not admit_directory(directory):
        directory.mkdir(mode=0o700)
    settings = directory / "settings.json"
    metadata = admit_file(settings)
    document = read_object(settings) if metadata is not None else {}
    updater = document.setdefault("updater", {})
    if not isinstance(updater, dict):
        raise ValueError(f"{settings}: updater must be an object")
    if updater.get("autoUpdateEnabled") is False:
        return
    updater["autoUpdateEnabled"] = False
    content = json.dumps(document, indent=2, allow_nan=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".djinn-codex-policy-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), stat.S_IMODE(metadata.st_mode) if metadata else 0o644)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, settings)
        directory_fd = os.open(directory, DIRECTORY_FLAGS)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


def entry_size(name: str, parent_fd: int) -> int:
    """Count lstat sizes through directory descriptors, including link sizes."""
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    size = metadata.st_size
    if stat.S_ISDIR(metadata.st_mode):
        descriptor = os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)
        try:
            size += sum(entry_size(child, descriptor) for child in os.listdir(descriptor))
        finally:
            os.close(descriptor)
    return size


class Policy:
    def __init__(self, home: str | Path, codex: str):
        self.home = Path(os.path.abspath(home))
        self.codex = codex
        self.environment = dict(os.environ, CODEX_HOME=os.fspath(home))
        self.package = self.home / "packages" / "app-server-daemon"
        self.releases = self.package / "releases"
        self.current = self.package / "current"
        self.warned = False

    def warn(self, path: Path | str, reason: object) -> None:
        self.warned = True
        print(f"warning: Codex daemon: {path}: {reason}", file=sys.stderr)

    def selection(self, *, required: bool = False) -> Path | None:
        for directory in (self.home / "packages", self.package, self.releases):
            admit_directory(directory)
        try:
            self.current.lstat()
        except FileNotFoundError:
            if required:
                raise ValueError(f"{self.current}: selection missing after update") from None
            return None
        target = Path(os.path.abspath(self.current.parent / os.readlink(self.current)))
        if target.parent != self.releases:
            raise ValueError(f"{self.current}: target must be a direct child of {self.releases}")
        present = admit_directory(target)
        manifest = admit_file(target / "codex-package.json") if present else None
        if required and (not present or manifest is None):
            raise ValueError(f"{target}: selected release or regular codex-package.json is missing")
        return target

    def update(self) -> bool:
        process = subprocess.Popen(
            [self.codex, "app-server", "daemon", "update", "--from-cli", "--yes"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=self.environment,
            start_new_session=True,
        )
        try:
            output = process.communicate(timeout=UPDATE_TIMEOUT)[0]
        except subprocess.TimeoutExpired:
            # The npm launcher and native child share this newly owned group.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=TERMINATE_GRACE)
            except subprocess.TimeoutExpired:
                pass
            finally:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            self.warn(self.current, "update timed out; process group terminated; skipping prune")
            return False
        if process.returncode:
            lines = output.decode("utf-8", errors="replace").strip().splitlines()
            self.warn(self.current, lines[-1] if lines else f"update exited {process.returncode}")
        return True

    def reconcile(self) -> Path | None:
        admit_directory(self.home / "app-server-daemon")
        selected = self.selection()
        if selected is None:
            return None
        result = subprocess.run(
            [self.codex, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=VERSION_TIMEOUT,
            env=self.environment,
            check=False,
        )
        match = re.fullmatch(rb"codex-cli (\d+\.\d+\.\d+)", result.stdout.strip())
        if result.returncode or match is None:
            raise ValueError(f"{self.codex}: unusable --version output: {result.stdout!r}")
        try:
            version = read_object(selected / "codex-package.json").get("version")
        except (OSError, ValueError, RecursionError):
            version = None
        if version != match[1].decode("ascii") and not self.update():
            return None
        # An update can publish current before a later error: never reuse selected.
        return self.selection(required=True)

    def prune(self, selected: Path) -> None:
        descriptor = os.open(self.releases, DIRECTORY_FLAGS)
        removed = freed = 0
        try:
            for name in os.listdir(descriptor):
                if name == selected.name:
                    continue
                size = entry_size(name, descriptor)
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    shutil.rmtree(name, dir_fd=descriptor)
                else:
                    os.unlink(name, dir_fd=descriptor)
                removed += 1
                freed += size
        finally:
            os.close(descriptor)
        print(f"Codex daemon: kept {selected.name}; removed {removed} entries; freed {freed} bytes")

    def run(self) -> int:
        try:
            updater_off(self.home)
        except (OSError, ValueError, RecursionError) as error:
            self.warn(self.home / "app-server-daemon" / "settings.json", error)
        try:
            selected = self.reconcile()
            if selected is not None:
                self.prune(selected)
        except (OSError, ValueError, RecursionError, subprocess.SubprocessError) as error:
            self.warn(self.current, error)
        return int(self.warned)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", required=True)
    parser.add_argument("--codex", default="codex")
    arguments = parser.parse_args(argv)
    return Policy(arguments.codex_home, arguments.codex).run()


if __name__ == "__main__":
    raise SystemExit(main())
