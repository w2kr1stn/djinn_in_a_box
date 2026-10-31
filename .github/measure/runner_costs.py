"""Temporary measurement for #214: per-process CPU cost of the tools the package replay starts."""

import resource
import shutil
import subprocess
import sys

N = 400
CLEAN = ["/usr/bin/env", "-i", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C.UTF-8"]
CASES = {
    "env -i true": CLEAN + ["/usr/bin/true"],
    "env -i date +%s": CLEAN + ["/usr/bin/date", "+%s"],
    "date +%s": ["/usr/bin/date", "+%s"],
    "sh -c :": ["/bin/sh", "-c", ":"],
    "grep -qxF": ["/usr/bin/grep", "-qxF", "--", "a"],
    "awk BEGIN": ["/usr/bin/awk", "BEGIN{}"],
}


def child_cpu() -> float:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


for tool in ("env", "date", "mv", "grep", "awk", "sh", "true"):
    path = shutil.which(tool) or "?"
    real = subprocess.run(["readlink", "-f", path], capture_output=True, text=True).stdout.strip()
    pkg = " / ".join(
        subprocess.run(["dpkg", "-S", p], capture_output=True, text=True).stdout.strip()
        for p in (real, real.replace("/usr/bin/", "/bin/"))
    )
    print(f"{tool}: {path} -> {real} [{pkg}]")
for cmd in (["env", "--version"], ["mv", "--version"]):
    out = subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines()
    print(" ".join(cmd), "->", out[0] if out else "?")

for name, argv in CASES.items():
    start = child_cpu()
    for _ in range(N):
        subprocess.run(argv, input="a\n", capture_output=True, text=True, check=False)
    per = (child_cpu() - start) / N * 1000
    print(f"{name}: {per:.3f} ms child CPU per process (n={N})")
sys.stdout.flush()
