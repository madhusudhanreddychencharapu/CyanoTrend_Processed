"""One isolated notebook adaptive-window attempt. The coordinator owns master CSV publication."""

from __future__ import annotations
import argparse
import json
import math
import os
import signal
import time
import zipfile
from .common import Workspace, atomic_json, digest, file_lock, sync_dir
from .ledger import Ledger
from .identity import science_fingerprint
from .telemetry import Telemetry, redact


def clean_json(value):
    if isinstance(value, dict):
        return {str(k): clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def candidate_path(workspace, sid):
    return workspace.output / ".pending" / f"{sid}.zip"


def build_bundle(workspace, scene, reference_id, raster, stats, metadata):
    """Publish science, enriched stats and provenance in a three-file ZIP."""
    from .schema import COLUMNS
    path = candidate_path(workspace, scene["id"])
    path.parent.mkdir(exist_ok=True)
    temp = path.with_suffix(".zip.part")
    prefix = f"S3_OLCI_{scene['id']}"
    csv_path = raster.parent / f"{prefix}_ALL_INDEX_STATS.csv"
    if list(stats.columns) != COLUMNS:
        raise ValueError("Notebook export columns differ from the approved schema")
    stats.to_csv(csv_path, index=False)
    meta = {
        **metadata,
        "software_version": __import__("cyanotrend").__version__,
        "science_fingerprint": science_fingerprint(),
        "scene": scene,
        "reference_id": reference_id,
        "statistics_rows": len(stats),
        "coordinate_source": {"latitude": "HydroLAKES Pour_lat", "longitude": "HydroLAKES Pour_long"},
        "files": {"snap_all_science_netcdf": raster.name, "all_index_stats_csv": csv_path.name},
        "sha256": {raster.name: digest(raster), csv_path.name: digest(csv_path)},
    }
    with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        z.write(raster, raster.name)
        z.write(csv_path, csv_path.name)
        z.writestr(f"{prefix}_METADATA.json", json.dumps(clean_json(meta), indent=2, default=str, allow_nan=False))
    with temp.open("rb") as f:
        os.fsync(f.fileno())
    from .publication import validate_archive
    validate_archive(temp, scene["id"], reference_id)
    os.replace(temp, path)
    sync_dir(path.parent)
    return path


def process(workspace, sid, attempt):
    from . import references
    from .core import catalogue, compact, staging, windows, exports
    from .core.configuration import processing_config_hash
    import pandas as pd
    from . import execution

    ledger = Ledger(workspace.db)
    workspace.ensure()
    scratch = workspace.scratch / sid
    with file_lock(workspace.output / ".scene-locks" / f"{sid}.lock"):
        record = ledger.scene(sid)
        # An old orphan that starts late must not execute a newer attempt.
        if record["attempts"] != attempt or record["status"] != "running":
            return
        scene = json.loads(record["scene_json"])
        if references.manifest(workspace)["reference_id"] != record["reference_id"]:
            raise ValueError("Worker reference data differs from saved plan")
        scratch.mkdir(parents=True, exist_ok=True)
        metrics = Telemetry(ledger, sid, attempt)
        try:
            with metrics.stage("total"):
                with metrics.stage("reference_loading"):
                    lakes = catalogue.candidate_lakes_for_scene(
                        scene, references.load_lakes(workspace, region=scene.get("lake_region", "WORLD"))
                    )
                if scene.get("lake_region") == "USA" and digest(workspace.references / "cb_2025_us_state_5m.zip") != scene.get("lake_region_source_sha256"):
                    raise ValueError("USA HydroLAKES selection boundary differs from saved plan")
                if lakes.empty:
                    ledger.update(sid, status="skipped_no_lakes", stage="skipped_no_lakes", rows=0)
                    return
                execution.configure(workspace, scratch, metrics)
                with metrics.stage("download"):
                    metrics.start = time.monotonic()
                    raw, sen3, method = staging.stage_scene_efficient(
                        scene, os.environ["CDSE_USERNAME"], os.environ["CDSE_PASSWORD"]
                    )
                    metrics.finish_download()
                from .core.l2gen import run_l2gen

                config_hash = processing_config_hash(1.3, scene.get("lake_region", "WORLD"))
                planned_windows = windows.plan_scene_windows(scene, lakes)
                files = []
                provenance = []
                for window in planned_windows:
                    wid = windows.window_id(window)
                    win_lakes = windows.lakes_for_window(lakes, window)
                    nc = scratch / f"{sid}_{wid}_{config_hash}_lakepixels.nc"
                    window_record = scratch / f"{wid}_provenance.json"
                    if nc.exists() and compact.validate_compact_product(nc, scene_id=sid, config_hash=config_hash):
                        # Notebook process_scene_once reuses completed compact windows after interruption.
                        files.append(nc)
                        provenance.append(json.loads(window_record.read_text()) if window_record.exists() else {"window_id": wid, "bbox": window, "reused_compact": True})
                        continue
                    with metrics.stage("l2_processing"):
                        l2, info = run_l2gen(sen3, process_full_scene=window is None, bbox=window, full_spectrum=False)
                    with metrics.stage("native_extraction"):
                        nc = scratch / f"{sid}_{wid}_{config_hash}_lakepixels.nc"
                        product, counts = compact.extract_lake_only_netcdf(
                            l2, scene, win_lakes, nc, window=window, config_hash=config_hash
                        )
                        if product is not None:
                            if not compact.validate_compact_product(nc, scene_id=sid, config_hash=config_hash):
                                raise ValueError("Native window validation failed")
                            files.append(nc)
                        provenance.append({"window_id": wid, "bbox": window, "l2gen": info, "native_observations": counts})
                        atomic_json(window_record, clean_json(provenance[-1]))
                        l2.unlink(missing_ok=True)
                if not files:
                    # process_scene_once completes with empty stats; its share action has no input.
                    ledger.update(sid, status="done", stage="complete_no_native", rows=0)
                    return
                # Share generation uses the same compact file order as the notebook's windows.
                exports.configure(lakes, files, scratch / "exports")
                exports.GLOBAL_STATE["config_hash"] = config_hash
                exports._REGISTRY = pd.DataFrame([{
                    "scene_id": sid, "scene_name": scene["name"],
                    "acquisition_start": scene["start"], "config_hash": config_hash,
                }])
                with metrics.stage("share_export"):
                    exports.create_scene_share_bundle(sid)
                    export_dir = exports.EXPORT_DIR / "scene_bundles"
                    meta = json.loads((export_dir / f"S3_OLCI_{sid}_METADATA.json").read_text())
                    # Keep numerical values in memory; do not round-trip floats through CSV.
                    _, table = exports._share_all_index_stats(sid, config_hash, exports._REGISTRY.iloc[0])
                    additional = lakes[["Hylak_id", "state_name", "state_id", "latitude", "longitude"]]
                    table = table.merge(additional, on="Hylak_id", how="left", validate="many_to_one", sort=False)
                    raster = exports.EXPORT_DIR / "snap_complete" / meta["files"]["snap_all_science_netcdf"]
                with metrics.stage("nasa_cyan_comparison"):
                    from .nasa_cyan import compare_scene
                    table, comparison, nasa_metadata = compare_scene(
                        table, lakes, scene["start"], workspace.references / "obpg_cyan"
                    )
                    meta["nasa_cyan_comparison"] = nasa_metadata
                with metrics.stage("packaging"):
                    matched_lakes = lakes[lakes.Hylak_id.isin(table.Hylak_id)]
                    unresolved = matched_lakes[matched_lakes.admin_match_status != "matched"] if "admin_match_status" in matched_lakes else matched_lakes.iloc[0:0]
                    meta.update({
                        "unresolved_state_assignments": unresolved[["Hylak_id", "admin_match_status"]].to_dict("records") if "admin_match_status" in unresolved else [],
                        "processing_scope": "full_scene" if planned_windows == [None] else "adaptive_lake_windows",
                        "window_provenance": provenance,
                        "native_compact_files_retained": False,
                        "lake_region": scene.get("lake_region", "WORLD"),
                        "min_lake_area_km2": 1.3,
                        "download_method": method,
                        "timings": ledger.scene(sid)["timings"],
                        "attempt": attempt,
                    })
                    build_bundle(workspace, scene, record["reference_id"], raster, table, meta)
            ledger.update(sid, stage="ready_to_publish")
        except BaseException as e:
            atomic_json(
                scratch / "failure.json",
                {"error": redact(f"{type(e).__name__}: {e}")[-4000:]},
            )
            raise
        finally:
            # Each worker deletes only its own temporary key and ancillary files.
            staging.release_temporary_s3_credentials(
                os.environ.get("CDSE_USERNAME", ""), os.environ.get("CDSE_PASSWORD", "")
            )


def main():
    from .execution import die_with_parent

    p = argparse.ArgumentParser()
    for name in ("output", "references", "scratch", "ocssw", "scene-id"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--attempt", type=int, required=True)
    a = p.parse_args()

    def stop(signum, frame):
        raise KeyboardInterrupt("Worker stopping")

    signal.signal(signal.SIGTERM, stop)
    die_with_parent()
    ws = Workspace.create(a.output, a.references, a.scratch, a.ocssw)
    try:
        process(ws, a.scene_id, a.attempt)
    except BaseException as e:
        print(redact(f"{type(e).__name__}: {e}"), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
