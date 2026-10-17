"""Pin the image environment used by processes without shell startup files."""

import re
import shlex
from pathlib import Path


def test_image_path_and_shell_initialization() -> None:
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    base = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    path = base
    for value in re.findall(r"^ENV\s+PATH=(.*)$", dockerfile, re.M):
        value = shlex.split(value)[0].replace("${USERNAME}", "dev")
        path = value.replace("${PATH}", path).replace("$PATH", path)
    entries = path.split(":")
    assert entries == [
        "/home/dev/.cache/djinn-tools/bin",
        "/home/dev/.local/share/fnm/aliases/default/bin",
        "/home/dev/.local/bin",
        "/home/dev/.local/share/fnm",
        *base.split(":"),
    ]
    assert len(entries) == len(set(entries))
    assert not re.search(r"^\s*ENV\s+.*\bLD_LIBRARY_PATH(?:=|\s)", dockerfile, re.M)
    zshrc = re.search(r"^RUN cat > ~/\.zshrc << 'EOF'\n(.*?)^EOF$", dockerfile, re.M | re.S)
    assert zshrc is not None
    assert not re.search(r"\bexport\s+(?:PATH|LD_LIBRARY_PATH)=", zshrc[1])
