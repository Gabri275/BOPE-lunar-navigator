"""BOPE Lunar Navigator as a Windows desktop app: the analysis API and the web interface in one native window.

One process: a local web server on 127.0.0.1 (random free port) serves
  /            the built interface (frontend/dist), the same page as the web version
  /api/...     the analysis API (analysis/api.py), loaded once the maps folder is known
  /setup       the first-run page that chooses or downloads the NASA maps
and a pywebview window (Microsoft Edge WebView2) shows it. Settings live in %LOCALAPPDATA%\\BOPE Lunar Navigator.

Run from source:  desktop/.venv/Scripts/python.exe desktop/app.py   (add --setup to show the maps page again)
Build the .exe:   desktop/.venv/Scripts/python.exe desktop/build.py
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

APP_NAME = "BOPE Lunar Navigator"
TITLE = "Lunar Landing Site Navigator"
REPO_URL = "https://github.com/Gabri275/BOPE-lunar-navigator"
FROZEN = getattr(sys, "frozen", False)
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))   # files packed into the .exe
HOME = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME              # settings, log, default maps folder
SETTINGS = HOME / "settings.json"
LOG = HOME / "app.log"
DEFAULT_MAPS = HOME / "maps"

if FROZEN:
    FRONTEND = BUNDLE / "frontend_dist"
    KERNELS = BUNDLE / "kernel"
    STATIC = BUNDLE / "desktop_static"
else:
    FRONTEND = BUNDLE / "frontend" / "dist"
    KERNELS = BUNDLE / "data" / "raw" / "kernel"
    STATIC = BUNDLE / "desktop" / "static"
    sys.path.insert(0, str(BUNDLE))

HOME.mkdir(parents=True, exist_ok=True)
if sys.stdout is None or sys.stderr is None:      # windowed .exe: no console, write everything to the log file
    _log = open(LOG, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log


def log(msg: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")


# ---------------------------------------------------------------- settings and the maps folder

def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(**changes) -> dict:
    s = {**load_settings(), **changes}
    SETTINGS.write_text(json.dumps(s, indent=2), encoding="utf-8")
    return s


def find_maps_dir(folder: str) -> Path | None:
    """The maps folder inside what the user picked: the folder itself, maps/, raw/maps/ or data/raw/maps/."""
    p = Path(folder)
    for cand in (p, p / "maps", p / "raw" / "maps", p / "data" / "raw" / "maps"):
        if (cand / "dem").is_dir():
            return cand
    return None


def folder_summary(maps_dir: Path) -> dict:
    """Which download sets are complete in a maps folder (checks names and sizes, reads nothing)."""
    from analysis import config      # config only: its MAPS table does not depend on the folder
    def present(keys):
        return [k for k in keys if (maps_dir / config.MAPS[k][0]).is_file()
                and (maps_dir / config.MAPS[k][0]).stat().st_size == config.MAPS[k][2]]
    out = {}
    for name, keys in config.MAP_SETS.items():
        ok = present(keys)
        out[name] = {"present": len(ok), "count": len(keys), "complete": len(ok) == len(keys)}
    return out


# ---------------------------------------------------------------- the analysis API, loaded once the folder is known

_analysis = {"app": None, "error": None, "maps_dir": None}
_analysis_lock = threading.Lock()


def activate(maps_dir: Path) -> None:
    """Point the analysis at the bundled kernels and the chosen maps folder. Must run before analysis.api is imported."""
    maps_dir.mkdir(parents=True, exist_ok=True)
    os.environ["BOPE_KERNEL_DIR"] = str(KERNELS)
    os.environ["BOPE_MAPS_DIR"] = str(maps_dir)
    from analysis import config
    config.KERNEL_DIR, config.MAPS_DIR = KERNELS, maps_dir
    _analysis["maps_dir"] = maps_dir


def analysis_app():
    """analysis.api.app, imported on first use (after activate). None if it could not start (the page then falls back
    to its offline mode with the 31 known sites)."""
    with _analysis_lock:
        if _analysis["app"] is None and _analysis["error"] is None and _analysis["maps_dir"] is not None:
            try:
                from analysis.api import app as api
                _analysis["app"] = api
                log(f"analysis API ready, maps in {_analysis['maps_dir']}")
            except Exception:
                _analysis["error"] = traceback.format_exc()
                log("analysis API failed to start:\n" + _analysis["error"])
        return _analysis["app"]


# ---------------------------------------------------------------- first-run page: choose or download the maps

class Job:
    """One background task of the setup page (download, then preparing the caches)."""
    def __init__(self):
        self.state = {"phase": "idle", "done": 0, "total": 0, "file": "", "index": 0, "count": 0, "error": None}
        self.stop = False
        self.thread: threading.Thread | None = None

    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


job = Job()


def prepare_caches() -> None:
    """Build the Diviner caches once (reading the 2.8 GB temperature tables takes a few minutes), so the first click
    on the map does not wait. Each function returns quickly when its map is missing or already cached."""
    from analysis import regolith
    for fn in (regolith.temperature_grids, regolith.polar_temperature, regolith.rock_grid, regolith.h_grid):
        if job.stop:
            return
        job.state["file"] = fn.__name__
        fn()


def run_download(set_name: str) -> None:
    try:
        from analysis import config, download_maps
        keys = config.MAP_SETS[set_name]
        job.state.update(phase="download", error=None)
        ok = download_maps.download(keys, progress=lambda p: job.state.update(p), should_stop=lambda: job.stop)
        if not ok:
            job.state["phase"] = "stopped"
            return
        job.state.update(phase="preparing", file="")
        prepare_caches()
        job.state["phase"] = "stopped" if job.stop else "done"
        if not job.stop:
            save_settings(setup_done=True, maps_dir=str(_analysis["maps_dir"]), maps_set=set_name)
    except Exception as e:
        log("download failed:\n" + traceback.format_exc())
        job.state.update(phase="error", error=f"{type(e).__name__}: {e}")


def run_prepare(maps_dir: Path) -> None:
    try:
        job.state.update(phase="preparing", file="", error=None)
        prepare_caches()
        job.state["phase"] = "done"
        save_settings(setup_done=True, maps_dir=str(maps_dir), maps_set="folder")
    except Exception as e:
        log("preparing failed:\n" + traceback.format_exc())
        job.state.update(phase="error", error=f"{type(e).__name__}: {e}")


def needs_restart(maps_dir: Path) -> bool:
    """The analysis reads the folder when it is imported: another folder after that needs a fresh start."""
    loaded = "analysis.api" in sys.modules or "analysis.terrain" in sys.modules
    return loaded and _analysis["maps_dir"] is not None and Path(_analysis["maps_dir"]) != maps_dir


def restart() -> None:
    """Start the app again (new process) and quit this one."""
    args = [sys.executable] + ([] if FROZEN else [str(Path(__file__).resolve())])
    subprocess.Popen(args, close_fds=True)
    os._exit(0)


def build_setup_app():
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import FileResponse, JSONResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    def sizes():
        from analysis import config
        return {k: round(sum(config.MAPS[m][2] for m in keys) / 1e9, 1) for k, keys in config.MAP_SETS.items()}

    async def page(_request):
        return FileResponse(STATIC / "setup.html")

    async def state(_request):
        s = load_settings()
        return JSONResponse({"sizes": sizes(), "default_dir": str(DEFAULT_MAPS),
                             "free_gb": round(shutil.disk_usage(HOME).free / 1e9, 1),
                             "settings": s, "job": job.state, "busy": job.busy(), "repo": REPO_URL})

    async def check_folder(request: Request):
        body = await request.json()
        maps_dir = find_maps_dir(body.get("path", ""))
        if maps_dir is None:
            return JSONResponse({"ok": False, "error": "No maps found there. Pick the folder that contains dem/ "
                                 "(or a project folder with data/raw/maps)."})
        return JSONResponse({"ok": True, "maps_dir": str(maps_dir), "sets": folder_summary(maps_dir)})

    async def use_folder(request: Request):
        maps_dir = find_maps_dir((await request.json()).get("path", ""))
        if maps_dir is None or job.busy():
            return JSONResponse({"ok": False}, status_code=400)
        if needs_restart(maps_dir):
            save_settings(setup_done=True, maps_dir=str(maps_dir), maps_set="folder")
            threading.Timer(0.5, restart).start()
            return JSONResponse({"ok": True, "restart": True})
        activate(maps_dir)
        job.stop = False
        job.thread = threading.Thread(target=run_prepare, args=(maps_dir,), daemon=True)
        job.thread.start()
        return JSONResponse({"ok": True})

    async def download(request: Request):
        set_name = (await request.json()).get("set")
        if set_name not in ("base", "complete") or job.busy():
            return JSONResponse({"ok": False}, status_code=400)
        if needs_restart(DEFAULT_MAPS):
            save_settings(setup_done=False, maps_dir=str(DEFAULT_MAPS))
            threading.Timer(0.5, restart).start()
            return JSONResponse({"ok": True, "restart": True})
        activate(DEFAULT_MAPS)
        job.stop = False
        job.thread = threading.Thread(target=run_download, args=(set_name,), daemon=True)
        job.thread.start()
        return JSONResponse({"ok": True})

    async def cancel(_request):
        job.stop = True
        return JSONResponse({"ok": True})

    async def no_maps(_request):
        if job.busy():
            return JSONResponse({"ok": False}, status_code=400)
        # Keep the folder already in use (the user may come back here from the Maps menu), else the default one.
        current = Path(_analysis["maps_dir"] or load_settings().get("maps_dir") or DEFAULT_MAPS)
        if _analysis["maps_dir"] is None:
            activate(current)
        save_settings(setup_done=True, maps_dir=str(current), maps_set=load_settings().get("maps_set") or "none")
        return JSONResponse({"ok": True})

    return Starlette(routes=[
        Route("/setup", page), Route("/setup/", page),
        Route("/setup/api/state", state),
        Route("/setup/api/check-folder", check_folder, methods=["POST"]),
        Route("/setup/api/use-folder", use_folder, methods=["POST"]),
        Route("/setup/api/download", download, methods=["POST"]),
        Route("/setup/api/cancel", cancel, methods=["POST"]),
        Route("/setup/api/no-maps", no_maps, methods=["POST"]),
        Mount("/setup/static", app=StaticFiles(directory=STATIC)),
    ])


# ---------------------------------------------------------------- one ASGI app for everything

class Root:
    """Routes /api to the analysis, /setup to the first-run page and everything else to the built interface."""
    def __init__(self):
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Mount
        from starlette.staticfiles import StaticFiles
        self.setup = build_setup_app()
        # Inside a Starlette app, so a missing file is a plain 404 answer instead of an unhandled exception.
        self.static = Starlette(routes=[Mount("/", app=StaticFiles(directory=FRONTEND, html=True))])
        self.unavailable = JSONResponse({"detail": "the analysis server is not running"}, status_code=503)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        path = scope.get("path", "")
        if path.startswith("/api"):
            api = analysis_app()
            return await (api or self.unavailable)(scope, receive, send)
        if path.startswith("/setup"):
            return await self.setup(scope, receive, send)
        return await self.static(scope, receive, send)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port: int):
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(Root(), host="127.0.0.1", port=port, log_config=None, access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("the local server did not start")


# ---------------------------------------------------------------- the window

class WindowApi:
    """Functions the setup page calls through window.pywebview.api."""
    def choose_folder(self):
        import webview
        picked = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        return picked[0] if picked else None


def main() -> None:
    import webview
    from webview.menu import Menu, MenuAction, MenuSeparator

    log(f"start {APP_NAME} (frozen={FROZEN})")
    settings = load_settings()
    show_setup = "--setup" in sys.argv or not settings.get("setup_done")
    if not show_setup:
        activate(Path(settings.get("maps_dir") or DEFAULT_MAPS))
    port = free_port()
    server = start_server(port)
    base = f"http://127.0.0.1:{port}"
    window = webview.create_window(TITLE, f"{base}/setup" if show_setup else f"{base}/", js_api=WindowApi(),
                                   width=1600, height=960, min_size=(1100, 700), maximized=True,
                                   background_color="#000000")

    def change_maps():
        save_settings(setup_done=False)
        restart()

    def open_maps_folder():
        folder = Path(load_settings().get("maps_dir") or DEFAULT_MAPS)
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    menu = [
        Menu("Maps", [MenuAction("Change or download the maps…", change_maps),
                      MenuAction("Open the maps folder", open_maps_folder)]),
        Menu("Help", [MenuAction("Project page on GitHub", lambda: webbrowser_open(REPO_URL)),
                      MenuSeparator(),
                      MenuAction("Open the log file", lambda: os.startfile(LOG))]),
    ]
    webview.start(menu=menu, private_mode=False, storage_path=str(HOME / "webview"))
    server.should_exit = True
    log("closed")


def webbrowser_open(url: str) -> None:
    import webbrowser
    webbrowser.open(url)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("fatal:\n" + traceback.format_exc())
        raise
