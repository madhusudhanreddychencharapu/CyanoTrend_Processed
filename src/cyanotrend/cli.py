"""Thin CLI: application functions remain usable by a future frontend."""

from __future__ import annotations
import argparse
import getpass
import json
import os
import sys
from .common import Workspace
from .ledger import Ledger


def positive(value):
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("Must be greater than zero")
    return n


def catalogue_cap(value):
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("Must be zero (all) or positive")
    return n


def parser():
    p = argparse.ArgumentParser(
        prog="cyanotrend",
        description="Notebook adaptive-window Sentinel-3 lake processing; five parallel scenes by default.",
    )
    p.add_argument(
        "--output",
        default="Output Data",
        help="Final ZIPs, master CSVs, plans and operational ledger",
    )
    p.add_argument(
        "--references",
        default="reference_data",
        help="One reusable copy of HydroLAKES and boundaries",
    )
    p.add_argument(
        "--scratch",
        default=".scratch",
        help="Temporary scene data; successful scratch removed, failed scratch retained as in notebook",
    )
    p.add_argument(
        "--ocssw",
        default="~/cyanoTrend/software/ocssw",
        help="Existing OCSSW installation; never installed automatically",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable report for scripts/frontends",
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "prepare",
        help="Download global HydroLAKES and ADM0/ADM1 once; resume interrupted setup",
    )
    sub.add_parser(
        "preflight",
        help="Check dependencies, OCSSW, credentials, reference checksums and free storage",
    )
    r = sub.add_parser("regions", help="List country or state boundary names and IDs")
    r.add_argument("--country", help="ISO3 country, e.g. USA; omit to list countries")
    q = sub.add_parser(
        "plan", help="Save a dated discovery plan and queue its distinct scenes"
    )
    q.add_argument("--start", required=True, help="First date, YYYY-MM-DD (inclusive)")
    q.add_argument("--end", required=True, help="Last date, YYYY-MM-DD (inclusive)")
    q.add_argument("--country", required=True, help="ISO3 country filter, e.g. USA")
    q.add_argument("--state-id", required=True, help="Exact state shapeID from regions")
    q.add_argument("--lake-region", choices=["WORLD", "USA"], default="WORLD", help="Loaded HydroLAKES universe, independent of planning state (notebook default: WORLD)")
    q.add_argument(
        "--max-scenes",
        type=catalogue_cap,
        help="Notebook catalogue cap before lake filtering; zero or omitted means all",
    )
    q = sub.add_parser(
        "run", help="Run queue; wait for future saved plans until stopped"
    )
    q.add_argument(
        "--workers", type=positive, default=5, help="Concurrent scenes (default: 5)"
    )
    q.add_argument(
        "--once",
        action="store_true",
        help="Exit when queued work and scheduled retries finish",
    )
    sub.add_parser("status", help="Unique-scene totals and active processing stages")
    q = sub.add_parser("scenes", help="List scene progress and staging transfer rates")
    q.add_argument(
        "--status",
        choices=["queued", "running", "retry", "publishing", "done", "failed", "skipped_no_lakes"],
    )
    q = sub.add_parser(
        "scene", help="Inspect one scene, attempts, errors, timings and archive"
    )
    q.add_argument("scene_id")
    sub.add_parser(
        "coverage",
        help="Discovered/processed scenes by country and state (regions overlap)",
    )
    q = sub.add_parser(
        "verify", help="Verify completed archive checksums and master CSV row identity"
    )
    q.add_argument("--scene-id")
    return p


def emit(value, as_json=False):
    if as_json:
        print(json.dumps(value, indent=2, default=str, allow_nan=False))
        return
    if isinstance(value, list):
        if not value:
            print("No records.")
            return
        keys = list(dict.fromkeys(k for row in value for k in row))
        rows = [[str(row.get(k, "")) for k in keys] for row in value]
        widths = [
            max(len(k), *(len(row[i]) for row in rows)) for i, k in enumerate(keys)
        ]
        print("  ".join(k.ljust(w) for k, w in zip(keys, widths)))
        for row in rows:
            print("  ".join(v.ljust(w) for v, w in zip(row, widths)))
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                print(k + ":")
                emit(v)
            elif isinstance(v, dict):
                print(k + ":")
                emit(v)
            else:
                print(f"{k}: {v}")
    else:
        print(value)


