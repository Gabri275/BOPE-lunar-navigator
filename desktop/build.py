"""Build desktop/dist/LunarNavigator.exe: the interface, the analysis API and the SPICE kernels in one Windows program.

Needs Windows, Python 3.11+ and Node.js (npm). From the repo root:
  py -3.12 -m venv desktop/.venv
  desktop/.venv/Scripts/python.exe -m pip install -r desktop/requirements.txt
  desktop/.venv/Scripts/python.exe desktop/build.py
The maps are not packed (about 14 GB): the app downloads them, or uses an existing folder, on first start.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DESKTOP = REPO / "desktop"
sys.path.insert(0, str(REPO))


def run(cmd: list[str], cwd: Path, env: dict | None = None) -> None:
    print("\n>", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, env=env, check=True, shell=(os.name == "nt" and cmd[0] == "npm"))


def main() -> None:
    from analysis import config, download_kernels

    # 1. SPICE kernels (45 MB), packed into the program so it works offline from the first start.
    if not all((config.KERNEL_DIR / name).exists() for name in config.KERNELS):
        download_kernels.main()

    # 2. The interface, with the API on the same local server as the page (relative /api).
    env = {**os.environ, "VITE_API_BASE": "/api"}
    if not (REPO / "frontend" / "node_modules").exists():
        run(["npm", "ci"], REPO / "frontend", env)
    run(["npm", "run", "build"], REPO / "frontend", env)
    built = (REPO / "frontend" / "dist" / "assets")
    if not any("/api`" in p.read_text(encoding="utf-8", errors="ignore") or '"/api"' in p.read_text(encoding="utf-8", errors="ignore")
               for p in built.glob("*.js")):
        sys.exit("the interface was not built with VITE_API_BASE=/api")

    # 3. One-file program. --windowed: no console window; logs go to %LOCALAPPDATA%\BOPE Lunar Navigator\app.log.
    sep = ";" if os.name == "nt" else ":"
    for d in ("build", "dist"):
        shutil.rmtree(DESKTOP / d, ignore_errors=True)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
         "--name", "LunarNavigator", "--icon", str(DESKTOP / "icon.ico"),
         "--distpath", str(DESKTOP / "dist"), "--workpath", str(DESKTOP / "build"), "--specpath", str(DESKTOP / "build"),
         "--add-data", f"{REPO / 'frontend' / 'dist'}{sep}frontend_dist",
         "--add-data", f"{config.KERNEL_DIR}{sep}kernel",
         "--add-data", f"{DESKTOP / 'static'}{sep}desktop_static",
         "--collect-submodules", "analysis", "--collect-submodules", "uvicorn",
         "--collect-all", "spiceypy", "--collect-all", "rasterio",
         "--exclude-module", "pytest", "--exclude-module", "tkinter", "--exclude-module", "matplotlib",
         str(DESKTOP / "app.py")], REPO)
    exe = DESKTOP / "dist" / "LunarNavigator.exe"
    print(f"\nbuilt {exe} ({exe.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
