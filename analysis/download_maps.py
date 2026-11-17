"""Check or download the NASA maps into data/raw/maps/ (lists in analysis/config.py, details in data/MAPS.md).

Run from the repo root:
  .venv/Scripts/python.exe -m analysis.download_maps --check     report which maps are present and complete
  .venv/Scripts/python.exe -m analysis.download_maps             download the "base" set (about 3 GB): terrain
  .venv/Scripts/python.exe -m analysis.download_maps --all       the "complete" set (about 13.5 GB): adds the 5 m
                                                                 south-pole map, temperatures, rocks, H-parameter
Interrupted downloads resume where they stopped. data/raw/ is git-ignored: everyone keeps their own copy.
The desktop app (desktop/app.py) uses download() with a progress callback.
"""
from __future__ import annotations

import argparse
import urllib.request
from collections.abc import Callable

from . import config

CHUNK = 1 << 20


def status(key: str) -> str:
    rel, _url, size = config.MAPS[key]
    path = config.MAPS_DIR / rel
    if not path.exists():
        return "missing"
    return "ok" if path.stat().st_size == size else f"wrong size ({path.stat().st_size} bytes, expected {size})"


def missing(keys: list[str]) -> list[str]:
    return [k for k in keys if status(k) != "ok"]


def _fetch(key: str, on_bytes: Callable[[int], None], should_stop: Callable[[], bool]) -> bool:
    """Download one map into <name>.part, resuming a partial file; rename when complete. False if stopped."""
    rel, url, size = config.MAPS[key]
    path = config.MAPS_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    have = tmp.stat().st_size if tmp.exists() else 0
    if have > size:
        tmp.unlink()
        have = 0
    headers = {"User-Agent": "lunar-navigator-maps"}
    if have:
        headers["Range"] = f"bytes={have}-"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
        if have and r.status != 206:      # the server ignored the range: start again
            have = 0
        on_bytes(have)
        with open(tmp, "ab" if have else "wb") as f:
            while chunk := r.read(CHUNK):
                if should_stop():
                    return False
                f.write(chunk)
                on_bytes(len(chunk))
    if tmp.stat().st_size != size:
        raise OSError(f"{rel}: got {tmp.stat().st_size} bytes, expected {size}")
    tmp.replace(path)
    return True


def download(keys: list[str], progress: Callable[[dict], None] | None = None,
             should_stop: Callable[[], bool] = lambda: False) -> bool:
    """Download the missing maps among `keys`. progress(dict) gets done/total bytes and the current file.
    Returns True when every map is present, False when stopped."""
    todo = missing(keys)
    total = sum(config.MAPS[k][2] for k in todo)
    state = {"done": 0, "total": total, "file": "", "index": 0, "count": len(todo)}

    def on_bytes(n: int) -> None:
        state["done"] += n
        if progress:
            progress(dict(state))

    for i, key in enumerate(todo, 1):
        state.update(file=config.MAPS[key][0], index=i)
        start = state["done"]
        if not _fetch(key, on_bytes, should_stop):
            return False
        state["done"] = start + config.MAPS[key][2]
    if progress:
        progress(dict(state, file=""))
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="only report, download nothing")
    ap.add_argument("--all", action="store_true", help="the complete set (about 13.5 GB) instead of the base set (about 3 GB)")
    args = ap.parse_args()
    keys = config.MAP_SETS["complete" if args.all else "base"]
    for key, (rel, url, size) in config.MAPS.items():
        note = "" if url else "  (download by hand, see data/MAPS.md)" if key not in keys else ""
        if key not in keys and url:
            note = "  (in the complete set: use --all)"
        print(f"{status(key):>8}  {rel}  ({size / 1e6:,.0f} MB){note}")
    if args.check:
        return
    last = [-1]

    def show(p: dict) -> None:
        pct = int(100 * p["done"] / p["total"]) if p["total"] else 100
        if pct != last[0]:
            last[0] = pct
            print(f"\r  {pct:3d} %  {p['done'] / 1e9:5.2f} / {p['total'] / 1e9:.2f} GB  {p['file']:<60}", end="", flush=True)

    todo = missing(keys)
    if not todo:
        print("all maps of this set are present")
        return
    print(f"downloading {len(todo)} file(s), {sum(config.MAPS[k][2] for k in todo) / 1e9:.2f} GB")
    download(keys, show)
    print("\nall maps of this set are present")


if __name__ == "__main__":
    main()
