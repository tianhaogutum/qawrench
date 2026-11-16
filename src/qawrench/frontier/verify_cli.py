"""Read-only local verification of submitted saved strict robots witnesses."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys

from .core import AuditError, json_text, strict_json
from .robots import RobotsPolicy
from .witness import verify_malformed_rule_witness

MAX_INPUT_BYTES = 16 * 1024 * 1024


def _read_json(path: Path) -> object:
    # Nonblocking open lets us reject a FIFO without waiting for a writer.
    # Check the opened descriptor, then bound the actual read (not just stat).
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise AuditError(f"input must be a local regular file: {path}")
        data = stream.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise AuditError(f"input exceeds {MAX_INPUT_BYTES} bytes: {path}")
    return strict_json(data.decode('utf-8'))


def _witnesses(report: object) -> list:
    if (not isinstance(report, dict) or type(report.get('schema_version')) is not int
            or report['schema_version'] != 1):
        raise AuditError("report requires schema_version 1 and a robots object")
    robots = report.get('robots')
    if (not isinstance(robots, dict) or robots.get('mode') != 'offline-enforced'
            or robots.get('strict') is not True):
        raise AuditError("report robots requires mode offline-enforced and strict true")
    if not isinstance(robots.get('malformed_rules'), list):
        raise AuditError("report robots.malformed_rules must be an explicit array")
    return robots['malformed_rules']


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='qawrench frontier verify-witnesses', allow_abbrev=False,
        description='Verify submitted saved strict robots witnesses against local snapshots; never fetches URLs')
    parser.add_argument('report', type=Path, help='local schema-version-1 strict audit report JSON')
    parser.add_argument('--robots', type=Path, required=True,
                        help='caller-retained local version-1 inline robots manifest JSON')
    parser.add_argument('--verify-reason', action='store_true',
                        help='also check each exact record against current whole-snapshot strict classification')
    args = parser.parse_args(argv)
    try:
        witnesses = _witnesses(_read_json(args.report))
        manifest = _read_json(args.robots)
        # Validate even unused snapshots and empty witness collections. The
        # unchanged library helper also validates the manifest on every call.
        RobotsPolicy.from_dict(manifest)
        options = {'verify_reason': True} if args.verify_reason else {}
        results = [{'index': index, **verify_malformed_rule_witness(witness, manifest, **options)}
                   for index, witness in enumerate(witnesses)]
        verified = sum(result['verified'] for result in results)
        failed = len(results) - verified
        output = {'schema_version': 1, 'scope': 'submitted-witnesses-only',
                  'counts': {'submitted': len(results), 'verified': verified, 'failed': failed},
                  'results': results}
        if args.verify_reason:
            output['verification_scope'] = 'provenance-and-strict-reason'
        print(json_text(output))
        return 1 if failed else 0
    except (AuditError, OSError, UnicodeError, RecursionError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
