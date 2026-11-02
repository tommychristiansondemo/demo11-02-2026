"""Build ARM64 (manylinux aarch64) code-zip packages for the three AgentCore Runtimes.

AgentCore Runtime runs on Graviton; this host has no arm64 Docker emulation, so we use direct code deployment:
cross-platform wheels + our sources, zipped with fixed timestamps so unchanged code keeps the same CDK asset hash.
Usage: python scripts/build_runtimes.py [service ...]
"""
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "infra" / "build"
SERVICES = {"agent": ["main.py"], "client_profile_mcp": ["main.py"], "planning_calc_mcp": ["main.py", "calc.py"]}


def build(name: str) -> Path:
    src = ROOT / "services" / name
    stage = OUT / f"{name}_pkg"
    req = src / "requirements.txt"
    stamp = stage / ".req-sha"
    req_sha = hashlib.sha256(req.read_bytes()).hexdigest()
    if not (stamp.exists() and stamp.read_text() == req_sha):
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "-r", str(req), "--target", str(stage),
                               "--platform", "manylinux2014_aarch64", "--implementation", "cp",
                               "--python-version", "3.12", "--only-binary=:all:", "--upgrade"])
        stamp.write_text(req_sha)
    for f in SERVICES[name]:
        shutil.copy2(src / f, stage / f)
    shutil.copy2(ROOT / "services" / "shared" / "jsonlog.py", stage / "jsonlog.py")
    zpath = OUT / f"{name}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(stage.rglob("*")):
            rel = p.relative_to(stage).as_posix()
            if p.is_dir() or "__pycache__" in rel or rel == ".req-sha":
                continue
            info = zipfile.ZipInfo(rel, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = (0o755 if rel.startswith("bin/") else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, p.read_bytes())
    print(f"{name}: {zpath} ({zpath.stat().st_size / 1e6:.1f} MB)")
    return zpath


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for n in (sys.argv[1:] or SERVICES):
        build(n)