def scene_summary(row):
    return {
        "scene_id": row["id"],
        "status": row["status"],
        "attempts": row["attempts"],
        "stage": row["stage"],
        "downloaded_MiB": round(row["bytes_downloaded"] / 1024**2, 2),
        "staging_MiB_per_second": (
            round(row["bytes_downloaded"] / 1024**2 / row["download_seconds"], 3)
            if row["download_seconds"]
            else None
        ),
        "error": row["error"] or "",
    }


def verify(workspace, scene_id=None):
    import csv
    from .common import digest
    from .publication import validate_archive

    ledger = Ledger(workspace.db)
    errors = []
    counts = {}
    keys = set()
    master = workspace.output / "master.csv"
    if not master.exists():
        errors.append("master.csv is missing")
    else:
        from .schema import COLUMNS

        with master.open(newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != COLUMNS:
                errors.append("Master schema mismatch")
            for row in reader:
                key = (row["scene_id"], row["Hylak_id"])
                if key in keys:
                    errors.append(f"Duplicate master key: {key}")
                keys.add(key)
                counts[row["scene_id"]] = counts.get(row["scene_id"], 0) + 1
    checked = 0
    for row in ledger.rows("done"):
        if scene_id and row["id"] != scene_id:
            continue
        if row["stage"] == "complete_no_native":
            if counts.get(row["id"], 0):
                errors.append(f"Empty completed scene has master rows: {row['id']}")
            else:
                checked += 1
            continue
        from pathlib import Path

        p = Path(row["archive"])
        try:
            if not p.is_file() or digest(p) != row["archive_sha256"]:
                raise ValueError("archive checksum mismatch/missing")
            meta, table = validate_archive(p, row["id"], row["reference_id"])
            if len(table) != counts.get(row["id"], 0):
                raise ValueError("Master row count mismatch")
            checked += 1
        except (ValueError, OSError) as e:
            errors.append(f"{row['id']}: {e}")
    if scene_id and checked == 0 and not errors:
        errors.append("No completed scene matched the requested ID")
    return {"ok": not errors, "checked_archives": checked, "errors": errors}


def dispatch(a):
    ws = Workspace.create(a.output, a.references, a.scratch, a.ocssw)
    if a.command == "prepare":
        from .references import prepare

        m = prepare(ws)
        return {
            "reference_id": m["reference_id"],
            "boundary_files": len(m["boundaries"]),
            "coverage": m["coverage"],
        }
    if a.command == "preflight":
        from .preflight import check

        return check(ws)
    if a.command == "regions":
        from .references import boundaries

        g = boundaries(ws, "ADM1" if a.country else "ADM0")
        if a.country:
            g = g[g.country_iso3 == a.country.upper()]
        return (
            g.drop(columns="geometry")
            .rename(columns={"shapeName": "name", "shapeID": "id"})
            .to_dict("records")
        )
    if a.command == "plan":
        from .planning import create_plan

        return create_plan(ws, a.start, a.end, a.country, a.state_id, a.max_scenes, a.lake_region)
    if a.command == "run":
        from .service import serve

        if sys.stdin.isatty():
            if not os.environ.get("CDSE_USERNAME"):
                os.environ["CDSE_USERNAME"] = input("CDSE username: ").strip()
            if not os.environ.get("CDSE_PASSWORD"):
                os.environ["CDSE_PASSWORD"] = getpass.getpass(
                    "CDSE password (hidden): "
                )
        return serve(ws, a.workers, a.once)
    if a.command == "verify":
        return verify(ws, a.scene_id)
    ledger = Ledger(ws.db)
    if a.command == "status":
        return {
            **ledger.summary(),
            "active": [
                scene_summary(r)
                for r in ledger.rows()
                if r["status"] in ("running", "publishing")
            ],
        }
    if a.command == "scenes":
        return [scene_summary(r) for r in ledger.rows(a.status)]
    if a.command == "scene":
        result = ledger.scene(a.scene_id)
        result.pop("scene_json", None)
        return result
    if a.command == "coverage":
        return ledger.regions()
    raise ValueError("Unknown command")


def main(argv=None):
    p = parser()
    a = p.parse_args(argv)
    try:
        if a.json and a.command in ("prepare", "plan", "run"):
            from contextlib import redirect_stdout

            with redirect_stdout(sys.stderr):
                result = dispatch(a)
        else:
            result = dispatch(a)
        emit(result, a.json)
        return 1 if isinstance(result, dict) and result.get("ok") is False else 0
    except (ValueError, RuntimeError, OSError, KeyError) as e:
        from .telemetry import redact

        emit({"ok": False, "error": redact(str(e))}, a.json)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
