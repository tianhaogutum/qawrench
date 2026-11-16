"""Inventory export contracts: literal exact keys, first occurrence, no inferred bytes."""
import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from examples.har.fixtures import entry, har
import qawrench.har as doctor

ROOT = Path(__file__).resolve().parents[2]
URL = 'https://STAGE.example:443/a?x=a+b&x=a%20b&cacheBust=1#frag'


def export_source():
    entries = [entry(URL, 'POST', 'é\x00'), entry(URL, 'GET'),
               entry(URL, 'POST', 'unused', response_text='different response'),
               entry(URL, 'POST', ' é\x00'), entry(URL, 'post', 'é\x00'),
               entry(URL, 'GET', ''), entry(URL, 'GET', 'body'),
               entry(URL, 'PUT', ''), entry(URL.replace('a+b', 'a%20b'), 'GET')]
    entries[2]['request']['postData'] = {
        'mimeType': 'application/octet-stream', 'encoding': 'base64',
        'text': base64.b64encode('é\x00'.encode()).decode()}
    entries[7]['request']['postData'] = {
        'mimeType': 'application/octet-stream', 'encoding': 'base64', 'text': 'AP8NgA=='}
    entries[2]['request']['bodySize'] = len('é\x00'.encode())
    entries[7]['request']['bodySize'] = 4
    return har(entries)


class ExportTests(unittest.TestCase):
    def test_exact_keys_first_entry_order_and_lossless_independent_output(self):
        source = export_source()
        original = copy.deepcopy(source)
        result = doctor.export_inventory(source)
        indexes = [0, 1, 3, 4, 6, 7, 8]
        expected = []
        for i in indexes:
            req = source['log']['entries'][i]['request']
            exported = {'id': f'/log/entries/{i}', 'url': req['url'], 'method': req['method']}
            if 'postData' in req:
                exported['postData'] = copy.deepcopy(req['postData'])
            expected.append(exported)
        self.assertEqual(result, {'schemaVersion': 1, 'requests': expected})
        doctor.validate_inventory(result)
        self.assertEqual(source, original)
        self.assertEqual(doctor.export_inventory(source), result)
        self.assertEqual([doctor.request_key(r) for r in result['requests']],
                         [doctor.request_key(source['log']['entries'][i]['request']) for i in indexes])
        self.assertEqual(base64.b64decode(result['requests'][5]['postData']['text']), b'\x00\xff\r\x80')
        result['requests'][0]['postData']['params'].append({'name': 'new', 'value': 'value'})
        self.assertEqual(source, original)

    def test_preflight_retains_duplicate_ambiguity_and_unique_matches(self):
        source = export_source()
        report = doctor.build_preflight(source, doctor.export_inventory(source))
        self.assertFalse(report['passed'])
        self.assertEqual(report['summary'], {'requests': 7, 'matchedRequests': 5,
                                             'missingRequests': 0, 'ambiguousRequests': 2})
        self.assertEqual([r['entryIndexes'] for r in report['requests']],
                         [[0, 2], [1, 5], [3], [4], [6], [7], [8]])
        unique = har([entry(URL, 'GET', '', response_text='unique')])
        # First explicit empty postData is retained, just as first missing postData is omitted.
        self.assertEqual(doctor.export_inventory(unique)['requests'][0]['postData']['text'], '')
        self.assertTrue(doctor.build_preflight(unique, doctor.export_inventory(unique))['passed'])

    def test_empty_and_later_invalid_entries_fail_without_mutating_input(self):
        with self.assertRaisesRegex(doctor.DoctorError, 'log.entries.*empty HAR.*inventor.*nonempty'):
            doctor.export_inventory(har([]))
        changes = [({'mimeType': 'text/plain', 'params': []}, 'params-only'),
                   ({'mimeType': 'text/plain', 'text': 'x', '_file': 'never-read'}, 'attached'),
                   ({'mimeType': 'text/plain', 'text': 'x', '_sha1': 'never-read'}, 'attached'),
                   ({'mimeType': 'text/plain', 'text': '!', 'encoding': 'base64'}, 'base64'),
                   ({'mimeType': 'multipart/form-data; boundary=x', 'text': 'bad'}, 'boundary')]
        for payload, error in changes:
            with self.subTest(payload=payload):
                source = har([entry(URL), entry(URL)])
                source['log']['entries'][1]['request']['postData'] = payload
                saved = copy.deepcopy(source)
                with self.assertRaisesRegex(doctor.DoctorError, r'log.entries\[1\].*' + error):
                    doctor.export_inventory(source)
                self.assertEqual(source, saved)
        source = har([entry(URL)])
        source['log']['entries'][0]['response']['content']['_file'] = 'never-read'
        with self.assertRaisesRegex(doctor.DoctorError, 'response.content.*attached'):
            doctor.export_inventory(source)


