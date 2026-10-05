"""Resolve the host Docker executable once, before any dev creation."""

import shutil
from pathlib import Path

DOCKER_EXECUTABLE = str(Path(shutil.which("docker") or "/usr/bin/docker").absolute())
