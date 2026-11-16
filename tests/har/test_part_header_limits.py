"""Exact header limits, independent of upload size, apply before publication."""
import base64
import copy
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import qawrench.har as doctor
from qawrench.har.core import _multipart_part_headers
from examples.har.fixtures import RULES, har
from .test_multipart import ROOT, upload
from .test_part_headers import two_parts


def byte_headers(size):
    prefix = b'Content-Disposition: form-data; name="file"; filename="/never/read.bin"\r\nX-Pad: '
    return prefix + b'x' * (size - len(prefix))


def counted_headers(count):
    return b'\r\n'.join([b'Content-Disposition: form-data; name="file"']
                        + [f'X-{i}: value'.encode() for i in range(count - 1)])


LIMIT_CASES = [('bytes', byte_headers(65537), 'part header block exceeds 65536-byte limit'),
               ('count', counted_headers(129), 'part header count exceeds 128-line limit')]


class PartHeaderLimitTests(unittest.TestCase):
    def test_exact_thresholds_preserve_binary_empty_files_and_large_payloads(self):
        for headers in (byte_headers(65535), byte_headers(65536), counted_headers(127), counted_headers(128)):
            for payload in (b'', b'\x00\xff' * 40000):
                source = har([upload(two_parts(headers, payload))])
                saved = copy.deepcopy(source)
                with self.subTest(headerBytes=len(headers), payloadBytes=len(payload)), patch.object(
                        Path, 'open', side_effect=AssertionError('file metadata must never be read')):
                    doctor.validate_har(source)
                    rewritten, plan = doctor.rewrite_har(source, RULES)
                    self.assertTrue(plan['safeToApply'])
                    self.assertEqual(rewritten['log']['entries'][0]['request']['postData'],
                                     saved['log']['entries'][0]['request']['postData'])
                    self.assertEqual(doctor.embed_form_params(source), saved)
                    inventory = doctor.export_inventory(source)
                    self.assertEqual(inventory['requests'][0]['postData'],
                                     saved['log']['entries'][0]['request']['postData'])
                    self.assertTrue(doctor.build_preflight(source, inventory)['passed'])
                    self.assertEqual(source, saved)
        # Text bodies use byte lengths too; a high byte counts once after base64
        # decoding, whereas a Unicode character in plain text is UTF-8 encoded.
        for text, expected in [('é', 0), ('éx', 2)]:
            headers = byte_headers(65534) + text.encode()
            item = upload(two_parts(headers, b''))
            post = item['request']['postData']
            post['text'] = base64.b64decode(post['text']).replace(b'\x00\xff', b'plain').decode()
            post.pop('encoding')
            if expected == 0:
                doctor.validate_har(har([item]))
            else:
                with self.assertRaisesRegex(doctor.DoctorError, '65536-byte limit'):
                    doctor.validate_har(har([item]))

    def test_over_limits_report_containing_request_and_later_part_in_every_api(self):
        for label, headers, error in LIMIT_CASES:
            source = har([upload(), upload(two_parts(headers))])
            saved = copy.deepcopy(source)
            inventory = doctor.export_inventory(har([upload()]))
            inventory['requests'].append({'id': 'bad', **{k: source['log']['entries'][1]['request'][k]
                                                        for k in ('url', 'method', 'postData')}})
            for operation in (doctor.validate_har, doctor.embed_form_params, doctor.export_inventory,
                              lambda h: doctor.build_plan(h, RULES),
                              lambda h: doctor.rewrite_har(h, RULES),
                              lambda h: doctor.build_preflight(h, inventory)):
                with self.subTest(limit=label, operation=operation), self.assertRaisesRegex(
                        doctor.DoctorError, re.escape('log.entries[1].request.postData.parts[1]: ' + error)
                        + '.*re-record'):
                    operation(source)
            with self.subTest(limit=label, inventory=True), self.assertRaisesRegex(doctor.DoctorError, re.escape(
                    'inventory.requests[1].postData.parts[1]: ' + error) + '.*re-record'):
                doctor.validate_inventory(inventory)
            self.assertEqual(source, saved)

    def test_header_search_and_copies_are_bounded_even_without_a_separator(self):
        class GuardedBody(bytes):
            def find(self, sub, start=0, end=None):
                # The helper must not search beyond a possible maximum-size
                # header block and its separator, even if no separator exists.
                if sub == b'\r\n\r\n':
                    self_outer.assertIsNotNone(end)
                    self_outer.assertLessEqual(end - start, 65536 + 4)
                return super().find(sub, start, len(self) if end is None else end)

            def __getitem__(self, key):
                if isinstance(key, slice):
                    self_outer.assertLessEqual(len(range(*key.indices(len(self)))), 65536)
                return super().__getitem__(key)

        self_outer = self
        # Nonzero offset ensures the bound is relative to the part, not body.
        prefix = b'ignored framing'
        good = GuardedBody(prefix + byte_headers(65536) + b'\r\n\r\n' + b'\xff' * 200000)
        _multipart_part_headers(good, len(prefix), len(good), 'request.postData.parts[1]')
        for block in (byte_headers(65537) + b'\r\n\r\n', b'x' * 200000):
            body = GuardedBody(prefix + block)
            with self.subTest(separator=b'\r\n\r\n' in block), self.assertRaisesRegex(
                    doctor.DoctorError, r'parts\[1\].*65536-byte limit'):
                _multipart_part_headers(body, len(prefix), len(body), 'request.postData.parts[1]')
        for length in (65535, 65536):
            with self.assertRaisesRegex(doctor.DoctorError, 'missing CRLF header/body separator'):
                _multipart_part_headers(b'x' * length, 0, length, 'request.postData.parts[0]')

    def test_later_over_limit_headers_block_json_zip_and_inventory_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rules, wanted, source = root / 'rules.json', root / 'requests.json', root / 'source.har'
            rules.write_text('{}')
            good = har([upload()])
            inventory = doctor.export_inventory(good)
            wanted.write_text(json.dumps(inventory))

            def refused(command, path, out, error):
                args = [sys.executable, '-m', 'qawrench.har', command, str(path), '--out', str(out)]
                if command in ('plan', 'apply', 'preflight'):
                    args += ['--rules', str(rules), '--inventory', str(wanted)]
                result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(error, result.stderr)
                self.assertIn('re-record', result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertFalse(out.exists())
                self.assertFalse(list(root.glob('.*.tmp')))

            for label, headers, error in LIMIT_CASES:
                bad_body = two_parts(headers)
                bad = har([upload(), upload(bad_body)])
                source.write_text(json.dumps(bad))
                saved = source.read_bytes()
                for command in ('plan', 'apply', 'preflight', 'embed-forms', 'export-inventory'):
                    refused(command, source, root / f'{label}-{command}.json',
                            'log.entries[1].request.postData.parts[1]: ' + error)
                self.assertEqual(source.read_bytes(), saved)
                attached = copy.deepcopy(bad)
                attached['log']['entries'][1]['request']['postData'] = {
                    'mimeType': 'multipart/form-data; boundary="upload:42"', '_file': 'upload.bin', 'text': ''}
                archive = root / (label + '.har.zip')
                with zipfile.ZipFile(archive, 'w') as z:
                    z.writestr('recording.har', json.dumps(attached))
                    z.writestr('upload.bin', bad_body)
                saved_archive = archive.read_bytes()
                for command in ('plan', 'apply', 'preflight', 'export-inventory'):
                    out = root / (f'{label}-zip-{command}' + ('.har.zip' if command == 'apply' else '.json'))
                    refused(command, archive, out, 'log.entries[1].request.postData.parts[1]: ' + error)
                self.assertEqual(archive.read_bytes(), saved_archive)
                source.write_text(json.dumps(good))
                bad_inventory = copy.deepcopy(inventory)
                bad_inventory['requests'].append({'id': 'bad', **{k: bad['log']['entries'][1]['request'][k]
                                                               for k in ('url', 'method', 'postData')}})
                wanted.write_text(json.dumps(bad_inventory))
                saved_inventory = wanted.read_bytes()
                for command in ('plan', 'apply', 'preflight'):
                    refused(command, source, root / f'{label}-inventory-{command}.json',
                            'inventory.requests[1].postData.parts[1]: ' + error)
                self.assertEqual(wanted.read_bytes(), saved_inventory)
                self.assertEqual(json.loads(source.read_bytes()), good)
                wanted.write_text(json.dumps(inventory))


if __name__ == '__main__':
    unittest.main()
