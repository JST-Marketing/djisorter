"""Core logic for the DJI footage sorter: metadata, place names, planning and copying.

No GUI code lives here, so it can be tested or scripted on its own.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

PHOTO_EXT = {".jpg", ".jpeg", ".dng", ".tif", ".tiff", ".heic"}
VIDEO_EXT = {".mp4", ".mov"}
SIDECAR_EXT = {".srt", ".lrf"}
MEDIA_EXT = PHOTO_EXT | VIDEO_EXT | SIDECAR_EXT


# --------------------------------------------------------------------------- config

def config_dir() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / "DJISorter"
    d.mkdir(parents=True, exist_ok=True)
    return d


DEFAULT_CONFIG = {
    "recent_destinations": [],
    "folder_format": "{date} - {place}",
    "mode": "copy",                # "copy" or "move"
    "skip_lrf": True,              # LRF = DJI low-res proxy files
    "online_place_names": True,    # reverse geocode via OpenStreetMap
    "lut_enabled": False,
    "lut_path": "",
    "lut_mode": "graded_copy",     # graded_copy (keep original + _graded) for now
    # Named spots win over online lookups. radius in km.
    "spots": [
        # {"name": "Bonneville Salt Flats", "lat": 40.760, "lon": -113.890, "radius_km": 5}
    ],
}


def load_config() -> dict:
    p = config_dir() / "config.json"
    cfg = dict(DEFAULT_CONFIG)
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    (config_dir() / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def remember_destination(cfg: dict, dest: str) -> None:
    rec = [d for d in cfg.get("recent_destinations", []) if d != dest]
    cfg["recent_destinations"] = [dest] + rec[:9]
    save_config(cfg)


# --------------------------------------------------------------------------- metadata

@dataclass
class MediaInfo:
    path: Path
    when: dt.datetime | None = None
    lat: float | None = None
    lon: float | None = None
    gps_source: str = ""
    duration: float = 0.0          # seconds, videos only
    sidecars: list[Path] = field(default_factory=list)

    @property
    def has_gps(self) -> bool:
        return self.lat is not None and self.lon is not None and not (self.lat == 0 and self.lon == 0)


_EXIFTOOL = shutil.which("exiftool")


def _exiftool(path: Path) -> dict:
    if not _EXIFTOOL:
        return {}
    try:
        out = subprocess.run(
            [_EXIFTOOL, "-j", "-n", "-ee", "-api", "LargeFileSupport=1",
             "-GPSLatitude", "-GPSLongitude", "-DateTimeOriginal", "-CreateDate",
             "-GPSPosition", str(path)],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        data = json.loads(out.stdout or "[]")
        return data[0] if data else {}
    except Exception:
        return {}


def _parse_exif_dt(s) -> dt.datetime | None:
    if not s or not isinstance(s, str):
        return None
    m = re.match(r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", s)
    if not m:
        return None
    try:
        d = dt.datetime(*map(int, m.groups()))
        return None if d.year < 2000 else d
    except ValueError:
        return None


def _pillow_exif(path: Path):
    try:
        from PIL import Image
    except ImportError:
        return None, None, None
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            when = _parse_exif_dt(exif.get_ifd(0x8769).get(36867) or exif.get(306))
            gps = exif.get_ifd(0x8825)
            if not gps or 2 not in gps or 4 not in gps:
                return when, None, None

            def conv(v, ref):
                d, m, s = (float(x) for x in v)
                val = d + m / 60 + s / 3600
                return -val if ref in ("S", "W") else val

            return when, conv(gps[2], gps.get(1, "N")), conv(gps[4], gps.get(3, "E"))
    except Exception:
        return None, None, None


_SRT_LAT = re.compile(r"\[latitude\s*:\s*(-?\d+\.\d+)\]", re.I)
_SRT_LON = re.compile(r"\[long(?:i|t)tude\s*:\s*(-?\d+\.\d+)\]", re.I)
_SRT_GPS_OLD = re.compile(r"GPS\s*\((-?\d+\.\d+),\s*(-?\d+\.\d+)")  # older DJI: GPS(lon, lat, alt)
_SRT_DT = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def parse_srt(path: Path):
    """DJI subtitle telemetry. Returns (when, lat, lon) from the first valid fix."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")[:20000]
    except Exception:
        return None, None, None
    when = None
    m = _SRT_DT.search(text)
    if m:
        when = _parse_exif_dt(m.group(1))
    for la, lo in zip(_SRT_LAT.finditer(text), _SRT_LON.finditer(text)):
        lat, lon = float(la.group(1)), float(lo.group(1))
        if lat or lon:
            return when, lat, lon
    for g in _SRT_GPS_OLD.finditer(text):
        lon, lat = float(g.group(1)), float(g.group(2))
        if lat or lon:
            return when, lat, lon
    return when, None, None


