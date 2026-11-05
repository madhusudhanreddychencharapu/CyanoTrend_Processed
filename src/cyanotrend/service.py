"""Five independent scene subprocesses and exactly one CSV publisher."""

from __future__ import annotations
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from .common import file_lock, utcnow
from .ledger import Ledger
from .publication import Publisher
from .worker import candidate_path
from .planning import import_plans


def cleanup(workspace, sid):
    """Remove only a validated scene-owned scratch directory."""
    import uuid

    uuid.UUID(sid)
    target = workspace.scratch / sid
    if target.is_symlink():
        raise RuntimeError("Refusing scene scratch symlink")
    if target.exists():
        shutil.rmtree(target)
    # Tiny operational lock files stay outside the science archive.


def failure_detail(workspace, sid):
    p = workspace.scratch / sid / "failure.json"
    if p.exists():
        return json.loads(p.read_text()).get("error", "Worker failed")
    return "Worker exited before producing a verified scene ZIP"


def reconcile(workspace, ledger, publisher, active_ids=()):
    """Finish ready ZIPs or retry abandoned attempts after their OS lock releases."""
    for record in ledger.rows():
        sid = record["id"]
        if sid in active_ids:
            continue
        if record["stage"] in {"complete_no_native", "skipped_no_lakes"}:
            cleanup(workspace, sid)
            continue
        if record["status"] not in ("running", "publishing"):
            continue
        try:
            with file_lock(
                workspace.output / ".scene-locks" / f"{sid}.lock", blocking=False
            ):
                scene = json.loads(record["scene_json"])
                candidate = candidate_path(workspace, sid)
                final = workspace.archive(scene)
                ready = (
                    candidate
                    if candidate.exists()
                    else final if final.exists() else None
                )
                if ready:
                    # Publication errors stop the coordinator, not an expensive
                    # scientific re-run. Pending/final ZIP remains recoverable.
                    started = time.monotonic()
                    stamp = utcnow()
                    publisher.publish(scene, record["reference_id"], ready)
                    ledger.timing(
                        sid,
                        record["attempts"],
                        "publication",
                        stamp,
                        time.monotonic() - started,
                        True,
                    )
                    cleanup(workspace, sid)
                else:
                    ledger.fail(sid, failure_detail(workspace, sid))
                    # Notebook KEEP_FAILED_SCRATCH=True: retain failure evidence for retries/inspection.
        except RuntimeError as e:
            if str(e).startswith("Another process holds"):
                continue
            raise


def serve(workspace, workers=5, once=False, poll_seconds=5):
    if workers < 1:
        raise ValueError("workers must be positive")
    workspace.ensure()
    from .preflight import check

    report = check(workspace)
    if not report["ok"]:
        raise RuntimeError("Preflight failed: " + "; ".join(report["errors"]))
    if not os.environ.get("CDSE_USERNAME") or not os.environ.get("CDSE_PASSWORD"):
        raise ValueError(
            "CDSE_USERNAME and CDSE_PASSWORD are required (or use the CLI secure prompt)"
        )
    ledger = Ledger(workspace.db)
    active = {}
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        print("Stopping new claims; allowing active scenes to finish.", flush=True)

    previous = {s: signal.signal(s, stop) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        with file_lock(workspace.output / ".service.lock", blocking=False):
            publisher = Publisher(workspace, ledger)
            publisher.initialize()
            reconcile(workspace, ledger, publisher)
            while True:
                import_plans(workspace, ledger)
                for sid, (proc, log) in list(active.items()):
                    if proc.poll() is not None:
                        log.close()
                        del active[sid]
                reconcile(workspace, ledger, publisher, active)
                while not stopping and len(active) < workers:
                    job = ledger.claim()
                    if not job:
                        break
                    sid = job["id"]
                    path = (
                        workspace.output
                        / "logs"
                        / f"{sid}_attempt{job['attempts']}.log"
                    )
                    log = path.open("w")
                    command = [
                        sys.executable,
                        "-m",
                        "cyanotrend.worker",
                        "--output",
                        str(workspace.output),
                        "--references",
                        str(workspace.references),
                        "--scratch",
                        str(workspace.scratch),
                        "--ocssw",
                        str(workspace.ocssw),
                        "--scene-id",
                        sid,
                        "--attempt",
                        str(job["attempts"]),
                    ]
                    try:
                        proc = subprocess.Popen(
                            command, stdout=log, stderr=subprocess.STDOUT
                        )
                    except BaseException:
                        log.close()
                        ledger.fail(sid, "Unable to start worker")
                        raise
                    active[sid] = (proc, log)
                    print(
                        f"{utcnow()} started {sid} attempt {job['attempts']}/{job['attempt_limit']} ({len(active)}/{workers} workers)",
                        flush=True,
                    )
                if not active:
                    if stopping:
                        break
                    pending = [
                        r
                        for r in ledger.rows()
                        if r["status"] in ("queued", "retry", "running", "publishing")
                    ]
                    if once and not pending:
                        break
                time.sleep(min(poll_seconds, 5))
    finally:
        # Unexpected coordinator errors must not leave unmanaged running jobs.
        for proc, log in active.values():
            if proc.poll() is None:
                proc.terminate()
        for proc, log in active.values():
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            log.close()
        for s, handler in previous.items():
            signal.signal(s, handler)
    return ledger.summary()
