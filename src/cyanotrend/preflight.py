"""Report installation, source integrity, storage and credentials without a scene run."""

import importlib
import netrc
import os
from pathlib import Path
import platform
import shutil
from . import references


def check(workspace):
    errors = []
    deps = {}
    for name in (
        "numpy",
        "pandas",
        "geopandas",
        "shapely",
        "pyogrio",
        "pyproj",
        "scipy",
        "netCDF4",
        "requests",
        "boto3",
        "tqdm",
        "rasterio",
    ):
        try:
            mod = importlib.import_module(name)
            deps[name] = getattr(mod, "__version__", "available")
        except Exception as e:
            deps[name] = f"{type(e).__name__}: {e}"
            errors.append(f"Cannot import {name}: {e}")
    for relative in ("OCSSW_bash.env", "bin/l2gen"):
        if not (workspace.ocssw / relative).is_file():
            errors.append(f"Missing OCSSW {relative}")
    getanc = next(
        (
            p
            for p in [
                workspace.ocssw / "bin/getanc",
                workspace.ocssw / "scripts/getanc",
                workspace.ocssw / "scripts/getanc.py",
            ]
            if p.is_file()
        ),
        None,
    )
    if getanc is None:
        errors.append("Missing OCSSW getanc")
    for path in [workspace.ocssw / "bin/l2gen", getanc]:
        if path and path.exists() and not os.access(path, os.X_OK):
            errors.append(f"Not executable: {path}")
    for sensor in ("s3a", "s3b"):
        if not (workspace.ocssw / "share/olci" / sensor).is_dir():
            errors.append(f"Missing OLCI {sensor} sensor data")
    try:
        auth = Path.home() / ".netrc"
        if auth.stat().st_mode & 0o077:
            errors.append("Earthdata .netrc permissions must be 0600")
        credentials = netrc.netrc(str(auth)).authenticators("urs.earthdata.nasa.gov")
        if not credentials or not credentials[0] or not credentials[2]:
            errors.append("Earthdata .netrc entry is missing")
    except (OSError, netrc.NetrcParseError):
        errors.append("Earthdata .netrc is missing or invalid")
    try:
        m = references.manifest(workspace)
        if m["coverage"] != "global":
            errors.append("Notebook processing requires globally prepared reference sources")
        verification = references.verify(workspace)
        if not verification["ok"]:
            errors.append(
                "Reference files changed: "
                + ", ".join(verification["changed_or_missing"])
            )
    except (ValueError, OSError) as e:
        errors.append(str(e))
    if workspace.db.exists():
        from .ledger import Ledger
        from .identity import science_fingerprint

        with Ledger(workspace.db).connect() as c:
            old = c.execute(
                "SELECT value FROM workspace_meta WHERE key='science_fingerprint'"
            ).fetchone()
        if old and old[0] != science_fingerprint():
            errors.append(
                "Scientific implementation differs from saved plans; use a separate output workspace"
            )
    storage = {}
    for label, p in [("output", workspace.output), ("scratch", workspace.scratch)]:
        existing = next(q for q in (p, *p.parents) if q.exists())
        storage[label] = {
            "path": str(p),
            "available_gib": round(shutil.disk_usage(existing).free / 1024**3, 2),
        }
    return {
        "ok": not errors,
        "errors": errors,
        "dependencies": deps,
        "platform": platform.platform(),
        "storage": storage,
        "workers_default": 5,
        "attempts_total": 3,
        "attempts_scope": "Initial allowance; retry-failed explicitly grants three more attempts per failed scene",
        "validation_scope": "Local prerequisites and frozen reference integrity; no authenticated scene or scientific parity test",
    }
