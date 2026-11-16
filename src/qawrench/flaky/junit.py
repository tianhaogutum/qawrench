"""Deterministic JUnit XML for one run's retained normalized observations."""

from contextlib import closing
from decimal import Decimal
import sqlite3
import xml.etree.ElementTree as ET

from .adapters import canonical, string
from .comparison import _run
from .ledger import LedgerError, _archive_column, _read_db


def _xml_string(value, location):
    # ElementTree escapes markup, but does not reject XML 1.0 control chars or
    # surrogates. Reject these rather than silently changing stored identity.
    if any(not (ord(c) in (9, 10, 13) or 0x20 <= ord(c) <= 0xD7FF
                or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF)
           for c in value):
        raise LedgerError(f'{location}: character not representable in XML 1.0')
    return value


def _properties(element, values, location):
    node = ET.SubElement(element, 'properties')
    for key, value in sorted(values.items()):
        text = value if isinstance(value, str) else canonical(value)
        ET.SubElement(node, 'property', name=key, value=_xml_string(text, f'{location}.{key}'))


def _state(evidence):
    """A terminal observation establishes a state; gaps never establish a pass."""
    outcome = evidence['outcome']
    status = outcome['terminal_status']
    if not evidence['terminal_observation_available'] or status in ('not_run', 'pending', 'other'):
        return 'skipped', 'unavailable', 'Retained evidence does not establish a terminal outcome'
    if status == 'skipped':
        return 'skipped', 'skipped', 'Recorded terminal outcome: skipped'
    if status == 'interrupted':
        return 'error', 'interrupted', 'Recorded terminal outcome: interrupted'
    if status in ('failed', 'timedOut'):
        if outcome['expected_failure']:
            return 'skipped', 'expected_failure', f'Recorded expected failure: {status}'
        return ('error' if status == 'timedOut' else 'failure'), status, f'Recorded terminal outcome: {status}'
    if status == 'passed':
        if outcome['unexpected_pass']:
            return 'failure', 'unexpected_pass', 'Recorded pass disagrees with expected status'
        if outcome['passed']:
            return None, 'passed', 'Recorded terminal outcome: passed'
    return 'skipped', 'unavailable', 'Retained evidence does not establish a terminal outcome'


def _render(run_id, context, rows):
    root = ET.Element('testsuites')
    suite = ET.SubElement(root, 'testsuite', name=run_id)
    _properties(suite, dict(run_id=run_id, **context,
                           evidence='retained observations; tests were not re-executed',
                           duration_basis='available attempt duration in milliseconds / 1000'), f'run-id {run_id!r}')
    counts = dict(tests=len(rows), failures=0, errors=0, skipped=0)
    for test_id, (identity, evidence) in rows.items():
        location = f'run-id {run_id!r} observation {test_id!r}'
        for key in ('project', 'project_id', 'file'):
            if identity[key] is not None:
                _xml_string(identity[key], f'{location}.{key}')
        for title in identity['title_path']:
            _xml_string(title, f'{location}.title_path')
        case = ET.SubElement(suite, 'testcase',
            classname=canonical([identity['format'], identity['project'], identity['project_id'], identity['file']]),
            name=canonical(identity['title_path']))
        if evidence['available_attempt_count'] > 0:
            # Shift the decimal exponent exactly, independent of the caller's
            # decimal context. The evidence validator checked finiteness.
            sign, digits, exponent = Decimal(str(evidence['available_attempt_duration_ms'])).as_tuple()
            case.set('time', format(Decimal((sign, digits, exponent - 3)), 'f'))
        state, reason, message = _state(evidence)
        _properties(case, {**identity, **{k: v for k, v in evidence.items() if k != 'outcome'},
                           **evidence['outcome'], 'junit_state': state or 'passed', 'junit_reason': reason}, location)
        if state:
            counts[dict(failure='failures', error='errors', skipped='skipped')[state]] += 1
            ET.SubElement(case, state, type=reason, message=message)
    for key, value in counts.items():
        root.set(key, str(value))
        suite.set(key, str(value))
    ET.indent(root, space='  ')
    return ET.tostring(root, encoding='unicode', xml_declaration=True)


def junit_report(db_path, *, run_id):
    """Export exactly one known v1/v2 run in one read-only snapshot.

    Reuses strict selected identity/evidence validation from comparison/history,
    including the report JSON/retained validators. Source blobs, metadata and
    archive paths are unused. No replay, external reads, writes or attempt cases.
    """
    try:
        string(run_id, '--run-id')
        run_id.encode('utf-8')
        _xml_string(run_id, '--run-id')
    except (ValueError, TypeError) as exc:
        raise LedgerError(f'invalid --run-id: {exc}') from exc
    try:
        with closing(_read_db(db_path)) as db:
            if db.execute('SELECT 1 FROM runs WHERE run_id=?', (run_id,)).fetchone() is None:
                raise LedgerError(f'unknown run-id {run_id!r}')
            context, rows = _run(db, run_id, _archive_column(db))
            for test_id, (_, evidence) in rows.items():
                # Full rows retain exact attempt indices. A remaining prior
                # pass is not a terminal pass if the final row is missing.
                terminal_available = evidence['available_attempt_count'] > 0
                if context['state'] == 'full' and terminal_available:
                    last_index = db.execute('SELECT MAX(attempt_index) FROM attempts WHERE run_id=? AND test_id=?',
                                            (run_id, test_id)).fetchone()[0]
                    terminal_available = last_index == evidence['retries']
                evidence['terminal_observation_available'] = terminal_available
            return _render(run_id, context, rows)
    except (sqlite3.Error, ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        raise LedgerError(f'cannot export JUnit: {exc}') from exc
