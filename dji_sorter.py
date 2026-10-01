#!/usr/bin/env python3
"""DJI footage sorter.

  python dji_sorter.py                  open the window
  python dji_sorter.py --source E:\\     open the window with a card preloaded
  python dji_sorter.py --watch          run in the background; opens the window when a DJI card is inserted
  python dji_sorter.py --install-autostart / --remove-autostart
  python dji_sorter.py --cli SRC... --dest DIR [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import sorter_core as core

HERE = Path(__file__).resolve().parent


def app_version() -> int:
    try:
        import json
        return int(json.loads((HERE / "version.json").read_text(encoding="utf-8"))["version"])
    except Exception:
        return 0


# --------------------------------------------------------------------------- GUI

def run_gui(preload: list[str] | None = None):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        root = TkinterDnD.Tk()
        dnd = True
    except Exception:
        root = tk.Tk()
        dnd = False

    cfg = core.load_config()
    root.title(f"DJI Footage Sorter  (version {app_version()})")
    root.geometry("780x720")
    root.minsize(640, 520)

    sources: list[str] = []
    events: queue.Queue = queue.Queue()
    busy = {"on": False}

    pad = {"padx": 10, "pady": 4}

    # ---- drop zone
    drop = tk.Label(root, text=("Drag DJI files or the SD card's DCIM folder here" if dnd
                                else "Use the buttons below to add files or a folder"),
                    relief="ridge", bd=2, height=4, bg="#eef3f8", fg="#333", font=("Segoe UI", 12))
    drop.pack(fill="x", **pad)

    btns = ttk.Frame(root)
    btns.pack(fill="x", **pad)
    count_var = tk.StringVar(value="No files added")

    lst = tk.Listbox(root, height=7)
    lst.pack(fill="both", expand=True, **pad)

    def add_sources(paths):
        for p in paths:
            if p and p not in sources:
                sources.append(p)
                lst.insert("end", p)
        n = len(core.collect(sources)) if sources else 0
        count_var.set(f"{n} photo/video files ready")

    def pick_files():
        add_sources(filedialog.askopenfilenames(title="Choose DJI files"))

    def pick_folder():
        d = filedialog.askdirectory(title="Choose a folder (e.g. the card's DCIM)")
        if d:
            add_sources([d])

    def clear():
        sources.clear()
        lst.delete(0, "end")
        count_var.set("No files added")

    ttk.Button(btns, text="Add files…", command=pick_files).pack(side="left")
    ttk.Button(btns, text="Add folder…", command=pick_folder).pack(side="left", padx=6)
    ttk.Button(btns, text="Clear", command=clear).pack(side="left")
    ttk.Label(btns, textvariable=count_var).pack(side="right")

    if dnd:
        def on_drop(e):
            add_sources(root.tk.splitlist(e.data))
        for w in (drop, lst):
            w.drop_target_register(DND_FILES)
            w.dnd_bind("<<Drop>>", on_drop)

    # ---- destination
    dest_frame = ttk.LabelFrame(root, text="Save to")
    dest_frame.pack(fill="x", **pad)
    dest_var = tk.StringVar(value=(cfg["recent_destinations"] or [""])[0])
    dest_box = ttk.Combobox(dest_frame, textvariable=dest_var, values=cfg["recent_destinations"])
    dest_box.pack(side="left", fill="x", expand=True, padx=6, pady=6)

    def pick_dest():
        d = filedialog.askdirectory(title="Where should sorted footage go?")
        if d:
            dest_var.set(d)
    ttk.Button(dest_frame, text="Browse…", command=pick_dest).pack(side="left", padx=6)

    # ---- options
    opt = ttk.LabelFrame(root, text="Options")
    opt.pack(fill="x", **pad)
    move_var = tk.BooleanVar(value=cfg.get("mode") == "move")
    lrf_var = tk.BooleanVar(value=not cfg.get("skip_lrf", True))
    online_var = tk.BooleanVar(value=cfg.get("online_place_names", True))
    lut_var = tk.BooleanVar(value=cfg.get("lut_enabled", False))
    lut_path = tk.StringVar(value=cfg.get("lut_path", ""))
    ttk.Checkbutton(opt, text="Move instead of copy (clears the card)", variable=move_var).grid(row=0, column=0, sticky="w", padx=6)
    ttk.Checkbutton(opt, text="Include LRF proxy files", variable=lrf_var).grid(row=0, column=1, sticky="w", padx=6)
    ttk.Checkbutton(opt, text="Look up place names online", variable=online_var).grid(row=1, column=0, sticky="w", padx=6)
    ttk.Checkbutton(opt, text="Apply LUT to videos (saves a _graded copy)", variable=lut_var).grid(row=1, column=1, sticky="w", padx=6)
    lut_row = ttk.Frame(opt)
    lut_row.grid(row=2, column=0, columnspan=2, sticky="we", padx=6, pady=(0, 6))
    ttk.Label(lut_row, text="LUT file:").pack(side="left")
    ttk.Entry(lut_row, textvariable=lut_path).pack(side="left", fill="x", expand=True, padx=4)
    ttk.Button(lut_row, text="Choose .cube…", command=lambda: lut_path.set(
        filedialog.askopenfilename(filetypes=[("LUT", "*.cube"), ("All", "*.*")]) or lut_path.get())).pack(side="left")
    fb_var = tk.BooleanVar(value=cfg.get("fb_enabled", False))
    fb_aspect = tk.StringVar(value=cfg.get("fb_aspect") or next(iter(core.FB_SIZES)))
    fb_row = ttk.Frame(opt)
    fb_row.grid(row=4, column=0, columnspan=2, sticky="we", padx=6, pady=(0, 6))
    ttk.Checkbutton(fb_row, text="Make Facebook copies (no audio) in a Facebook folder, size:",
                    variable=fb_var).pack(side="left")
    ttk.Combobox(fb_row, textvariable=fb_aspect, values=list(core.FB_SIZES), state="readonly", width=28).pack(side="left", padx=4)
    auto_var = tk.BooleanVar(value=autostart_installed())

    def toggle_auto():
        try:
            install_autostart() if auto_var.get() else remove_autostart()
        except Exception as e:
            messagebox.showerror("DJI Sorter", f"Couldn't change auto-open: {e}")
            auto_var.set(autostart_installed())
    ttk.Checkbutton(opt, text="Open automatically when a DJI SD card is inserted",
                    variable=auto_var, command=toggle_auto).grid(row=3, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 6))
    opt.columnconfigure(1, weight=1)

    # ---- actions / progress / log
    act = ttk.Frame(root)
    act.pack(fill="x", **pad)
    prog = ttk.Progressbar(act, mode="determinate")
    prog.pack(side="left", fill="x", expand=True)
    status = tk.StringVar(value="")
    log_box = tk.Text(root, height=8, state="disabled", wrap="none", font=("Consolas", 9))
    ttk.Label(root, textvariable=status).pack(fill="x", padx=10)
    log_box.pack(fill="both", expand=True, **pad)

    def log(msg):
        events.put(("log", msg))

    def progress(n, total, msg):  # planning phase: count of files
        events.put(("prog", (n / max(total, 1), msg)))

    def copy_progress(frac, secs_left, msg):  # sorting phase: weighted by work, with time left
        events.put(("prog", (frac, f"{msg}  ·  {int(frac * 100)}%  ·  {core.fmt_eta(secs_left)}")))

    def current_cfg():
        cfg.update(mode="move" if move_var.get() else "copy", skip_lrf=not lrf_var.get(),
                   online_place_names=online_var.get(), lut_enabled=lut_var.get(), lut_path=lut_path.get(),
                   fb_enabled=fb_var.get(), fb_aspect=fb_aspect.get())
        return cfg

    def start(dry: bool):
        if busy["on"]:
            return
        dest = dest_var.get().strip()
        if not sources:
            messagebox.showinfo("DJI Sorter", "Add some files first.")
            return
        if not dest:
            messagebox.showinfo("DJI Sorter", "Pick where to save the footage.")
            return
        c = current_cfg()
        core.save_config(c)
        if not dry:
            core.remember_destination(c, dest)
            dest_box["values"] = c["recent_destinations"]
        busy["on"] = True

        def work():
            try:
                files = core.collect(sources)
                items = core.plan(files, Path(dest), c, log=log, progress=progress)
                folders = {}
                for it in items:
                    folders.setdefault(it.dest.parent, 0)
                    folders[it.dest.parent] += 1
                log(f"{'Preview' if dry else 'Sorting'}: {len(files)} files into {len(folders)} folders")
                for f, n in sorted(folders.items()):
                    log(f"  {f}  ({n} files)")
                for it in items:
                    if it.info.gps_source.startswith("nearby"):
                        log(f"  note: {it.src.name} had no GPS, placed using {it.info.gps_source}")
                if not dry:
                    t0 = time.time()
                    s = core.execute(items, c, log=log, progress=copy_progress, dest_root=Path(dest))
                    mins = (time.time() - t0) / 60
                    log(f"Done in {mins:.1f} min. {s['copied']} {'moved' if c['mode'] == 'move' else 'copied'}, "
                        f"{s['skipped']} already there, {s['failed']} failed"
                        + (f", {s['graded']} graded" if c.get("lut_enabled") else "")
                        + (f", {s['facebook']} Facebook copies in {core.fb_root(Path(dest), c)}" if c.get("fb_enabled") else ""))
                    events.put(("done", dest))
                else:
                    events.put(("done", None))
            except Exception as e:
                log(f"Error: {e}")
                events.put(("done", None))

        threading.Thread(target=work, daemon=True).start()

    ttk.Button(act, text="Preview", command=lambda: start(True)).pack(side="left", padx=6)
    ttk.Button(act, text="Sort footage", command=lambda: start(False)).pack(side="left")

    def pump():
        try:
            while True:
                kind, val = events.get_nowait()
                if kind == "log":
                    log_box.configure(state="normal")
                    log_box.insert("end", val + "\n")
                    log_box.see("end")
                    log_box.configure(state="disabled")
                elif kind == "prog":
                    frac, msg = val
                    prog["maximum"] = 1000
                    prog["value"] = int(frac * 1000)
                    status.set(msg)
                elif kind == "done":
                    busy["on"] = False
                    status.set("Finished")
                    if val:
                        open_folder(val)
        except queue.Empty:
            pass
        root.after(100, pump)

    pump()
    if os.environ.get("DJISORTER_UPDATE_MSG"):
        log(os.environ["DJISORTER_UPDATE_MSG"])
    if preload:
        add_sources(preload)
    root.lift()
    root.attributes("-topmost", True)
    root.after(500, lambda: root.attributes("-topmost", False))
    root.mainloop()


def open_folder(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


# --------------------------------------------------------------------------- watcher

def self_cmd() -> list[str]:
    """How to launch this app again: the .exe itself when frozen, else pythonw + this script."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [_pythonw(), str(HERE / "dji_sorter.py")]


