"""A request has at most 1024 outer parts; body bytes are never rewritten."""
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
from qawrench.har import core
from examples.har.fixtures import RULES, har
from .test_multipart import ROOT, upload

ERROR = 'part count exceeds 1024-part limit'
HEADERS = (b'Content-Disposition: form-data; name="file"; filename="/never/read.bin"\r\n'
           b'Content-Type: multipart/mixed; boundary=inner')
# These outer boundary lookalikes and inner delimiters remain payload data.
BINARY = (b'\x00\xffprefix--upload:42\r\n--upload:42-extra\r\n--upload:42--extra\r\n'
          b'--upload:42\x00\r\n--upload:42 \tx\r\n--inner\r\nx\r\n--inner--\r\n')


def many_parts(count, payloads=(b'', b'x'), *, close=True):
    return (b'preamble\r\n' + b''.join(
        b'--upload:42 \t\r\n' + HEADERS + b'\r\n\r\n' + payloads[i % len(payloads)] + b'\r\n'
        for i in range(count))
        + (b'--upload:42--\r\nepilogue\r\n--upload:42\r\nignored' if close else b''))


def make_archive(path, data, body):
    attached = copy.deepcopy(data)
    attached['log']['entries'][-1]['request']['postData'] = {
        'mimeType': 'multipart/form-data; boundary="upload:42"', '_file': 'upload.bin', 'text': ''}
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('recording.har', json.dumps(attached))
        archive.writestr('upload.bin', body)


class PartCountLimitTests(unittest.TestCase):
    def test_exact_threshold_is_per_request_and_preserves_empty_binary_and_text(self):
        for count in (1023, 1024):
            # Two independently bounded requests; the counter must reset.
            bodies = [many_parts(count, (b'', BINARY, b'\x00\xff' * 40000)), many_parts(count)]
            data = har([upload(body) for body in bodies])
            text_post = data['log']['entries'][1]['request']['postData']
            text_post['text'] = bodies[1].decode()
            text_post.pop('encoding')
            saved = copy.deepcopy(data)
            with self.subTest(count=count), patch.object(
                    Path, 'open', side_effect=AssertionError('filename must never be read')):
                doctor.validate_har(data)
                changed, plan = doctor.rewrite_har(data, RULES)
                self.assertTrue(plan['safeToApply'])
                for i, body in enumerate(bodies):
                    self.assertEqual(doctor.request_key(changed['log']['entries'][i]['request'])[2], body)
                    self.assertEqual(changed['log']['entries'][i]['request']['postData'],
                                     saved['log']['entries'][i]['request']['postData'])
                self.assertEqual(doctor.embed_form_params(data), saved)
                inventory = doctor.export_inventory(data)
                self.assertTrue(doctor.build_preflight(data, inventory)['passed'])
                for i in range(2):
                    self.assertEqual(inventory['requests'][i]['postData'],
                                     saved['log']['entries'][i]['request']['postData'])
                self.assertEqual(data, saved)

    def test_later_excess_reports_path_index_limit_and_recapture_in_every_api(self):
        data = har([upload(), upload(many_parts(1025))])
        saved = copy.deepcopy(data)
        good = har([upload()])
        inventory = doctor.export_inventory(good)
        bad_inventory = copy.deepcopy(inventory)
        bad_inventory['requests'].append({'id': 'bad', **{k: data['log']['entries'][1]['request'][k]
                                                        for k in ('url', 'method', 'postData')}})
        saved_inventory = copy.deepcopy(bad_inventory)
        for operation in (doctor.validate_har, doctor.embed_form_params, doctor.export_inventory,
                          lambda h: doctor.build_plan(h, RULES),
                          lambda h: doctor.rewrite_har(h, RULES),
                          lambda h: doctor.build_preflight(h, inventory)):
            with self.subTest(operation=operation), self.assertRaisesRegex(doctor.DoctorError, re.escape(
                    'log.entries[1].request.postData.parts[1024]: ' + ERROR) + '.*re-record'):
                operation(data)
        for operation in (doctor.validate_inventory,
                          lambda inv: doctor.build_plan(good, RULES, inventory=inv),
                          lambda inv: doctor.rewrite_har(good, RULES, inventory=inv),
                          lambda inv: doctor.build_preflight(good, inv)):
            with self.subTest(inventoryOperation=operation), self.assertRaisesRegex(doctor.DoctorError, re.escape(
                    'inventory.requests[1].postData.parts[1024]: ' + ERROR) + '.*re-record'):
                operation(bad_inventory)
        self.assertEqual(data, saved)
        self.assertEqual(bad_inventory, saved_inventory)

    def test_excess_opening_stops_before_its_headers_even_without_a_next_delimiter(self):
        prefix = many_parts(1024, close=False)
        for tail in (b'bad header\r\n\r\nx\r\n--upload:42--\r\n',
                     b'x' * 65537 + b'\r\n\r\nx\r\n--upload:42--\r\n', b''):
            with self.subTest(tailLength=len(tail)), patch.object(
                    core, '_multipart_part_headers', wraps=core._multipart_part_headers) as headers:
                with self.assertRaisesRegex(doctor.DoctorError, re.escape('parts[1024]: ' + ERROR)):
                    doctor.validate_har(har([upload(prefix + b'--upload:42\r\n' + tail)]))
                self.assertEqual(headers.call_count, 1024)
                self.assertTrue(headers.call_args.args[-1].endswith('parts[1023]'))

    def test_later_excess_blocks_all_json_zip_and_inventory_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, wanted, rules = root/'source.har', root/'requests.json', root/'rules.json'
            rules.write_text('{}')
            good = har([upload()])
            inventory = doctor.export_inventory(good)
            wanted.write_text(json.dumps(inventory))
            body = many_parts(1025)
            bad = har([upload(), upload(body)])
            source.write_text(json.dumps(bad))
            archive = root/'bad.har.zip'
            make_archive(archive, bad, body)

            def refused(command, path, label, location):
                out = root/(label + '-' + command + ('.har.zip' if path.suffix == '.zip' and command == 'apply' else '.json'))
                args = [sys.executable, '-m', 'qawrench.har', command, str(path), '--out', str(out)]
                if command in ('plan', 'apply', 'preflight'):
                    args += ['--rules', str(rules), '--inventory', str(wanted)]
                before = {p: p.read_bytes() for p in root.iterdir()}
                result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(location + '.postData.parts[1024]: ' + ERROR, result.stderr)
                self.assertIn('re-record', result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertEqual({p: p.read_bytes() for p in root.iterdir()}, before)

            for path in (source, archive):
                for command in ('plan', 'apply', 'preflight', 'export-inventory') + (() if path == archive else ('embed-forms',)):
                    with self.subTest(path=path, command=command):
                        refused(command, path, path.name, 'log.entries[1].request')
            source.write_text(json.dumps(good))
            make_archive(archive, good, base64.b64decode(good['log']['entries'][0]['request']['postData']['text']))
            inventory['requests'].append({'id': 'bad', **{k: bad['log']['entries'][1]['request'][k]
                                                        for k in ('url', 'method', 'postData')}})
            wanted.write_text(json.dumps(inventory))
            for path in (source, archive):
                for command in ('plan', 'apply', 'preflight'):
                    with self.subTest(inventory=True, path=path, command=command):
                        refused(command, path, 'inventory-' + path.name, 'inventory.requests[1]')


if __name__ == '__main__':
    unittest.main()
