#!/usr/bin/env python3
"""Install byte-identical Phoenix artwork in the bundle's MAVProxy resources.

Presentation only: never import TDOC, send MAVLink, reset or modify flight models.
Run with FlightStack/ardupilot/venv-ardupilot-debian13/bin/python.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
COLORS = {"red": "redphoniex.png", "green": "greenphoniex.png",
          "blue": "bluephoniex.png", "purple": "purplephoniex.png",
          "orange": "orengephoniex.png"}
AIRCRAFT = ("plane", "copter", "heli", "singlecopter", "blimp")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data, mode=0o644):
    fd, name = tempfile.mkstemp(prefix=".phoenix-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def install(flightstack, *, check=False):
    flightstack = Path(flightstack).resolve(strict=True)
    venv = flightstack / "ardupilot/venv-ardupilot-debian13"
    if Path(sys.prefix).resolve() != venv.resolve():
        raise ValueError("Run with this FlightStack's native venv Python")
    from MAVProxy.modules import mavproxy_map
    from MAVProxy.modules.mavproxy_map import mp_tile
    from PIL import Image
    import cv2
    import numpy as np

    module = Path(mavproxy_map.__file__).resolve()
    if not module.is_relative_to(venv.resolve()):
        raise ValueError("MAVProxy imported from outside this bundle")
    data_root = module.parent / "data"
    manifest = json.loads((ROOT / "assets_manifest.json").read_text())
    expected = {row["name"]: row["sha256"] for row in manifest["assets"]}
    targets = {}
    for color, name in COLORS.items():
        raw = (ROOT / "assets" / name).read_bytes()
        if sha(raw) != expected[name]:
            raise ValueError("Phoenix asset hash mismatch: " + name)
        with Image.open(ROOT / "assets" / name) as im:
            im.verify()
        for kind in AIRCRAFT:
            targets[data_root / (color + kind + ".png")] = raw
        targets[data_root / (color + "phoenix.png")] = raw

    # Relocated console scripts may otherwise run an unrelated external venv.
    launcher = venv / "bin/mavproxy.py"
    original = launcher.read_bytes()
    first, separator, body = original.partition(b"\n")
    if not first.startswith(b"#!") or not separator:
        raise ValueError("Unexpected MAVProxy launcher format")
    python = str(venv / "bin/python")
    if any(c.isspace() for c in python) or len(python.encode()) > 240:
        raise ValueError("Native Python path cannot be used as a kernel shebang")
    targets[launcher] = ("#!" + python + "\n").encode() + body
    state = flightstack / "runtime/phoenix_map_icons"
    backup = state / "backups"
    changed = []
    if not check:
        backup.mkdir(parents=True, exist_ok=True)
        for path, raw in targets.items():
            current = path.read_bytes() if path.exists() else None
            if current == raw:
                continue
            if current is not None:
                saved = backup / (sha(current) + ".bin")
                if not saved.exists():
                    atomic_write(saved, current)
            mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
            atomic_write(path, raw, mode)
            changed.append(str(path.relative_to(flightstack)))

    records = []
    for path, raw in targets.items():
        actual = path.read_bytes()
        if actual != raw:
            raise ValueError("Installed presentation differs: " + str(path))
        records.append({"path": str(path), "sha256": sha(actual), "bytes": len(actual)})
    decoded = []
    for color, name in COLORS.items():
        source = cv2.imread(str(ROOT / "assets" / name), cv2.IMREAD_COLOR)
        for kind in AIRCRAFT:
            alias = color + kind + ".png"
            image = mp_tile.mp_icon(alias)
            if image is None or not np.array_equal(image, source):
                raise ValueError("MAVProxy decoded icon mismatch: " + alias)
            decoded.append(alias)
    result = {"schema": "TDDT-Phoenix-map-icon-install-v1", "status": "PASS",
              "checked_at_utc": datetime.now(timezone.utc).isoformat(),
              "check_only": check, "flightstack": str(flightstack),
              "mavproxy_module": str(module), "mavproxy_version": importlib.metadata.version("MAVProxy"),
              "native_python": python, "source_manifest_sha256": sha((ROOT / "assets_manifest.json").read_bytes()),
              "aircraft_aliases": len(decoded), "decoded_pixel_matches": decoded,
              "changed_files": changed, "installed_files": records,
              "control_source_changed": False, "compiled_components_changed": False,
              "knowledge_modified": False, "non_aircraft_markers_changed": False}
    if not check:
        atomic_write(state / "INSTALL_MANIFEST.json", (json.dumps(result, indent=2) + "\n").encode())
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--flightstack-dir", type=Path, required=True)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    result = install(args.flightstack_dir, check=args.check)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: result[k] for k in ("status", "check_only", "mavproxy_version", "aircraft_aliases", "changed_files")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
