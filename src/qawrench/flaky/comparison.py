"""Compare two exact runs' stored normalized evidence, without source replay."""

from contextlib import closing
import hashlib
import sqlite3

from .adapters import (CTRF_STATUSES, PW_OUTCOMES, PW_STATUSES, arr, boolean,
                       canonical, choice, number, string)
from .ledger import (LedgerError, _archive_column, _classify, _duration_sum, _read_db,
                     _report_duration, _report_observation, _report_retained)


def _identity(entry, test_id, format, project_context, location):
    string(entry.get('project'), f'{location}.project', empty=format == 'playwright')
    if format == 'playwright':
        string(entry.get('project_id'), f'{location}.project_id', empty=True)
    elif entry.get('project_id') is not None or entry['project'] != project_context:
        raise LedgerError(f'{location}: CTRF project/project_id disagrees with run context')
    string(entry.get('file'), f'{location}.file')
    titles = arr(entry.get('title_path'), f'{location}.title_path')
    if not titles:
        raise LedgerError(f'{location}.title_path: expected nonempty array')
    for i, title in enumerate(titles):
        string(title, f'{location}.title_path[{i}]', empty=i < len(titles) - 1)
    opaque = boolean(entry.get('identity_path_opaque'), f'{location}.identity_path_opaque')
    if opaque != (format == 'ctrf'):
        raise LedgerError(f'{location}.identity_path_opaque: disagrees with adapter')
    expected = hashlib.sha256(canonical([format, entry['project'], entry.get('project_id'),
                                        entry['file'], titles]).encode()).hexdigest()
    if test_id != expected or entry.get('test_id') != test_id:
        raise LedgerError(f'{location}.test_id: stored identity mismatch')
    return {key: entry[key] for key in ('project', 'project_id', 'file', 'title_path', 'identity_path_opaque')} | {
        'test_id': test_id, 'format': format}


def _evidence(db, entry, run_id, test_id, format, archived, location):
    native = format == 'playwright'
    if native:
        choice(entry.get('expected_status'), PW_STATUSES, f'{location}.expected_status')
    elif entry.get('expected_status') is not None:
        raise LedgerError(f'{location}.expected_status: CTRF requires null')
    choice(entry.get('reported_status'), PW_OUTCOMES if native else CTRF_STATUSES, f'{location}.reported_status')
    number(entry.get('retries'), f'{location}.retries', integer=True)
    for key in ('reported_flaky', 'attempt_details_unavailable', 'status_detail_unavailable'):
        boolean(entry.get(key), f'{location}.{key}')
    _report_duration(entry, format=format, location=location)
    attempts = []
    statuses = PW_STATUSES if native else PW_STATUSES | CTRF_STATUSES
    for index, status, duration, text in db.execute(
            'SELECT attempt_index,status,duration_ms,data FROM attempts '
            'WHERE run_id=? AND test_id=? ORDER BY attempt_index', (run_id, test_id)):
        if archived:
            raise LedgerError(f'{location}: archived observation cannot have local attempts')
        path = f'{location} attempt {index!r}'
        attempt = _report_observation(text, path)
        number(attempt.get('index'), f'{path}.index', integer=True)
        choice(attempt.get('status'), statuses, f'{path}.status')
        number(attempt.get('duration_ms'), f'{path}.duration_ms')
        # The query columns and JSON are both normalized stored evidence. Do
        # not silently choose one when the metrics used here disagree.
        for key, column in (('index', index), ('status', status), ('duration_ms', duration)):
            if attempt[key] != column:
                raise LedgerError(f'{path}.{key}: relational/JSON mismatch')
        attempts.append(attempt)
    retained = _report_retained(entry, attempts, archived=archived, location=location)
    outcome = retained['classification'] if retained is not None else _classify(entry, attempts)
    choice(outcome['terminal_status'], statuses | {'not_run'}, f'{location}.outcome.terminal_status')
    count = retained['available_attempts'] if retained is not None else len(attempts)
    duration = retained['available_attempt_duration_ms'] if retained is not None else sum(a['duration_ms'] for a in attempts)
    _duration_sum(duration, f'{location}.available_attempt_duration_ms')
    return {key: entry[key] for key in ('expected_status', 'reported_status', 'reported_flaky', 'retries',
                                      'reported_duration_ms', 'attempt_details_unavailable', 'status_detail_unavailable')} | {
        'outcome': outcome, 'available_attempt_count': count, 'available_attempt_duration_ms': duration,
        'attempts_archived': archived}


def _run_context(db, run_id, archive_column):
    format, project, archived = db.execute(
        f'SELECT format,project_context,{archive_column} IS NOT NULL FROM runs WHERE run_id=?', (run_id,)).fetchone()
    label = f'run-id {run_id!r}'
    if format not in ('playwright', 'ctrf'):
        raise LedgerError(f'{label}: invalid stored format')
    if format == 'playwright':
        if project is not None:
            raise LedgerError(f'{label}: native project context must be null')
    else:
        string(project, f'{label} CTRF project context')
    return dict(format=format, project_context=project, state='archived' if archived else 'full')


def _run(db, run_id, archive_column):
    context = _run_context(db, run_id, archive_column)
    format, project, archived = context['format'], context['project_context'], context['state'] == 'archived'
    label = f'run-id {run_id!r}'
    rows = {}
    for test_id, text in db.execute('SELECT test_id,data FROM observations WHERE run_id=? ORDER BY test_id', (run_id,)):
        location = f'{label} observation {test_id!r}'
        entry = _report_observation(text, location)
        identity = _identity(entry, test_id, format, project, location)
        evidence = _evidence(db, entry, run_id, test_id, format, bool(archived), location)
        rows[test_id] = (identity, evidence)
    return dict(**context, observation_count=len(rows)), rows


def compare_runs(db_path, *, base_run_id, run_id):
    """Sorted identity union and per-side metrics in one read-only v1/v2 snapshot.

    Missing sides are null observations, never inferred test outcomes. Only the
    two selected runs are read; sources, metadata and archive paths are unused.
    This is a stored-evidence comparison, not replay verification.
    """
    for name, value in (('--base-run-id', base_run_id), ('--run-id', run_id)):
        try:
            string(value, name)
            value.encode('utf-8')
        except (ValueError, TypeError) as exc:
            raise LedgerError(f'invalid {name}: {exc}') from exc
    try:
        with closing(_read_db(db_path)) as db:
            selected = sorted({base_run_id, run_id})
            missing = [key for key in selected if db.execute('SELECT 1 FROM runs WHERE run_id=?', (key,)).fetchone() is None]
            if missing:
                raise LedgerError(f"unknown run-id(s): {', '.join(repr(key) for key in missing)}")
            archive_column = _archive_column(db)
            runs = {key: _run(db, key, archive_column) for key in selected}
            base, base_rows = runs[base_run_id]
            current, current_rows = runs[run_id]
            tests = []
            for test_id in sorted(base_rows.keys() | current_rows.keys()):
                b, c = base_rows.get(test_id), current_rows.get(test_id)
                tests.append({**(b or c)[0], 'membership': 'both' if b and c else 'base_only' if b else 'current_only',
                              'base': b[1] if b else None, 'current': c[1] if c else None})
            return dict(action='compared', base_run_id=base_run_id, run_id=run_id, base=base, current=current, tests=tests)
    except (sqlite3.Error, ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        raise LedgerError(f'cannot compare runs: {exc}') from exc
