import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid
from cyanotrend.common import Workspace
from cyanotrend.ledger import Ledger
from cyanotrend.service import serve


class Service(unittest.TestCase):
    def test_five_subprocesses_complete_unique_scenes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = Workspace.create(
                root / "out", root / "refs", root / "scratch", root / "ocssw"
            )
            ws.ensure()
            ledger = Ledger(ws.db)
            scenes = [
                {
                    "id": str(uuid.uuid4()),
                    "name": "S3A_TEST",
                    "archive_region": {"country": "USA", "state_id": "mi", "state_name": "Michigan"},
                    "start": "2024-01-01T00:00:00Z",
                }
                for _ in range(6)
            ]
            ledger.enqueue(
                {
                    "id": "p",
                    "created": "now",
                    "reference_id": "ref",
                    "selection": {},
                    "scenes": scenes,
                },
                root / "p.json",
            )
            original = subprocess.Popen

            def launch(command, **kw):
                return original(
                    [
                        sys.executable,
                        str(Path(__file__).with_name("fake_worker.py")),
                        *command[3:],
                    ],
                    **kw
                )

            with patch("cyanotrend.preflight.check", return_value={"ok": True}), patch(
                "cyanotrend.service.import_plans"
            ), patch(
                "cyanotrend.service.subprocess.Popen", side_effect=launch
            ), patch.dict(
                os.environ, {"CDSE_USERNAME": "fixture", "CDSE_PASSWORD": "fixture"}
            ):
                result = serve(ws, workers=5, once=True, poll_seconds=0.02)
            self.assertEqual(result["counts"], {"done": 6})
            self.assertEqual(
                max(int(p.read_text()) for p in ws.output.glob("*.concurrency")), 5
            )
            self.assertEqual(len(list((ws.output / "World").rglob("*.zip"))), 6)


if __name__ == "__main__":
    unittest.main()
