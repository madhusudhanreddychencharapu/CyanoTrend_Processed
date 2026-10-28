"""Frozen scene plans, with administrative regions as discovery filters only."""

from __future__ import annotations
from datetime import date, timedelta
import hashlib
import json
import uuid
from .common import atomic_json, file_lock, utcnow, digest
from .ledger import Ledger
from . import references
from .identity import science_fingerprint


def date_chunks(start, end):
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if first > last:
        raise ValueError("Start date must not exceed end date")
    while first <= last:
        next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        stop = min(last, next_month - timedelta(days=1))
        yield first.isoformat(), stop.isoformat()
        first = stop + timedelta(days=1)


def create_plan(workspace, start, end, country=None, state_id=None, max_scenes=None, lake_region="WORLD"):
    from .core.catalogue import search_olci_l1_catalog, candidate_lakes_for_scene, scene_geometry
    from shapely import union_all

    if not country or not state_id:
        raise ValueError("Planning requires both --country and --state-id for the output folders")
    workspace.ensure()
    m = references.manifest(workspace)
    if m["coverage"] != "global":
        raise ValueError(
            "Notebook processing requires globally prepared reference sources; use a global reference directory"
        )
    list(date_chunks(start, end))  # Validate inclusive date order before catalogue access.
    lakes = references.load_lakes(workspace, region=lake_region)
    usa_checksum = digest(workspace.references / "cb_2025_us_state_5m.zip") if lake_region == "USA" else None
    pool = lakes
    bbox = None
    geom = None
    if state_id and not country:
        raise ValueError("A state selection also requires --country")
    if country:
        admin = references.boundaries(workspace, "ADM1" if state_id else "ADM0")
        chosen = admin[admin.country_iso3 == country.upper()]
        if state_id:
            chosen = chosen[chosen.shapeID.astype(str) == state_id]
        if chosen.empty:
            raise ValueError("Country/state not found; inspect regions first")
        names = chosen.shapeName.dropna().astype(str).unique()
        if len(names) != 1:
            raise ValueError("Selected state must have one unambiguous name for its output folder")
        archive_region = {"country": country.upper(), "state_id": state_id, "state_name": names[0]}
        geom = union_all(chosen.geometry)
        pool = lakes[lakes.intersects(geom)].copy()
        bbox = tuple(float(x) for x in geom.bounds)
    if pool.empty:
        raise ValueError("No eligible lakes in selected region")
    scenes = {}
    for a, b in [(start, end)]:
        # Match the notebook: cap raw catalogue results before regional filtering.
        for scene in search_olci_l1_catalog(a, b, bbox=bbox, max_products=max_scenes):
            if scene["satellite"] not in ("S3A", "S3B"):
                continue
            uuid.UUID(scene["id"])
            if scene["id"] in scenes:
                continue
            sg = scene_geometry(scene)
            if geom is not None and (sg is None or not sg.intersects(geom)):
                continue
            subset = candidate_lakes_for_scene(scene, pool)
            if subset.empty:
                continue
            all_hits = candidate_lakes_for_scene(scene, lakes)
            groups = all_hits[
                ["country_iso3", "state_id", "state_name"]
            ].drop_duplicates()
            scene["regions"] = [
                {
                    "country": r.country_iso3,
                    "state_id": r.state_id,
                    "state_name": r.state_name,
                }
                for r in groups.itertuples()
            ]
            scene["archive_region"] = dict(archive_region)
            # Validate the destination before saving a plan or starting processing.
            workspace.archive(scene)
            scene["candidate_lakes"] = int(len(subset))
            scene["lake_region"] = lake_region
            scene["lake_region_source_sha256"] = usa_checksum
            scenes[scene["id"]] = scene
    selection = {
        "start": start,
        "end": end,
        "country": country,
        "state_id": state_id,
        "max_scenes": max_scenes,
        "lake_region": lake_region,
        "lake_region_source_sha256": usa_checksum,
    }
    payload = {
        "schema": "cyanotrend-plan-1",
        "science_fingerprint": science_fingerprint(),
        "reference_id": m["reference_id"],
        "selection": selection,
        "scenes": list(scenes.values()),
        "created": utcnow(),
    }
    identity = {
        "selection": selection,
        "reference_id": m["reference_id"],
        "scene_ids": sorted(scenes),
        "science_fingerprint": science_fingerprint(),
    }
    payload["id"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode()
    ).hexdigest()[:24]
    path = workspace.output / "plans" / f"{payload['id']}.json"
    # Register only once the complete plan is atomically on disk. A service
    # also imports saved-but-unregistered plans after an interrupted CLI call.
    with file_lock(workspace.output / ".plan.lock"):
        if not path.exists():
            atomic_json(path, payload)
        else:
            payload = json.loads(path.read_text())
        Ledger(workspace.db).enqueue(payload, path)
    return {
        "plan": str(path),
        "id": payload["id"],
        "eligible_scenes": len(scenes),
        "selection": selection,
    }


def import_plans(workspace, ledger):
    with ledger.connect() as c:
        known = {r[0] for r in c.execute("SELECT id FROM plans")}
    for p in sorted((workspace.output / "plans").glob("*.json")):
        if p.stem in known:
            continue
        plan = json.loads(p.read_text())
        if plan.get("schema") != "cyanotrend-plan-1" or plan.get("id") != p.stem:
            raise ValueError(f"Invalid saved plan: {p.name}")
        if plan["reference_id"] != references.manifest(workspace)["reference_id"]:
            raise ValueError(f"Saved plan references a different dataset: {p.name}")
        if plan.get("science_fingerprint") != science_fingerprint():
            raise ValueError(
                f"Saved plan uses a different scientific implementation: {p.name}"
            )
        for scene in plan["scenes"]:
            uuid.UUID(scene["id"])
        ledger.enqueue(plan, p)
