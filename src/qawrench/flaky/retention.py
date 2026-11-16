"""Archive full source bundles before retaining only historical attempt metrics."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .adapters import canonical
from .ledger import (LedgerError, RETENTION_SCHEMA_VERSION, _archive_column,
                     _check_schema, _classify, _read_db)
from .transfer import _export_snapshot


def _plan(db, keep_latest, archive):
    # Archived runs still occupy their original place in the retention order.
    # Import timestamps are local UTC SQLite timestamps; ties use run ID.
    rows = list(db.execute(f"SELECT run_id,{_archive_column(db)} FROM runs "
                           "ORDER BY imported_at DESC, run_id DESC"))
    run_ids = sorted(run_id for run_id, old_archive in rows[keep_latest:] if old_archive is None)
    observations = attempts = source_bytes = 0
    for run_id in run_ids:
        observations += db.execute("SELECT count(*) FROM observations WHERE run_id=?", (run_id,)).fetchone()[0]
        attempts += db.execute("SELECT count(*) FROM attempts WHERE run_id=?", (run_id,)).fetchone()[0]
        source_bytes += db.execute("SELECT length(raw_report) FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
    return {"action": "planned", "keep_latest": keep_latest, "run_ids": run_ids,
            "observations": observations, "attempts": attempts, "source_bytes": source_bytes,
            "archive_path": str(archive)}


def _publish_archive(path, raw):
    """Sync a complete sibling file, then publish it without replacing any path.

    A failed DB commit may leave this complete, independently importable file.
    Never remove a published archive: the caller may already have consumed it.
    """
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".retry-history-", suffix=".tmp", dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Atomic no-clobber publication on a local filesystem.
        temporary.unlink()
        temporary = None
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prune(db_path, *, keep_latest: int, archive_path, dry_run=False):
    """Keep N most recently imported full runs; archive older full runs.

    Dry run only reads SQLite, never creates an archive or migrates the schema.
    Apply locks one snapshot, publishes a durable v1 source bundle, then prunes
    in one SQLite transaction. Errors leave the original database unchanged.
    On SQL/commit failure a complete archive can remain; choose a new path to
    retry. No physical compaction or automatic external archive reads occur.
    """
    if type(keep_latest) is not int or keep_latest < 0:
        raise LedgerError("keep-latest must be a nonnegative integer")
    path = Path(db_path).resolve()
    archive = Path(archive_path).absolute()
    reserved = {path, *(Path(str(path) + suffix) for suffix in ("-journal", "-wal", "-shm"))}
    if archive.resolve() in reserved:
        raise LedgerError("archive path must differ from the database path and its SQLite sidecars")
    try:
        if dry_run:
            with closing(_read_db(path)) as db:
                return _plan(db, keep_latest, archive)
        # mode=rw prevents a typo/missing DB from creating an empty file.
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                db.execute("BEGIN IMMEDIATE")
                _check_schema(db)
                plan = _plan(db, keep_latest, archive)
                if not plan["run_ids"]:
                    return {**plan, "action": "unchanged"}
                raw = _export_snapshot(db, plan["run_ids"])
                digest = hashlib.sha256(raw).hexdigest()
                _publish_archive(archive, raw)
                if db.execute("PRAGMA user_version").fetchone()[0] != RETENTION_SCHEMA_VERSION:
                    db.execute("ALTER TABLE runs ADD COLUMN archive TEXT")
                    db.execute(f"PRAGMA user_version={RETENTION_SCHEMA_VERSION}")
                for run_id in plan["run_ids"]:
                    for test_id, data in list(db.execute("SELECT test_id,data FROM observations WHERE run_id=?", (run_id,))):
                        entry = json.loads(data)
                        attempts = [json.loads(row[0]) for row in db.execute(
                            "SELECT data FROM attempts WHERE run_id=? AND test_id=? ORDER BY attempt_index", (run_id, test_id))]
                        entry.pop("source", None)
                        entry["retained_attempts"] = {
                            "classification": _classify(entry, attempts),
                            "available_attempts": len(attempts),
                            "available_attempt_duration_ms": sum(a["duration_ms"] for a in attempts)}
                        db.execute("UPDATE observations SET data=? WHERE run_id=? AND test_id=?",
                                   (canonical(entry), run_id, test_id))
                    db.execute("DELETE FROM attempts WHERE run_id=?", (run_id,))
                    source_digest = db.execute("SELECT digest FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
                    reference = {"path": str(archive), "sha256": digest, "source_sha256": source_digest}
                    db.execute("UPDATE runs SET raw_report=?,archive=? WHERE run_id=?",
                               (b"", canonical(reference), run_id))
        return {**plan, "action": "pruned", "archive_sha256": digest}
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
        raise LedgerError(f"retention failed (database transaction rolled back; a complete archive may remain): {exc}") from exc
