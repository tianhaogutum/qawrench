"""Bounded part-header validation must never rebuild embedded upload bytes."""
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
from examples.har.fixtures import RULES, entry, har
from .test_multipart import BODY, ROOT, upload


def two_parts(headers, payload=b'\x80\x00\xff'):
    return (BODY.replace(b'--upload:42--\r\n', b'--upload:42\r\n')
            + headers + b'\r\n\r\n' + payload + b'\r\n--upload:42--\r\n')


class PartHeaderTests(unittest.TestCase):
    def test_missing_disposition_and_name_report_containing_request_and_part(self):
        for headers, diagnostic in [(b'Content-Type: application/octet-stream', 'missing Content-Disposition'),
                                    (b'Content-Disposition: form-data; filename="x.bin"', 'missing name parameter')]:
            source = har([entry('https://stage.example/a'), upload(two_parts(headers))])
            saved = copy.deepcopy(source)
            inventory = {'schemaVersion': 1, 'requests': [
                {'id': 'valid', 'method': 'GET', 'url': 'https://stage.example/a'},
                {'id': 'broken', **{k: source['log']['entries'][1]['request'][k]
                                   for k in ('url', 'method', 'postData')}}]}
            for operation in (doctor.validate_har, doctor.embed_form_params, doctor.export_inventory,
                              lambda h: doctor.build_plan(h, RULES),
                              lambda h: doctor.rewrite_har(h, RULES),
                              lambda h: doctor.build_preflight(h, inventory)):
                with self.subTest(headers=headers, operation=operation), self.assertRaisesRegex(
                        doctor.DoctorError, re.escape('log.entries[1].request.postData.parts[1]: ' + diagnostic)):
                    operation(source)
            with self.assertRaisesRegex(doctor.DoctorError, re.escape(
                    'inventory.requests[1].postData.parts[1]: ' + diagnostic)):
                doctor.validate_inventory(inventory)
            self.assertEqual(source, saved)

    def test_header_and_disposition_grammar_rejects_ambiguous_or_malformed_capture(self):
        valid = b'Content-Disposition: form-data; name=file'
        cases = [(b'Content-Type: text/plain', 'missing Content-Disposition'),
                 (b'', 'missing Content-Disposition'),
                 (b'Content-Disposition: attachment; name=file', 'expected form-data'),
                 (b'Content-Disposition: form-data; name*=utf-8', 'missing name parameter'),
                 (valid + b'\r\nContent-Disposition: form-data; name=file', 'duplicate part header'),
                 (valid + b'\r\nX-A: one\r\nx-a: one', 'duplicate part header'),
                 (valid + b'; NAME=other', 'duplicate Content-Disposition parameter'),
                 (valid + b'; filename=a; FILENAME=b', 'duplicate Content-Disposition parameter'),
                 (b'Content-Disposition: form-data;\r\n name=file', 'folded part header'),
                 (b' Content-Disposition: form-data; name=file', 'folded part header'),
                 (b'Content-Disposition : form-data; name=file', 'malformed part header'),
                 (valid + b'\nX-A: value', 'malformed part header'),
                 (valid + b'\rX-A: value', 'malformed part header'),
                 (valid + b'\r\nX-A: \x00', 'malformed part header'),
                 (valid + b'\r\nX-A: \x7f', 'malformed part header'),
                 (b'Content-Disposition: form-data; name="broken', 'malformed Content-Disposition'),
                 (b'Content-Disposition: form-data; name=a b', 'malformed Content-Disposition'),
                 (b'Content-Disposition: form-data; name="bad\\\x01"', 'malformed part header')]
        for headers, diagnostic in cases:
            with self.subTest(headers=headers), self.assertRaisesRegex(
                    doctor.DoctorError, r'postData.parts\[1\].*' + diagnostic):
                doctor.validate_har(har([upload(two_parts(headers))]))
        no_separator = BODY.replace(b'\r\n\r\n', b'\r\n')
        with self.assertRaisesRegex(doctor.DoctorError, r'parts\[0\].*missing.*header/body separator'):
            doctor.validate_har(har([upload(no_separator)]))
        adjacent = b'--upload:42\r\n--upload:42--\r\n'
        with self.assertRaisesRegex(doctor.DoctorError, r'parts\[0\].*header/body separator'):
            doctor.validate_har(har([upload(adjacent)]))

    def test_supported_headers_binary_and_empty_payloads_are_preserved_without_file_reads(self):
        headers = (b'cOnTeNt-DiSpOsItIoN:\tFoRm-DaTa ; NaMe=""; filename="a;\\"b.bin"; x-extra=token\t\r\n'
                   b'X-Opaque: \xff\x80\tvalue\r\nContent-Type: application/octet-stream')
        for payload in (b'', b'\x00\xff\r\n--upload:42-extra\r\nContent-Disposition: attachment\n\x7f', 'é'.encode()):
            source = har([upload(two_parts(headers, payload))])
            source['log']['entries'][0]['request']['postData']['params'][0]['fileName'] = '/never/read/private.bin'
            saved = copy.deepcopy(source)
            # In-memory validation and rewrites cannot consult fileName or filename.
            with patch.object(Path, 'open', side_effect=AssertionError('unexpected file read')):
                result, _ = doctor.rewrite_har(source, RULES)
                self.assertEqual(doctor.embed_form_params(source), saved)
                exported = doctor.export_inventory(source)
                self.assertEqual(exported['requests'][0]['postData'], saved['log']['entries'][0]['request']['postData'])
            self.assertEqual(result['log']['entries'][0]['request']['postData'], saved['log']['entries'][0]['request']['postData'])
            self.assertEqual(source, saved)
        text_body = two_parts(b'Content-Disposition: form-data; name=field', b'')
        item = upload(text_body)
        item['request']['postData'].pop('encoding')
        item['request']['postData']['text'] = text_body.replace(b'\x00\xff', b'plain').decode()
        self.assertEqual(doctor.embed_form_params(har([item])), har([item]))

    def test_later_invalid_part_blocks_every_cli_publication_in_json_zip_and_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rules = root / 'rules.json'
            rules.write_text(json.dumps(RULES))
            source, wanted = root / 'source.har', root / 'requests.json'
            good = har([upload()])
            inventory = doctor.export_inventory(good)
            wanted.write_text(json.dumps(inventory))
            for label, headers, diagnostic in (
                    ('disposition', b'Content-Type: application/octet-stream', 'missing Content-Disposition'),
                    ('name', b'Content-Disposition: form-data; filename=x.bin', 'missing name parameter')):
                bad = har([upload(), upload(two_parts(headers))])
                source.write_text(json.dumps(bad))
                saved = source.read_bytes()
                for command in ('plan', 'apply', 'preflight', 'embed-forms', 'export-inventory'):
                    out = root / (label + '-' + command + '.json')
                    args = [sys.executable, '-m', 'qawrench.har', command, str(source), '--out', str(out)]
                    if command in ('plan', 'apply', 'preflight'):
                        args.extend(['--rules', str(rules), '--inventory', str(wanted)])
                    run = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=20)
                    self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                    self.assertIn('log.entries[1].request.postData.parts[1]: ' + diagnostic, run.stderr)
                    self.assertFalse(out.exists())
                    self.assertNotIn('Traceback', run.stderr)
                self.assertEqual(source.read_bytes(), saved)
            # The same shared validation applies after ZIP _file resolution.
            attached = copy.deepcopy(bad)
            attached['log']['entries'][1]['request']['postData'] = {'mimeType': 'multipart/form-data; boundary="upload:42"', '_file': 'upload.bin', 'text': ''}
            archive = root / 'bad.har.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('recording.har', json.dumps(attached))
                z.writestr('upload.bin', two_parts(headers))
            for command in ('plan', 'apply', 'preflight', 'export-inventory'):
                out = root / ('zip-' + command + ('.har.zip' if command == 'apply' else '.json'))
                args = [sys.executable, '-m', 'qawrench.har', command, str(archive), '--out', str(out)]
                if command in ('plan', 'apply', 'preflight'):
                    args.extend(['--rules', str(rules), '--inventory', str(wanted)])
                run = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=20)
                self.assertEqual(run.returncode, 2, run.stderr)
                self.assertIn('log.entries[1].request.postData.parts[1]: missing name parameter', run.stderr)
                self.assertFalse(out.exists())
            source.write_text(json.dumps(good))
            inventory['requests'].append({'id': 'bad', **{k: bad['log']['entries'][1]['request'][k] for k in ('url', 'method', 'postData')}})
            wanted.write_text(json.dumps(inventory))
            for command in ('plan', 'apply', 'preflight'):
                out = root / ('inventory-' + command + '.json')
                run = subprocess.run([sys.executable, '-m', 'qawrench.har', command, str(source),
                                      '--rules', str(rules), '--inventory', str(wanted), '--out', str(out)],
                                     cwd=ROOT, capture_output=True, text=True, timeout=20)
                self.assertEqual(run.returncode, 2, run.stderr)
                self.assertIn('inventory.requests[1].postData.parts[1]: missing name parameter', run.stderr)
                self.assertFalse(out.exists())
            self.assertFalse(list(root.glob('.*.tmp')))


if __name__ == '__main__':
    unittest.main()
