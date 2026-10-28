"""Single-writer, recoverable ZIP/master publication.

The journal bridges filesystem replacements and SQLite commits. Recovery checks
content hashes, so a crash after CSV replacement cannot append the scene twice.
The backup is the previous committed master, not a mirror of the current one.
"""

from __future__ import annotations
import csv
import io
import json
import os
import zipfile
from pathlib import Path
from .common import atomic_json, digest, sync_dir, utcnow
from .schema import COLUMNS


def validate_archive(path, scene_id, reference_id):
    with zipfile.ZipFile(path) as z:
        prefix = f"S3_OLCI_{scene_id}"
        metadata_member = f"{prefix}_METADATA.json"
        csv_member = f"{prefix}_ALL_INDEX_STATS.csv"
        raster_member = f"{prefix}_ALL_SCIENCE_300m_CF.nc"
        expected = {metadata_member, csv_member, raster_member}
        if set(z.namelist()) != expected or len(z.namelist()) != 3:
            raise ValueError("Scene ZIP must contain metadata, statistics CSV and science NetCDF")
        if z.testzip():
            raise ValueError("Scene ZIP failed CRC verification")
        meta = json.loads(z.read(metadata_member))
        if meta["scene"]["id"] != scene_id or meta["reference_id"] != reference_id:
            raise ValueError("Scene ZIP identity does not match the queue")
        table = list(
            csv.DictReader(io.StringIO(z.read(csv_member).decode("utf-8")))
        )
        header = next(csv.reader(io.StringIO(z.read(csv_member).decode("utf-8"))))
        if header != COLUMNS:
            raise ValueError("Scene CSV schema mismatch")
        keys = set()
        for row in table:
            if row["scene_id"] != scene_id:
                raise ValueError("Unexpected scene/window identity in CSV")
            key = (row["scene_id"], row["Hylak_id"])
            if key in keys:
                raise ValueError("Duplicate lake row in scene CSV")
            keys.add(key)
        if len(table) != meta["statistics_rows"]:
            raise ValueError("CSV row count disagrees with metadata")
        if z.getinfo(raster_member).file_size == 0:
            raise ValueError("Empty NetCDF member")
        for member in (csv_member, raster_member):
            import hashlib

            h = hashlib.sha256()
            with z.open(member) as f:
                for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
                    h.update(chunk)
            if h.hexdigest() != meta["sha256"][member]:
                raise ValueError(f"Checksum mismatch: {member}")
    return meta, table


class Publisher:
    def __init__(self, workspace, ledger):
        self.ws = workspace
        self.ledger = ledger
        self.master = workspace.output / "master.csv"
        self.backup = workspace.output / "master_backup.csv"
        self.pending = workspace.output / ".master.pending.csv"
        self.journal = workspace.output / ".publication.json"

    def initialize(self):
        if self.journal.exists():
            self.recover()
        if not self.master.exists():
            # Header-only master is the initial successful version.
            if any(r["status"] == "done" for r in self.ledger.rows()):
                raise RuntimeError(
                    "Master CSV is missing although completed scenes exist"
                )
            with self.master.open("x", newline="") as f:
                csv.writer(f).writerow(COLUMNS)
                f.flush()
                os.fsync(f.fileno())
            if not self.backup.exists():
                os.link(self.master, self.backup)
            sync_dir(self.master.parent)
        with self.master.open(newline="") as f:
            if next(csv.reader(f), None) != COLUMNS:
                raise ValueError("Master CSV schema mismatch")

    def recover(self):
        if not self.journal.exists():
            return
        j = json.loads(self.journal.read_text())
        # Replacing master is idempotent: never rotate the backup again if the
        # new master already reached disk before an interrupted ledger commit.
        current = digest(self.master) if self.master.exists() else None
        if current != j["new_sha256"]:
            if (
                current != j["old_sha256"]
                or not self.pending.exists()
                or digest(self.pending) != j["new_sha256"]
            ):
                raise RuntimeError(
                    "Publication recovery requires intact master and pending CSV; files left untouched"
                )
            previous = self.backup.with_name(".master.previous.csv")
            previous.unlink(missing_ok=True)
            os.link(self.master, previous)
            os.replace(previous, self.backup)
            sync_dir(self.master.parent)
            os.replace(self.pending, self.master)
            sync_dir(self.master.parent)
        archive = Path(j["archive"])
        if not archive.is_file() or digest(archive) != j["archive_sha256"]:
            raise RuntimeError("Publication archive is missing or changed")
        self.ledger.update(
            j["scene_id"],
            status="done",
            stage="complete",
            error=None,
            archive=j["archive"],
            archive_sha256=j["archive_sha256"],
            rows=j["rows"],
        )
        self.journal.unlink()
        sync_dir(self.master.parent)

    def publish(self, scene, reference_id, candidate):
        self.initialize()
        sid = scene["id"]
        record = self.ledger.scene(sid)
        final = self.ws.archive(scene)
        if record["status"] == "done":
            if not final.exists() or digest(final) != record["archive_sha256"]:
                raise RuntimeError("Completed scene archive changed or is missing")
            return
        meta, rows = validate_archive(candidate, sid, reference_id)
        final.parent.mkdir(parents=True, exist_ok=True)
        if Path(candidate) != final:
            # Worker already writes candidate on output filesystem, so rename is atomic.
            os.replace(candidate, final)
            sync_dir(final.parent)
        self.ledger.update(
            sid, status="publishing", stage="master_csv", archive=str(final)
        )
        old_hash = digest(self.master)
        with self.master.open(newline="") as src, self.pending.open(
            "w", newline=""
        ) as dst:
            reader = csv.DictReader(src)
            writer = csv.DictWriter(dst, fieldnames=COLUMNS)
            writer.writeheader()
            for row in reader:
                if row["scene_id"] == sid:
                    raise RuntimeError(
                        "Scene already occurs in master without completed ledger state; refusing duplicate append"
                    )
                writer.writerow(row)
            writer.writerows(rows)
            dst.flush()
            os.fsync(dst.fileno())
        atomic_json(
            self.journal,
            {
                "scene_id": sid,
                "archive": str(final),
                "archive_sha256": digest(final),
                "rows": len(rows),
                "old_sha256": old_hash,
                "new_sha256": digest(self.pending),
                "created": utcnow(),
            },
        )
        self.recover()
