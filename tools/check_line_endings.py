"""Settle the line-ending question with exact byte counts.

PowerShell string handling can alter CR characters, so this reads the raw
working-tree file and the raw Git blob as bytes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(r"C:\Users\Bhatek Space\Desktop\newseting\ems2\ems")
FILES = [
    "docker-entrypoint.sh",
    "Dockerfile",
    "railway.toml",
    ".dockerignore",
    "requirements.txt",
]

print("=== working tree (what a local Docker build would copy) ===")
critical_bad = []
for name in FILES:
    path = REPO / name
    if not path.exists():
        print(f"  {name:<24} MISSING")
        continue
    data = path.read_bytes()
    cr = data.count(b"\r")
    lf = data.count(b"\n")
    verdict = "LF" if cr == 0 else f"CRLF ({cr} CR)"
    print(f"  {name:<24} CR={cr:<5} LF={lf:<5} -> {verdict}")
    if cr and name in {"docker-entrypoint.sh", "Dockerfile"}:
        critical_bad.append(name)

print("\n=== committed blob in Git (what Railway/Docker actually builds) ===")
for name in FILES:
    result = subprocess.run(
        ["git", "-C", str(REPO), "cat-file", "-p", f"HEAD:{name}"],
        capture_output=True,
    )
    if result.returncode != 0:
        print(f"  {name:<24} not in the commit")
        continue
    blob = result.stdout
    cr = blob.count(b"\r")
    lf = blob.count(b"\n")
    verdict = "LF" if cr == 0 else f"CRLF ({cr} CR)"
    print(f"  {name:<24} CR={cr:<5} LF={lf:<5} -> {verdict}")
    if cr and name in {"docker-entrypoint.sh", "Dockerfile"}:
        critical_bad.append(f"HEAD:{name}")

print("\n=== git's normalisation report ===")
eol = subprocess.run(
    ["git", "-C", str(REPO), "ls-files", "--eol", *FILES],
    capture_output=True, text=True,
)
print(eol.stdout.rstrip() or "(no output)")

print("\n=== VERDICT ===")
if critical_bad:
    print(f"  PROBLEM: CRLF stored for {sorted(set(critical_bad))}")
    print("  The container entrypoint will fail on Linux.")
    sys.exit(1)
print("  SAFE: docker-entrypoint.sh and Dockerfile are LF in the commit.")
print("  Docker will execute the entrypoint correctly.")
