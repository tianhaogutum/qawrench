"""One exact identity's stored evidence in a caller-selected run sequence."""

from contextlib import closing
import re
import sqlite3

from .adapters import string
from .comparison import _evidence, _identity, _run_context
from .ledger import LedgerError, _archive_column, _read_db, _report_observation


def test_history(db_path, *, test_id, run_ids):
    """Read one v1/v2 snapshot; preserve run order and duplicate selections.

    The fingerprint must exist in observation keys somewhere in the ledger.
    Only the selected runs' context and this identity's evidence are decoded.
    Absent observations have null evidence, never an inferred outcome; identity
    is null if none of the selected runs contains this known fingerprint.
    This summarizes stored evidence without replay, external reads or writes.
    """
    if not isinstance(test_id, str) or re.fullmatch(r'[0-9a-f]{64}', test_id) is None:
        raise LedgerError('invalid --test-id: expected exactly 64 lowercase hexadecimal characters')
    try:
        if isinstance(run_ids, (str, bytes)) or run_ids is None:
            raise ValueError('expected a nonempty iterable of run ID strings')
        selected = []
        for run_id in run_ids:
            string(run_id, '--run-id')
            run_id.encode('utf-8')
            selected.append(run_id)
        if not selected:
            raise ValueError('expected at least one --run-id')
    except (TypeError, ValueError) as exc:
        raise LedgerError(f'invalid --run-id selection: {exc}') from exc
    try:
        with closing(_read_db(db_path)) as db:
            unique = dict.fromkeys(selected)
            missing = [key for key in unique if db.execute('SELECT 1 FROM runs WHERE run_id=?', (key,)).fetchone() is None]
            if missing:
                raise LedgerError(f"unknown run-id(s): {', '.join(repr(key) for key in missing)}")
            if db.execute('SELECT 1 FROM observations WHERE test_id=? LIMIT 1', (test_id,)).fetchone() is None:
                raise LedgerError(f'unknown test-id {test_id!r}')
            archive_column = _archive_column(db)
            identity, records = None, {}
            for run_id in unique:
                context = _run_context(db, run_id, archive_column)
                row = db.execute('SELECT data FROM observations WHERE run_id=? AND test_id=?', (run_id, test_id)).fetchone()
                evidence = None
                if row is not None:
                    location = f'run-id {run_id!r} observation {test_id!r}'
                    entry = _report_observation(row[0], location)
                    identity = _identity(entry, test_id, context['format'], context['project_context'], location)
                    evidence = _evidence(db, entry, run_id, test_id, context['format'],
                                         context['state'] == 'archived', location)
                records[run_id] = dict(run_id=run_id, **context,
                                      presence='present' if row is not None else 'absent', evidence=evidence)
            return dict(action='test-history', test_id=test_id, identity=identity,
                        runs=[records[key] for key in selected])
    except (sqlite3.Error, ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        raise LedgerError(f'cannot read test history: {exc}') from exc
