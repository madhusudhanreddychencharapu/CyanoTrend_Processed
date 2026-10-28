"""Crash recovery and concurrency tests using no satellite services."""

import csv
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zipfile

from cyanotrend.common import Workspace
from cyanotrend.ledger import Ledger
from cyanotrend.publication import Publisher
from cyanotrend.planning import date_chunks
from cyanotrend.schema import COLUMNS
from cyanotrend.service import reconcile


class Operations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        r = Path(self.temp.name)
        self.ws = Workspace.create(r / "out", r / "refs", r / "scratch", r / "ocssw")
        self.ws.ensure()
        self.ledger = Ledger(self.ws.db)
        self.publisher = Publisher(self.ws, self.ledger)
        self.publisher.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def scene(self):
        return {
            "id": str(uuid.uuid4()),
            "start": "2024-08-14T12:00:00Z",
            "name": "S3A_TEST",
            "archive_region": {"country": "USA", "state_id": "mi", "state_name": "Michigan"},
            "regions": [
                {"country": "USA", "state_id": "one", "state_name": "One"},
                {"country": "USA", "state_id": "two", "state_name": "Two"},
            ],
        }

    def enqueue(self, scenes, pid=None):
        plan = {
            "id": pid or str(uuid.uuid4()),
            "created": "now",
            "reference_id": "ref",
            "selection": {},
            "scenes": scenes,
        }
        self.ledger.enqueue(plan, self.ws.output / "plans" / f"{plan['id']}.json")

    def archive(self, s, rows=1):
        p = self.ws.output / ".pending" / f"{s['id']}.zip"
        p.parent.mkdir(exist_ok=True)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=COLUMNS)
        writer.writeheader()
        for i in range(rows):
            writer.writerow(
                {"scene_id": s["id"], "Hylak_id": i + 1}
            )
        contents = {
            f"S3_OLCI_{s['id']}_ALL_SCIENCE_300m_CF.nc": b"synthetic fixture, not scientific NetCDF",
            f"S3_OLCI_{s['id']}_ALL_INDEX_STATS.csv": buf.getvalue().encode(),
        }
        meta = {
            "scene": s,
            "reference_id": "ref",
            "statistics_rows": rows,
            "sha256": {k: hashlib.sha256(v).hexdigest() for k, v in contents.items()},
        }
        with zipfile.ZipFile(p, "w") as z:
            for k, v in contents.items():
                z.writestr(k, v)
            z.writestr(f"S3_OLCI_{s['id']}_METADATA.json", json.dumps(meta))
        return p

    def test_claim_deduplicates_plans_and_concurrent_workers(self):
        scenes = [self.scene() for _ in range(30)]
        self.enqueue(scenes)
        self.enqueue(scenes)
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(lambda _: self.ledger.claim(), range(40)))
        ids = [r["id"] for r in results if r]
        self.assertEqual(len(ids), 30)
        self.assertEqual(len(set(ids)), 30)
        self.assertEqual(len(self.ledger.regions()), 2)
        self.assertEqual(self.ledger.regions()[0]["discovered"], 30)

    def test_requested_state_path_and_first_plan_survive_duplicate_plan(self):
        scene = self.scene()
        self.enqueue([scene])
        other = {**scene, "archive_region": {"country": "USA", "state_id": "oh", "state_name": "Ohio"}}
        self.enqueue([other])
        stored = json.loads(self.ledger.scene(scene["id"])["scene_json"])
        expected = self.ws.output / "World/USA/Michigan/2024/08/14" / f"{scene['id']}.zip"
        self.assertEqual(self.ws.archive(stored), expected)
        self.publisher.publish(stored, "ref", self.archive(stored))
        self.assertIsNone(self.ledger.claim())
        self.assertEqual(list((self.ws.output / "World").rglob("*.zip")), [expected])

    def test_three_attempts_total(self):
        s = self.scene()
        self.enqueue([s])
        for attempt in range(1, 4):
            job = self.ledger.claim()
            self.assertEqual(job["attempts"], attempt)
            self.ledger.fail(s["id"], "failure")
            self.ledger.update(s["id"], next_attempt=0)
        self.assertIsNone(self.ledger.claim())
        self.assertEqual(self.ledger.scene(s["id"])["status"], "failed")

    def test_backup_previous_master_and_duplicate_publication(self):
        a, b = self.scene(), self.scene()
        self.enqueue([a, b])
        self.publisher.publish(a, "ref", self.archive(a))
        first = self.publisher.master.read_bytes()
        self.publisher.publish(b, "ref", self.archive(b))
        self.assertEqual(self.publisher.backup.read_bytes(), first)
        self.publisher.publish(b, "ref", self.ws.archive(b))
        with self.publisher.master.open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)

    def test_crash_after_master_replaced_before_ledger_commit(self):
        s = self.scene()
        self.enqueue([s])
        p = self.archive(s)
        original = self.ledger.update

        def fail_done(sid, **kw):
            if kw.get("status") == "done":
                raise OSError("Simulated crash")
            return original(sid, **kw)

        with patch.object(self.ledger, "update", side_effect=fail_done):
            with self.assertRaises(OSError):
                self.publisher.publish(s, "ref", p)
        backup = self.publisher.backup.read_bytes()
        Publisher(self.ws, self.ledger).recover()
        self.assertEqual(self.publisher.backup.read_bytes(), backup)
        self.assertEqual(self.ledger.scene(s["id"])["status"], "done")
        with self.publisher.master.open() as f:
            self.assertEqual(len(list(csv.DictReader(f))), 1)

    def test_crash_before_master_replacement(self):
        s = self.scene()
        self.enqueue([s])
        p = self.archive(s)
        with patch.object(self.publisher, "recover", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                self.publisher.publish(s, "ref", p)
        self.assertTrue(self.publisher.journal.exists())
        Publisher(self.ws, self.ledger).recover()
        with self.publisher.master.open() as f:
            self.assertEqual(len(list(csv.DictReader(f))), 1)

    def test_restart_recovers_finished_worker_without_reprocessing(self):
        s = self.scene()
        self.enqueue([s])
        self.ledger.claim()
        self.archive(s)
        reconcile(self.ws, self.ledger, self.publisher)
        record = self.ledger.scene(s["id"])
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["attempts"], 1)

    def test_restart_retries_abandoned_worker(self):
        s = self.scene()
        self.enqueue([s])
        self.ledger.claim()
        reconcile(self.ws, self.ledger, self.publisher)
        self.assertEqual(self.ledger.scene(s["id"])["status"], "retry")

    def test_changed_reference_rejected(self):
        s = self.scene()
        self.enqueue([s])
        with self.assertRaises(ValueError):
            self.ledger.enqueue(
                {
                    "id": "bad",
                    "reference_id": "other",
                    "selection": {},
                    "created": "now",
                    "scenes": [s],
                },
                Path("bad"),
            )

    def test_different_lake_universes_cannot_share_master(self):
        scene = self.scene()
        self.enqueue([scene])
        with self.assertRaisesRegex(ValueError, "HydroLAKES universe differs"):
            self.ledger.enqueue({"id":"usa", "created":"now", "reference_id":"ref", "selection":{"lake_region":"USA"}, "scenes":[scene]}, Path("usa.json"))

    def test_old_master_schema_is_not_relabelled(self):
        self.publisher.master.write_text("scene_id,Hylak_id,native_polygon_pixels\n")
        before = self.publisher.master.read_bytes()
        with self.assertRaisesRegex(ValueError, "Master CSV schema mismatch"):
            self.publisher.initialize()
        self.assertEqual(self.publisher.master.read_bytes(), before)

    def test_failed_scratch_is_retained_like_notebook(self):
        scene = self.scene()
        self.enqueue([scene])
        self.ledger.claim()
        directory = self.ws.scratch / scene["id"]
        directory.mkdir()
        (directory / "failure.json").write_text('{"error":"synthetic failure"}')
        reconcile(self.ws, self.ledger, self.publisher)
        self.assertEqual(self.ledger.scene(scene["id"])["status"], "retry")
        self.assertTrue((directory / "failure.json").exists())

    def test_corrupt_zip_not_published(self):
        s = self.scene()
        self.enqueue([s])
        p = self.archive(s)
        with zipfile.ZipFile(p, "a") as z:
            z.writestr("unexpected.txt", "bad")
        with self.assertRaises(ValueError):
            self.publisher.publish(s, "ref", p)
        self.assertFalse(self.ws.archive(s).exists())

    def test_month_intervals_and_custom_dates(self):
        self.assertEqual(
            list(date_chunks("2024-02-28", "2024-03-02")),
            [("2024-02-28", "2024-02-29"), ("2024-03-01", "2024-03-02")],
        )
        with self.assertRaises(ValueError):
            list(date_chunks("2024-03-02", "2024-01-01"))

    def test_scratch_cannot_contain_output(self):
        with self.assertRaises(ValueError):
            Workspace.create(
                self.ws.scratch / "out",
                self.ws.references,
                self.ws.scratch,
                self.ws.ocssw,
            ).ensure()


if __name__ == "__main__":
    unittest.main()