def _mp4_atoms(f, start, end):
    f.seek(start)
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        size, kind = struct.unpack(">I4s", hdr)
        hlen = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            hlen = 16
        elif size == 0:
            size = end - pos
        if size < hlen:
            return
        yield kind, pos + hlen, pos + size
        pos += size


def parse_mp4(path: Path):
    """Pure-python read of creation time and duration (mvhd) and ISO 6709 location (udta/©xyz)."""
    when = lat = lon = None
    duration = 0.0
    try:
        with open(path, "rb") as f:
            end = os.fstat(f.fileno()).st_size
            for kind, s, e in _mp4_atoms(f, 0, end):
                if kind != b"moov":
                    continue
                for k2, s2, e2 in list(_mp4_atoms(f, s, e)):
                    if k2 == b"mvhd":
                        f.seek(s2)
                        ver = f.read(1)[0]
                        f.read(3)
                        fmt, n = (">Q", 8) if ver == 1 else (">I", 4)
                        secs = struct.unpack(fmt, f.read(n))[0]
                        f.read(n)  # modification time
                        timescale = struct.unpack(">I", f.read(4))[0]
                        dur = struct.unpack(fmt, f.read(n))[0]
                        if timescale:
                            duration = dur / timescale
                        if secs > 0:
                            # DJI writes local time here on most models
                            d = dt.datetime(1904, 1, 1) + dt.timedelta(seconds=secs)
                            if d.year >= 2000:
                                when = d
                    elif k2 == b"udta":
                        for k3, s3, e3 in list(_mp4_atoms(f, s2, e2)):
                            if k3 == b"\xa9xyz":
                                f.seek(s3 + 4)
                                txt = f.read(e3 - s3 - 4).decode("latin-1", "ignore")
                                m = re.search(r"([+-]\d+\.\d+)([+-]\d+\.\d+)", txt)
                                if m:
                                    lat, lon = float(m.group(1)), float(m.group(2))
                break
    except Exception:
        pass
    return when, lat, lon, duration


_FNAME_DT = re.compile(r"(20\d{2})(\d{2})(\d{2})_?(\d{2})(\d{2})(\d{2})")


def read_info(path: Path) -> MediaInfo:
    info = MediaInfo(path=path)
    ext = path.suffix.lower()

    ex = _exiftool(path)
    if ex:
        info.when = _parse_exif_dt(ex.get("DateTimeOriginal")) or _parse_exif_dt(ex.get("CreateDate"))
        la, lo = ex.get("GPSLatitude"), ex.get("GPSLongitude")
        if isinstance(la, list):
            la = la[0]
        if isinstance(lo, list):
            lo = lo[0]
        if isinstance(la, (int, float)) and isinstance(lo, (int, float)) and (la or lo):
            info.lat, info.lon, info.gps_source = float(la), float(lo), "exiftool"

    if ext in PHOTO_EXT and not info.has_gps:
        w, la, lo = _pillow_exif(path)
        info.when = info.when or w
        if la is not None:
            info.lat, info.lon, info.gps_source = la, lo, "exif"

    if ext in VIDEO_EXT:
        srt = next((p for p in (path.with_suffix(".SRT"), path.with_suffix(".srt")) if p.exists()), None)
        if srt:
            info.sidecars.append(srt)
            if not info.has_gps:
                w, la, lo = parse_srt(srt)
                info.when = info.when or w
                if la is not None:
                    info.lat, info.lon, info.gps_source = la, lo, "srt"
        lrf = next((p for p in (path.with_suffix(".LRF"), path.with_suffix(".lrf")) if p.exists()), None)
        if lrf:
            info.sidecars.append(lrf)
        w, la, lo, info.duration = parse_mp4(path)
        if not info.has_gps or not info.when:
            info.when = info.when or w
            if not info.has_gps and la is not None and (la or lo):
                info.lat, info.lon, info.gps_source = la, lo, "mp4"

    if not info.when:
        m = _FNAME_DT.search(path.name)
        if m:
            try:
                info.when = dt.datetime(*map(int, m.groups()))
            except ValueError:
                pass
    if not info.when:
        info.when = dt.datetime.fromtimestamp(path.stat().st_mtime)
    return info


