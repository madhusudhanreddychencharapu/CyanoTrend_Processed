"""Small filesystem primitives shared by the queue and publisher."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import hashlib
import json
import os
import uuid


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("w") as f:
            json.dump(value, f, indent=2, default=str, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        sync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def file_lock(path, blocking=True):
    """OS-released lock; no PID-file guesses or expired-lease races."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise RuntimeError(f"Another process holds {path.name}") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@dataclass(frozen=True)
class Workspace:
    """Machine paths are CLI arguments, never repeated configuration exports."""

    output: Path
    references: Path
    scratch: Path
    ocssw: Path

    @classmethod
    def create(
        cls,
        output="Output Data",
        references="reference_data",
        scratch=".scratch",
        ocssw="~/cyanoTrend/software/ocssw",
    ):
        return cls(
            *(
                Path(x).expanduser().resolve()
                for x in (output, references, scratch, ocssw)
            )
        )

    @property
    def db(self):
        return self.output / "operations.sqlite3"

    def ensure(self):
        for p in (
            self.output,
            self.output / "plans",
            self.output / "logs",
            self.references,
            self.scratch,
        ):
            p.mkdir(parents=True, exist_ok=True)
        # Nested output/scratch paths would make cleanup unsafe.
        if (
            self.output == self.scratch
            or self.output.is_relative_to(self.scratch)
            or self.scratch.is_relative_to(self.output)
        ):
            raise ValueError("Output and scratch must be separate directory trees")
        if self.references == self.scratch or self.references.is_relative_to(
            self.scratch
        ):
            raise ValueError("Reference data cannot live inside scratch")

    def archive(self, scene):
        stamp = datetime.fromisoformat(scene["start"].replace("Z", "+00:00"))
        sid = str(uuid.UUID(scene["id"]))
        region = scene["archive_region"]
        country = region["country"]
        state = region["state_name"]
        for label, value in (("country", country), ("state", state)):
            if (
                not isinstance(value, str)
                or not value.strip()
                or value in {".", ".."}
                or any(c in value for c in ("/", "\\", "\x00"))
            ):
                raise ValueError(f"Invalid archive {label} folder name: {value!r}")
        return (
            self.output
            / "World"
            / country
            / state
            / f"{stamp.year:04d}"
            / f"{stamp.month:02d}"
            / f"{stamp.day:02d}"
            / f"{sid}.zip"
        )