def watch(interval=3.0, autostarted=False):
    """Polls for newly inserted DJI cards and opens the sorter window pointed at the card."""
    import socket
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", 47831))  # only one watcher at a time
    except OSError:
        return
    known = set(map(str, core.mounted_volumes()))
    print("Watching for DJI SD cards... (Ctrl+C to stop)")
    while True:
        time.sleep(interval)
        if autostarted and not autostart_installed():
            return  # switched off from the app
        now = set(map(str, core.mounted_volumes()))
        for vol in sorted(now - known):
            if core.looks_like_dji_card(Path(vol)):
                print(f"DJI card found at {vol}")
                subprocess.Popen(self_cmd() + ["--source", str(Path(vol) / "DCIM")])
        known = now


# --------------------------------------------------------------------------- autostart

def _pythonw():
    exe = Path(sys.executable)
    if sys.platform.startswith("win"):
        w = exe.with_name("pythonw.exe")
        return str(w if w.exists() else exe)
    return str(exe)


def _autostart_file() -> Path:
    if sys.platform.startswith("win"):
        return Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs\Startup\DJI Sorter Watcher.vbs"
    if sys.platform == "darwin":
        return Path.home() / "Library/LaunchAgents/com.djisorter.watcher.plist"
    return Path.home() / ".config/autostart/djisorter-watcher.desktop"