def fill_missing_gps(infos: list[MediaInfo], max_gap=dt.timedelta(hours=2)) -> None:
    """Videos without GPS borrow the location of the closest-in-time file that has it."""
    located = [i for i in infos if i.has_gps]
    for i in infos:
        if i.has_gps or not located:
            continue
        best = min(located, key=lambda j: abs(j.when - i.when))
        if abs(best.when - i.when) <= max_gap:
            i.lat, i.lon, i.gps_source = best.lat, best.lon, f"nearby ({best.path.name})"


# --------------------------------------------------------------------------- places

def haversine_km(a_lat, a_lon, b_lat, b_lon) -> float:
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def coord_label(lat, lon) -> str:
    return f"{abs(lat):.3f}{'N' if lat >= 0 else 'S'} {abs(lon):.3f}{'E' if lon >= 0 else 'W'}"


class PlaceNamer:
    """Turns GPS into a folder-friendly place name. Named spots > cache > OpenStreetMap > coordinates."""

    def __init__(self, cfg: dict, log=print):
        self.cfg = cfg
        self.log = log
        self.cache_path = config_dir() / "place_cache.json"
        try:
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            self.cache = {}
        self._last_call = 0.0
        self._offline = False

    def name(self, lat, lon) -> str:
        for s in self.cfg.get("spots", []):
            if haversine_km(lat, lon, s["lat"], s["lon"]) <= s.get("radius_km", 2):
                return s["name"]
        key = f"{lat:.2f},{lon:.2f}"  # ~1 km buckets
        if key in self.cache:
            return self.cache[key]
        name = None
        if self.cfg.get("online_place_names", True) and not self._offline:
            name = self._nominatim(lat, lon)
        if not name:
            return coord_label(lat, lon)
        self.cache[key] = name
        try:
            self.cache_path.write_text(json.dumps(self.cache, indent=1), encoding="utf-8")
        except Exception:
            pass
        return name

    def _nominatim(self, lat, lon):
        wait = 1.1 - (time.time() - self._last_call)  # OSM usage policy: max 1 request/sec
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()
        q = urllib.parse.urlencode({"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 12})
        req = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/reverse?{q}",
            headers={"User-Agent": "DJISorter/1.0 (personal drone footage organizer)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            self.log(f"Place lookup offline, using coordinates ({e.__class__.__name__})")
            self._offline = True
            return None
        a = data.get("address", {})
        local = next((a[k] for k in ("city", "town", "village", "hamlet", "national_park",
                                      "park", "county") if a.get(k)), None)
        region = a.get("state") or a.get("country")
        parts = [p for p in (local, region) if p]
        return ", ".join(parts) or data.get("name") or None


def safe_folder(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s).strip(" .")
    return s or "Unknown"


# --------------------------------------------------------------------------- planning

def collect(paths: list[str | Path], skip_lrf=True) -> list[Path]:
    """Expand dropped files/folders into main media files (sidecars ride along with their video)."""
    out, seen = [], set()
    for p in map(Path, paths):
        files = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file()]
        for f in files:
            ext = f.suffix.lower()
            if ext not in PHOTO_EXT | VIDEO_EXT or f.name.startswith("._"):
                continue
            if f.resolve() not in seen:
                seen.add(f.resolve())
                out.append(f)
        # loose SRT/LRF dropped without their video are handled with the video only
    return sorted(out)


@dataclass
class PlanItem:
    src: Path
    dest: Path
    info: MediaInfo


def plan(files: list[Path], dest_root: Path, cfg: dict, log=print, progress=None) -> list[PlanItem]:
    infos = []
    for n, f in enumerate(files, 1):
        infos.append(read_info(f))
        if progress:
            progress(n, len(files), f"Reading {f.name}")
    fill_missing_gps(infos)
    namer = PlaceNamer(cfg, log)
    items = []
    for n, i in enumerate(infos, 1):
        place = namer.name(i.lat, i.lon) if i.has_gps else "Unknown Location"
        folder = cfg.get("folder_format", DEFAULT_CONFIG["folder_format"]).format(
            date=i.when.strftime("%Y-%m-%d"), place=place,
            year=i.when.strftime("%Y"), month=i.when.strftime("%m"),
        )
        target = dest_root / Path(*[safe_folder(part) for part in re.split(r"[\\/]", folder)])
        items.append(PlanItem(i.path, target / i.path.name, i))
        for sc in i.sidecars:
            if skip_lrf_for(cfg) and sc.suffix.lower() == ".lrf":
                continue
            items.append(PlanItem(sc, target / sc.name, i))
        if progress:
            progress(n, len(infos), f"Locating {i.path.name}")
    return items


def skip_lrf_for(cfg):
    return cfg.get("skip_lrf", True)


class Progress:
    """Time-remaining estimate across two kinds of work: copying (bytes) and encoding (video seconds).

    Each kind learns its own speed as it goes, so the estimate settles after the first file or two.
    """

    def __init__(self, copy_bytes: int, encode_secs: float, callback=None):
        self.total_copy, self.total_enc = max(copy_bytes, 1), encode_secs
        self.done_copy = self.done_enc = 0.0
        self.copy_rate = 80e6     # bytes/sec guess until measured (typical SD card reader)
        self.enc_rate = 0.5       # video-seconds per second guess for 4K -> H.264
        self.copy_time = self.enc_time = 0.0
        self.callback = callback
        self.start = time.time()
        self._last = 0.0

    def add_copy(self, nbytes, elapsed):
        self.done_copy += nbytes
        self.copy_time += elapsed
        if self.copy_time > 0.5:
            self.copy_rate = self.done_copy / self.copy_time

    def add_encode(self, secs, elapsed):
        self.done_enc += secs
        self.enc_time += elapsed
        if self.enc_time > 2 and self.done_enc > 0:
            self.enc_rate = self.done_enc / self.enc_time

    def remaining(self) -> float:
        return (max(self.total_copy - self.done_copy, 0) / self.copy_rate
                + max(self.total_enc - self.done_enc, 0) / self.enc_rate)

    def fraction(self) -> float:
        total = self.total_copy / self.copy_rate + self.total_enc / self.enc_rate
        done = self.done_copy / self.copy_rate + self.done_enc / self.enc_rate
        return min(done / total, 1.0) if total else 1.0

    def report(self, msg, force=False):
        now = time.time()
        if self.callback and (force or now - self._last > 0.25):
            self._last = now
            self.callback(self.fraction(), self.remaining(), msg)


def fmt_eta(secs: float) -> str:
    secs = int(secs)
    if secs < 60:
        return "less than a minute left"
    h, m = divmod((secs + 59) // 60, 60)
    return f"about {h} hr {m} min left" if h else f"about {m} min left"


def _copy_with_progress(src: Path, dst: Path, prog: Progress, label: str):
    tmp = dst.with_name(dst.name + ".part")
    with open(src, "rb") as fi, open(tmp, "wb") as fo:
        while True:
            t = time.time()
            chunk = fi.read(8 * 1024 * 1024)
            if not chunk:
                break
            fo.write(chunk)
            prog.add_copy(len(chunk), time.time() - t)
            prog.report(label)
    shutil.copystat(src, tmp)
    os.replace(tmp, dst)


def _unique(path: Path) -> Path:
    dest, k = path, 1
    while dest.exists():
        dest = path.with_name(f"{path.stem}_{k}{path.suffix}")
        k += 1
    return dest


FB_SIZES = {
    "4:5 vertical (1080x1350)": (1080, 1350),
    "1:1 square (1080x1080)": (1080, 1080),
    "9:16 Reels/Stories (1080x1920)": (1080, 1920),
    "16:9 landscape (1920x1080)": (1920, 1080),
}


def fb_root(dest_root: Path, cfg: dict) -> Path:
    return Path(cfg.get("fb_folder") or (dest_root / "Facebook"))


def execute(items: list[PlanItem], cfg: dict, log=print, progress=None, dest_root: Path | None = None) -> dict:
    """progress(fraction 0-1, seconds_left, message)."""
    move = cfg.get("mode") == "move"
    stats = {"copied": 0, "skipped": 0, "failed": 0, "graded": 0, "facebook": 0}
    lut = Path(cfg["lut_path"]) if cfg.get("lut_enabled") and cfg.get("lut_path") else None
    fb = bool(cfg.get("fb_enabled")) and dest_root is not None
    fb_size = FB_SIZES.get(cfg.get("fb_aspect"), (1080, 1350))
    ffmpeg = shutil.which("ffmpeg")
    if (lut or fb) and not ffmpeg:
        log("ffmpeg is not installed, so LUT and Facebook video copies are skipped (see SETUP.md)")
    if lut and not lut.exists():
        log(f"LUT file not found, skipping LUT: {lut}")
        lut = None

    main_videos = [it for it in items if it.src == it.info.path and it.src.suffix.lower() in VIDEO_EXT]
    enc_secs = 0.0
    if ffmpeg:
        per = lambda it: it.info.duration or 30.0
        enc_secs = sum(per(it) for it in main_videos) * ((1 if lut else 0) + (1 if fb else 0))
    copy_bytes = sum(it.src.stat().st_size for it in items if it.src.exists())
    prog = Progress(copy_bytes, enc_secs, progress)

    for n, it in enumerate(items, 1):
        label = f"{'Moving' if move else 'Copying'} {it.src.name} ({n} of {len(items)})"
        prog.report(label, force=True)
        try:
            it.dest.parent.mkdir(parents=True, exist_ok=True)
            size = it.src.stat().st_size
            if it.dest.exists() and it.dest.stat().st_size == size:
                stats["skipped"] += 1
                prog.add_copy(size, size / prog.copy_rate)  # don't let skips distort the speed
                log(f"Already there, skipped: {it.dest}")
            else:
                dest = _unique(it.dest)
                same_drive = move and os.path.splitdrive(str(it.src.resolve()))[0].lower() == \
                    os.path.splitdrive(str(dest.resolve()))[0].lower() and os.name == "nt"
                if same_drive:
                    shutil.move(str(it.src), str(dest))
                    prog.add_copy(size, 0.01)
                else:
                    _copy_with_progress(it.src, dest, prog, label)
                    if move:
                        it.src.unlink()
                it.dest = dest
                stats["copied"] += 1

            is_main_video = it in main_videos
            if ffmpeg and is_main_video and lut:
                out = it.dest.with_name(f"{it.dest.stem}_graded.mp4")
                if not out.exists() and encode(ffmpeg, it.dest, out, it.info.duration, prog, log,
                                               f"Applying LUT to {it.dest.name}", lut=lut):
                    stats["graded"] += 1
                elif out.exists():
                    prog.add_encode(it.info.duration or 30.0, 0.01)
            if fb:
                fb_dir = fb_root(dest_root, cfg) / it.dest.parent.relative_to(dest_root)
                fb_dir.mkdir(parents=True, exist_ok=True)
                if ffmpeg and is_main_video:
                    out = fb_dir / f"{it.dest.stem}_fb.mp4"
                    if out.exists():
                        prog.add_encode(it.info.duration or 30.0, 0.01)
                    elif encode(ffmpeg, it.dest, out, it.info.duration, prog, log,
                                f"Making Facebook copy of {it.dest.name}", lut=lut, size=fb_size, audio=False):
                        stats["facebook"] += 1
                elif it.src.suffix.lower() in PHOTO_EXT and it.src.suffix.lower() != ".dng":
                    out = fb_dir / it.dest.name
                    if not out.exists():
                        shutil.copy2(it.dest, out)
                        stats["facebook"] += 1
        except Exception as e:
            stats["failed"] += 1
            log(f"FAILED {it.src.name}: {e}")
    prog.report("Finished", force=True)
    return stats


# --------------------------------------------------------------------------- video encoding

def encode(ffmpeg: str, src: Path, out: Path, duration: float, prog: Progress, log, label,
           lut: Path | None = None, size: tuple[int, int] | None = None, audio=True) -> bool:
    """Re-encode src to H.264 MP4, optionally with a LUT, a center crop to size, and no audio."""
    import tempfile
    work = Path(tempfile.mkdtemp(prefix="djienc"))
    filters = []
    if size:
        w, h = size
        filters += [f"scale={w}:{h}:force_original_aspect_ratio=increase", f"crop={w}:{h}", "setsar=1"]
    if lut:
        # ffmpeg filter strings choke on drive colons and quotes, so use a plain-named copy
        shutil.copy(lut, work / "lut.cube")
        filters.append("lut3d=lut.cube")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y",
           "-i", str(src.resolve())]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    if size:  # Facebook: 30/60 fps max, sensible bitrate cap
        cmd += ["-fpsmax", "60", "-c:v", "libx264", "-profile:v", "high", "-crf", "20", "-preset", "medium",
                "-maxrate", "12M", "-bufsize", "24M"]
    else:
        cmd += ["-c:v", "libx264", "-crf", "16", "-preset", "medium"]
    cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", "-map_metadata", "0"]
    cmd += ["-c:a", "aac", "-b:a", "192k"] if audio else ["-an"]
    tmp = out.with_name(out.stem + ".part.mp4")
    cmd.append(str(tmp.resolve()))
    log(label + "...")
    expected = duration or 30.0
    done = 0.0
    t_last = time.time()
    p = subprocess.Popen(cmd, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for line in p.stdout:
        if line.startswith("out_time_us="):
            try:
                pos = min(int(line.split("=")[1]) / 1e6, expected)
            except ValueError:
                continue
            now = time.time()
            if pos > done:
                prog.add_encode(pos - done, now - t_last)
                done, t_last = pos, now
                prog.report(label)
    err = p.stderr.read()
    p.wait()
    shutil.rmtree(work, ignore_errors=True)
    if done < expected:
        prog.add_encode(expected - done, time.time() - t_last)
    if p.returncode != 0:
        tmp.unlink(missing_ok=True)
        log(f"Encoding failed for {src.name}: {err.strip()[-300:]}")
        return False
    os.replace(tmp, out)
    return True


# --------------------------------------------------------------------------- SD cards

def mounted_volumes() -> list[Path]:
    if sys.platform.startswith("win"):
        import string
        import ctypes
        vols = []
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if mask & (1 << i) and letter not in "AB":
                # 2 = removable drive; also accept fixed (3) for USB card readers that report as fixed
                t = ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\")
                if t in (2, 3) and letter != "C":
                    vols.append(Path(f"{letter}:\\"))
        return vols
    roots = [Path("/Volumes")] if sys.platform == "darwin" else [
        Path("/media") / os.environ.get("USER", ""), Path("/run/media") / os.environ.get("USER", ""), Path("/mnt")]
    vols = []
    for r in roots:
        try:
            vols += [p for p in r.iterdir() if p.is_dir()]
        except Exception:
            pass
    return vols


def looks_like_dji_card(vol: Path) -> bool:
    dcim = vol / "DCIM"
    try:
        if not dcim.is_dir():
            return False
        for sub in dcim.iterdir():
            if not sub.is_dir():
                continue
            if "DJI" in sub.name.upper() or "MEDIA" in sub.name.upper():
                for f in sub.iterdir():
                    if f.name.upper().startswith("DJI_"):
                        return True
    except Exception:
        return False
    return False
