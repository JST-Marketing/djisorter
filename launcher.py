"""Entry point for the built DJI Sorter app.

The app's own code (dji_sorter.py, sorter_core.py) is NOT frozen into the .exe. It's shipped as plain
files and loaded from %LOCALAPPDATA%/DJISorter/code when a newer copy is there. On every launch the
launcher checks the update address for a newer version and downloads it, so code changes never need a
reinstall. Only a change to the libraries bundled here (see the hidden imports below) needs Setup again.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

LAUNCHER_VERSION = 1
DEFAULT_UPDATE_URL = "https://raw.githubusercontent.com/JST-Marketing/djisorter/main/"

if os.environ.get("DJISORTER_NEVER_SET"):
    # Never runs: tells PyInstaller to bundle everything the loaded code (and future versions) may import.
    import argparse, csv, ctypes, datetime, math, queue, re, socket, sqlite3, string, struct, subprocess  # noqa
    import tempfile, threading, time, urllib.parse, zipfile, concurrent.futures, dataclasses, xml.etree.ElementTree  # noqa
    import tkinter, tkinter.ttk, tkinter.filedialog, tkinter.messagebox, tkinter.simpledialog  # noqa
    import tkinterdnd2  # noqa
    from PIL import Image, ImageOps, TiffImagePlugin, JpegImagePlugin  # noqa


def app_data() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "DJISorter"


def bundled_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "app_code" \
        if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def read_version(d: Path) -> int:
    try:
        return int(json.loads((d / "version.json").read_text(encoding="utf-8"))["version"])
    except Exception:
        return 0


def update_url() -> str:
    # config.json (written by the app) can override the address
    for cfg in (Path(os.environ.get("APPDATA", "")) / "DJISorter" / "config.json",
                Path.home() / "Library/Application Support/DJISorter/config.json",
                Path.home() / ".config/DJISorter/config.json"):
        try:
            url = json.loads(cfg.read_text(encoding="utf-8")).get("update_url")
            if url:
                return url
        except Exception:
            pass
    return DEFAULT_UPDATE_URL


def _get(url: str, timeout=4) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "DJISorter-updater", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def try_update(code_dir: Path, current: int) -> str | None:
    """Downloads a newer version into code_dir. Returns a message for the user, or None."""
    base = update_url()
    if not base:
        return None
    base = base.rstrip("/") + "/"
    try:
        manifest = json.loads(_get(base + "version.json"))
        latest = int(manifest["version"])
        if latest <= current:
            return None
        if int(manifest.get("launcher_min", 1)) > LAUNCHER_VERSION:
            return (f"Version {latest} is available but needs a fresh install: "
                    f"run 'Setup (Windows).bat' from the latest download.")
        new = code_dir.with_name("code.new")
        shutil.rmtree(new, ignore_errors=True)
        new.mkdir(parents=True)
        for name, sha in manifest["files"].items():
            data = _get(f"{base}{name}?v={latest}", timeout=20)
            if hashlib.sha256(data).hexdigest() != sha:
                raise ValueError(f"checksum mismatch on {name}")
            (new / name).write_bytes(data)
        (new / "version.json").write_text(json.dumps(manifest), encoding="utf-8")
        old = code_dir.with_name("code.old")
        shutil.rmtree(old, ignore_errors=True)
        if code_dir.exists():
            code_dir.rename(old)
        new.rename(code_dir)
        shutil.rmtree(old, ignore_errors=True)
        return f"Updated to version {latest}. {manifest.get('notes', '')}".strip()
    except Exception:
        shutil.rmtree(code_dir.with_name("code.new"), ignore_errors=True)
        return None  # offline or server hiccup: just run what we have


def main():
    code_dir = app_data() / "code"
    bundled = bundled_dir()
    use = code_dir if read_version(code_dir) > read_version(bundled) else bundled
    current = read_version(use)
    if "--watch" not in sys.argv and "--no-update" not in sys.argv:
        msg = try_update(code_dir, current)
        if msg:
            os.environ["DJISORTER_UPDATE_MSG"] = msg
            if read_version(code_dir) > current:
                use = code_dir
    sys.argv = [a for a in sys.argv if a != "--no-update"]
    sys.path.insert(0, str(use))
    import importlib
    importlib.import_module("dji_sorter").main()  # dynamic, so PyInstaller doesn't freeze an old copy


if __name__ == "__main__":
    main()