def autostart_installed() -> bool:
    return _autostart_file().exists()


def install_autostart():
    script = HERE / "dji_sorter.py"
    if sys.platform.startswith("win"):
        vbs = _autostart_file()
        cmd = " ".join(f'""{c}""' for c in self_cmd()) + " --watch --autostarted"
        vbs.write_text('CreateObject("WScript.Shell").Run "{}", 0, False\n'.format(cmd), encoding="utf-8")
        subprocess.Popen(["wscript", str(vbs)])
        print(f"Installed: {vbs}\nThe watcher is running now and will start at every login.")
    elif sys.platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents/com.djisorter.watcher.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.djisorter.watcher</string>
  <key>ProgramArguments</key><array>
    <string>{sys.executable}</string><string>{script}</string><string>--watch</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/tmp/djisorter.log</string>
</dict></plist>
""")
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        subprocess.run(["launchctl", "load", str(plist)])
        print(f"Installed: {plist}\nThe watcher is running now and will start at every login.")
    else:
        d = Path.home() / ".config/autostart"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "djisorter-watcher.desktop"
        f.write_text(f"[Desktop Entry]\nType=Application\nName=DJI Sorter Watcher\n"
                     f"Exec={sys.executable} {script} --watch\nX-GNOME-Autostart-enabled=true\n")
        subprocess.Popen([sys.executable, str(script), "--watch"], start_new_session=True)
        print(f"Installed: {f}")


def remove_autostart():
    if sys.platform.startswith("win"):
        vbs = Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs\Startup\DJI Sorter Watcher.vbs"
        vbs.unlink(missing_ok=True)
        print("Removed from startup.")
    elif sys.platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents/com.djisorter.watcher.plist"
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        plist.unlink(missing_ok=True)
        print("Removed.")
    else:
        (Path.home() / ".config/autostart/djisorter-watcher.desktop").unlink(missing_ok=True)
        print("Removed from autostart.")


# --------------------------------------------------------------------------- CLI

def run_cli(srcs, dest, dry):
    cfg = core.load_config()
    items = core.plan(core.collect(srcs), Path(dest), cfg)
    for it in items:
        print(f"{it.src.name:32} -> {it.dest.parent}   [{it.info.gps_source or 'no gps'}]")
    if not dry:
        print(core.execute(items, cfg, dest_root=Path(dest),
                           progress=lambda f, s, m: print(f"\r{int(f*100):3d}% {core.fmt_eta(s):24} {m[:60]:60}", end="")))


def main():
    ap = argparse.ArgumentParser(description="Sort DJI footage into date + location folders")
    ap.add_argument("--source", action="append")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--autostarted", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--install-autostart", action="store_true")
    ap.add_argument("--remove-autostart", action="store_true")
    ap.add_argument("--cli", nargs="+", metavar="SRC")
    ap.add_argument("--dest")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.install_autostart:
        install_autostart()
    elif a.remove_autostart:
        remove_autostart()
    elif a.watch:
        watch(autostarted=a.autostarted)
    elif a.cli:
        if not a.dest:
            ap.error("--cli needs --dest")
        run_cli(a.cli, a.dest, a.dry_run)
    else:
        run_gui(a.source)


if __name__ == "__main__":
    main()
