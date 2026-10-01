"""Maintainer tool: bump the version and write version.json with checksums of the app's code files."""
import hashlib, json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = ["dji_sorter.py", "sorter_core.py"]
vf = HERE / "version.json"
cur = json.loads(vf.read_text()) if vf.exists() else {"version": 0}
manifest = {
    "version": cur["version"] + 1,
    "launcher_min": int(sys.argv[2]) if len(sys.argv) > 2 else cur.get("launcher_min", 1),
    "notes": sys.argv[1] if len(sys.argv) > 1 else "",
    "files": {f: hashlib.sha256((HERE / f).read_bytes()).hexdigest() for f in FILES},
}
vf.write_text(json.dumps(manifest, indent=2) + "\n")
print(f"version {manifest['version']}")
