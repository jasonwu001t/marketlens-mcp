"""Store eviction and the portable lock (contract 5.1).

Under the lock, at start, after every put and every 10 minutes while
serving: delete results whose ``expires_at <= now``; delete orphan files
(Parquet or temp files with no sidecar) older than one hour; if the total
size is above the cap, delete oldest-first by ``created_at`` (any session)
until the total is at most 90 % of the cap; remove empty session folders.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import time
from collections.abc import Collection, Iterator
from dataclasses import dataclass, field
from datetime import datetime

LOCK_NAME = ".lock"
LOCK_STALE_SECONDS = 60.0
LOCK_RETRY_SECONDS = 0.05
ORPHAN_AGE_SECONDS = 3600.0
TARGET_FRACTION = 0.9


class StoreLockTimeout(RuntimeError):
    """The store lock stayed taken by another live process."""


@contextlib.contextmanager
def store_lock(results_dir: pathlib.Path, *, timeout: float = 5.0) -> Iterator[None]:
    """O_CREAT|O_EXCL lock file; retried every 50 ms up to ``timeout``; a lock
    older than 60 s is considered stale and broken."""
    results_dir.mkdir(parents=True, exist_ok=True)
    lock = results_dir / LOCK_NAME
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > LOCK_STALE_SECONDS:
                with contextlib.suppress(FileNotFoundError):
                    lock.unlink()
                continue
            if time.monotonic() >= deadline:
                raise StoreLockTimeout("the result store is locked by another marketlens process") from None
            time.sleep(LOCK_RETRY_SECONDS)
            continue
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        break
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            lock.unlink()


@dataclass
class Entry:
    session: str
    result_id: str
    created_at: datetime
    expires_at: datetime
    bytes: int
    files: list[pathlib.Path]


@dataclass
class EvictReport:
    expired: list[str] = field(default_factory=list)
    evicted: list[str] = field(default_factory=list)
    orphans: int = 0
    sessions_removed: int = 0


def _parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def scan(results_dir: pathlib.Path) -> list[Entry]:
    """Every stored result with a readable sidecar, across sessions."""
    out: list[Entry] = []
    if not results_dir.is_dir():
        return out
    for session in sorted(p for p in results_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for sidecar in session.glob("r_*.json"):
            try:
                info = json.loads(sidecar.read_text(encoding="utf-8"))["info"]
                created, expires = _parse_time(info["created_at"]), _parse_time(info["expires_at"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            parquet = sidecar.with_suffix(".parquet")
            size = 0
            for f in (parquet, sidecar):
                with contextlib.suppress(FileNotFoundError):
                    size += f.stat().st_size
            out.append(Entry(session.name, sidecar.stem, created, expires, size, [parquet, sidecar]))
    return out


def _delete(files: list[pathlib.Path]) -> None:
    for f in files:
        with contextlib.suppress(FileNotFoundError):
            f.unlink()


def evict(
    results_dir: pathlib.Path, *, now: datetime, max_bytes: int, protect: Collection[str] = ()
) -> EvictReport:
    """One eviction pass (the caller holds the lock). ``protect`` names result
    ids never evicted for size in this pass (the one just written)."""
    report = EvictReport()
    if not results_dir.is_dir():
        return report
    entries = scan(results_dir)
    live = []
    for e in entries:
        if e.expires_at <= now:
            _delete(e.files)
            report.expired.append(e.result_id)
        else:
            live.append(e)
    known = {(e.session, e.result_id) for e in entries}
    cutoff = now.timestamp() - ORPHAN_AGE_SECONDS
    for session in (p for p in results_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for f in session.iterdir():
            if f.is_dir():
                continue
            stem = f.name.split(".", 1)[0]
            if f.suffix == ".json" and (session.name, stem) in known:
                continue
            if f.suffix == ".parquet" and (session.name, stem) in known:
                continue
            try:
                old = f.stat().st_mtime < cutoff
            except FileNotFoundError:
                continue
            if old:
                _delete([f])
                report.orphans += 1
    total = sum(e.bytes for e in live)
    if total > max_bytes:
        target = max_bytes * TARGET_FRACTION
        for e in sorted(live, key=lambda e: e.created_at):
            if total <= target:
                break
            if e.result_id in protect:
                continue
            _delete(e.files)
            total -= e.bytes
            report.evicted.append(e.result_id)
    for session in (p for p in results_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        try:
            session.rmdir()
            report.sessions_removed += 1
        except OSError:
            pass
    return report