class ExportCLITests(unittest.TestCase):
    def run_export(self, source, output, *options):
        return subprocess.run([sys.executable, '-m', 'qawrench.har', 'export-inventory',
                               str(source), '--out', str(output), *options], cwd=ROOT,
                              capture_output=True, text=True, timeout=20)

    def test_export_cli_is_deterministic_literal_and_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'input.har'
            source.write_text(json.dumps(export_source(), ensure_ascii=False))
            original = source.read_bytes()
            first, second = root / 'first.json', root / 'second.json'
            for output in (first, second):
                run = self.run_export(source, output)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(json.loads(run.stdout), {'command': 'export-inventory',
                                                         'output': str(output), 'requests': 7})
            self.assertEqual(first.read_bytes(), second.read_bytes())
            doctor.validate_inventory(doctor.load_json(first))
            self.assertEqual(doctor.load_json(first), doctor.export_inventory(doctor.load_json(source)))
            self.assertEqual(source.read_bytes(), original)

    def test_cli_negative_inputs_publish_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / 'input.har', root / 'out.json'
            params = har([entry(URL), entry(URL, 'POST', '')])
            params['log']['entries'][1]['request']['postData'].pop('text')
            attached = har([entry(URL)])
            attached['log']['entries'][0]['response']['content']['_file'] = 'never-read'
            binary = har([entry(URL, 'POST', '')])
            binary['log']['entries'][0]['request']['postData'].update(encoding='base64', text='!')
            cases = [('{}', 'log'), ('{', 'cannot read JSON'),
                     ('{"log":{},"log":{}}', 'duplicate JSON'),
                     (json.dumps(har([])), 'empty HAR'), (json.dumps(params), 'params-only'),
                     (json.dumps(attached), 'attached'), (json.dumps(binary), 'base64')]
            for text, error in cases:
                with self.subTest(error=error):
                    source.write_text(text)
                    saved = source.read_bytes()
                    run = self.run_export(source, output)
                    self.assertEqual(run.returncode, 2, run.stderr)
                    self.assertIn(error, run.stderr)
                    self.assertNotIn('Traceback', run.stderr)
                    self.assertFalse(output.exists())
                    self.assertFalse(list(root.glob('.*.tmp')))
                    self.assertEqual(source.read_bytes(), saved)

    def test_existing_outputs_aliases_and_rewrite_flags_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'input.har'
            source.write_text(json.dumps(export_source()))
            original = source.read_bytes()
            existing = root / 'existing.json'
            existing.write_bytes(b'keep')
            hardlink, symlink, dangling = (root / name for name in ('hard', 'sym', 'dangling'))
            os.link(source, hardlink)
            symlink.symlink_to(source)
            dangling.symlink_to(root / 'missing')
            for output in (source, existing, hardlink, symlink, dangling):
                with self.subTest(output=output):
                    run = self.run_export(source, output)
                    self.assertEqual(run.returncode, 2, run.stderr)
                    self.assertRegex(run.stderr, 'output.*(exists|differ)')
                    self.assertEqual(source.read_bytes(), original)
                    self.assertEqual(existing.read_bytes(), b'keep')
                    self.assertTrue(dangling.is_symlink())
                    self.assertFalse((root / 'missing').exists())
                    self.assertFalse(list(root.glob('.*.tmp')))
            output = root / 'new.json'
            run = self.run_export(source, output, '--rules', 'not-read.json')
            self.assertEqual(run.returncode, 2)
            self.assertIn('unrecognized arguments', run.stderr)
            self.assertFalse(output.exists())
            run = self.run_export(source, root / 'absent' / 'new.json')
            self.assertEqual(run.returncode, 2)
            self.assertIn('cannot write', run.stderr)
            self.assertFalse((root / 'absent').exists())


if __name__ == '__main__':
    unittest.main()
