"""Inventory selection: exact unique keys, complete entries and atomic publication."""
import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import qawrench.har as doctor
from qawrench.har.cli import main
from qawrench.har.core import atomic_write_json
from examples.har.fixtures import entry, har

ROOT = Path(__file__).resolve().parents[2]


def selection_fixture():
    source = json.loads((ROOT / 'examples/har/selection-source.har').read_bytes())
    wanted = json.loads((ROOT / 'examples/har/selection-requests.json').read_bytes())
    return source, wanted


def expected_selection(source):
    expected = copy.deepcopy(source)
    expected['log']['entries'] = [expected['log']['entries'][i] for i in (1, 3)]
    return expected


class SelectionTests(unittest.TestCase):
    def test_unique_selection_copies_complete_entries_and_metadata_in_capture_order(self):
        source, wanted = selection_fixture()
        saved = copy.deepcopy((source, wanted))
        selected = doctor.select_requests(source, wanted)
        self.assertEqual(selected, expected_selection(source))
        self.assertEqual((source, wanted), saved)
        self.assertTrue(doctor.build_preflight(selected, wanted)['passed'])
        selected['log']['creator']['name'] = 'mutated'
        selected['log']['entries'][0]['response']['headers'][0]['value'] = 'mutated'
        selected['_synthetic'] = False
        self.assertEqual((source, wanted), saved)

    def test_exact_key_equivalence_and_literal_distinctions(self):
        # All methods include exact body bytes; headers and MIME do not select.
        url = 'https://STAGE.example:443/a?x=a+b#frag'
        source = har([entry(url, 'GET', 'é\x00'), entry(url, 'get', 'é\x00'),
                      entry(url, 'GET', 'other'), entry(url.replace('a+b', 'a%20b'), 'GET', 'é\x00'),
                      entry(url.replace('STAGE', 'stage'), 'GET', 'é\x00'), entry(url, 'POST', 'é\x00')])
        req = {'id': 'selector', 'url': url, 'method': 'GET',
               'headers': [{'name': 'X-Different', 'value': 'ignored'}],
               'postData': {'mimeType': 'application/octet-stream', 'encoding': 'base64',
                            'text': base64.b64encode('é\x00'.encode()).decode()}}
        selected = doctor.select_requests(source, {'schemaVersion': 1, 'requests': [req]})
        self.assertEqual(selected['log']['entries'], source['log']['entries'][:1])
        for post in (None, {'mimeType': 'text/plain', 'text': ''},
                     {'mimeType': 'text/plain', 'text': '', 'encoding': 'base64'}):
            empty = har([entry(url)])
            wanted = {'schemaVersion': 1, 'requests': [{'id': 'empty', 'url': url, 'method': 'GET'}]}
            if post is not None:
                wanted['requests'][0]['postData'] = post
            self.assertEqual(doctor.select_requests(empty, wanted), empty)

    def test_missing_ambiguous_even_identical_responses_and_no_mutation(self):
        source, wanted = selection_fixture()
        for mode in ('missing', 'identical', 'different'):
            h, inv = copy.deepcopy((source, wanted))
            if mode == 'missing':
                inv['requests'][-1]['url'] += '-absent'
            else:
                h['log']['entries'].append(copy.deepcopy(h['log']['entries'][3]))
                if mode == 'different':
                    h['log']['entries'][-1]['response']['status'] = 201
            saved = copy.deepcopy((h, inv))
            with self.subTest(mode=mode), self.assertRaisesRegex(
                    doctor.DoctorError, r'inventory.requests\[\d+\].*(missing|ambiguous).*exact') as caught:
                doctor.select_requests(h, inv)
            self.assertNotIn(inv['requests'][-1]['url'], str(caught.exception))
            self.assertNotIn(inv['requests'][0]['id'], str(caught.exception))
            self.assertEqual((h, inv), saved)
        with self.assertRaisesRegex(doctor.DoctorError, 'missing'):
            doctor.select_requests(har([]), wanted)

    def test_entire_har_and_inventory_validated_including_late_unselected_records(self):
        source, wanted = selection_fixture()
        changes = [('response', 'content', {'mimeType': 'text/plain', 'text': '!', 'encoding': 'base64'}),
                   ('request', 'postData', {'mimeType': 'text/plain', 'params': []}),
                   ('request', 'postData', {'mimeType': 'text/plain', 'text': '', '_file': '/never/read'}),
                   ('request', 'postData', {'mimeType': 'text/plain', 'text': '', '_sha1': '/never/read'}),
                   ('request', 'postData', {'mimeType': 'multipart/form-data; boundary=b', 'text': 'bad'}),
                   ('response', 'status', 0)]
        for side, field, value in changes:
            h = copy.deepcopy(source)
            h['log']['entries'][-1][side][field] = value
            saved = copy.deepcopy(h)
            with self.subTest(field=field, value=value), self.assertRaisesRegex(
                    doctor.DoctorError, r'log.entries\[5\]'):
                doctor.select_requests(h, wanted)
            self.assertEqual(h, saved)
        for inv in (None, {}, {'schemaVersion': 1, 'requests': []},
                    {**wanted, 'unknown': True},
                    {'schemaVersion': 1, 'requests': wanted['requests'] + [{'id': 'late'}]}):
            with self.subTest(inventory=inv), self.assertRaises(doctor.DoctorError):
                doctor.select_requests(source, inv)


class SelectionCLITests(unittest.TestCase):
    def invoke(self, source, wanted, output, *extra):
        return subprocess.run([sys.executable, '-m', 'qawrench.har', 'select-requests',
                               str(source), '--inventory', str(wanted), '--out', str(output), *extra],
                              cwd=ROOT, capture_output=True, text=True, timeout=20)

    def prepare(self, root):
        source, wanted = root / 'source.har', root / 'requests.json'
        h, inv = selection_fixture()
        source.write_text(json.dumps(h, ensure_ascii=False))
        wanted.write_text(json.dumps(inv, ensure_ascii=False))
        return source, wanted, h, inv

    def test_two_of_several_entries_select_then_preflight_without_changing_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, wanted, h, _ = self.prepare(root)
            original = (source.read_bytes(), wanted.read_bytes())
            output = root / 'selected.har'
            run = self.invoke(source, wanted, output)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout), {'command': 'select-requests', 'output': str(output),
                                                     'selectedEntries': 2, 'totalEntries': 6})
            self.assertEqual(run.stderr, '')
            self.assertEqual(json.loads(output.read_bytes()), expected_selection(h))
            report = root / 'preflight.json'
            preflight = subprocess.run([sys.executable, '-m', 'qawrench.har', 'preflight', str(output),
                                        '--inventory', str(wanted), '--out', str(report)], cwd=ROOT,
                                       capture_output=True, text=True, timeout=20)
            self.assertEqual(preflight.returncode, 0, preflight.stderr)
            self.assertTrue(json.loads(report.read_bytes())['passed'])
            self.assertEqual((source.read_bytes(), wanted.read_bytes()), original)
            self.assertFalse(list(root.glob('.*.tmp')))

    def test_failure_never_publishes_or_mutates_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, wanted, h, inv = self.prepare(root)
            # Unique per case; later malformed records cannot be hidden by selection.
            cases = []
            missing = copy.deepcopy(inv)
            missing['requests'][-1]['url'] += '-missing'
            cases.append((h, missing, 'missing'))
            duplicate = copy.deepcopy(h)
            duplicate['log']['entries'].append(copy.deepcopy(h['log']['entries'][3]))
            cases.append((duplicate, inv, 'ambiguous'))
            bad = copy.deepcopy(h)
            bad['log']['entries'][-1]['response']['content']['encoding'] = 'gzip'
            cases.append((bad, inv, 'encoding'))
            invalid = copy.deepcopy(inv)
            invalid['requests'][-1]['postData'] = {'mimeType': 'text/plain', 'params': []}
            cases.append((h, invalid, 'params-only'))
            cases.extend([(h, None, 'inventory'), (h, {**inv, 'extra': 1}, 'unknown fields')])
            for i, (data, requests, error) in enumerate(cases):
                with self.subTest(error=error):
                    source.write_text(json.dumps(data))
                    wanted.write_text(json.dumps(requests))
                    before = {p.name: p.read_bytes() for p in root.iterdir()}
                    output = root / f'blocked-{i}.har'
                    run = self.invoke(source, wanted, output)
                    self.assertEqual(run.returncode, 2, run.stderr)
                    self.assertIn(error, run.stderr)
                    self.assertNotIn('Traceback', run.stderr)
                    self.assertEqual(run.stdout, '')
                    self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)
            source.write_text(json.dumps(h))
            for raw in ('{"schemaVersion":1,"requests":[],"requests":[]}', '{', 'NaN'):
                wanted.write_text(raw)
                before = (source.read_bytes(), wanted.read_bytes())
                run = self.invoke(source, wanted, root / 'invalid.har')
                self.assertEqual(run.returncode, 2)
                self.assertFalse((root / 'invalid.har').exists())
                self.assertEqual((source.read_bytes(), wanted.read_bytes()), before)

    def test_paths_flags_and_invalid_zip_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, wanted, _, _ = self.prepare(root)
            existing = root / 'existing.har'
            existing.write_bytes(b'keep')
            hard = root / 'hard.har'
            os.link(source, hard)
            symbolic = root / 'symbolic.har'
            symbolic.symlink_to(wanted)
            dangling = root / 'dangling.har'
            dangling.symlink_to(root / 'absent')
            originals = (source.read_bytes(), wanted.read_bytes())
            for target in (source, wanted, existing, hard, symbolic, dangling, root/'new.har.zip',
                           root/'absent-directory'/'out.har'):
                with self.subTest(target=target):
                    run = self.invoke(source, wanted, target)
                    self.assertEqual(run.returncode, 2, run.stderr)
                    self.assertEqual((source.read_bytes(), wanted.read_bytes()), originals)
                    self.assertEqual(existing.read_bytes(), b'keep')
                    self.assertFalse(list(root.glob('.*.tmp')))
            self.assertTrue(dangling.is_symlink())
            self.assertFalse((root/'absent').exists())
            for alias in (source, hard):
                run = self.invoke(source, alias, root/'alias.har')
                self.assertEqual(run.returncode, 2)
                self.assertIn('input and inventory must differ', run.stderr)
                self.assertFalse((root/'alias.har').exists())
            for extra in (('--rules', 'never-read.json'), ('--unknown',)):
                run = self.invoke(source, wanted, root/'flags.har', *extra)
                self.assertEqual(run.returncode, 2)
                self.assertFalse((root/'flags.har').exists())
            # Invalid archives and non-.har.zip paths still fail closed.
            for name in ('input.har.zip', 'input.ZIP'):
                archive = root/name
                archive.write_bytes(b'PK\x03\x04not parsed')
                run = self.invoke(archive, wanted, root/'zip-output.har')
                self.assertEqual(run.returncode, 2)
                self.assertIn('cannot read', run.stderr)
                self.assertFalse((root/'zip-output.har').exists())

    def test_no_capture_filename_reads_and_atomic_publication_failure_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, wanted, _, _ = self.prepare(root)
            # Library matching/copying cannot open filename metadata or contact a network.
            h, inv = selection_fixture()
            with patch('builtins.open', side_effect=AssertionError('unexpected file read')), \
                    patch('socket.socket', side_effect=AssertionError('unexpected network')):
                self.assertEqual(doctor.select_requests(h, inv), expected_selection(h))
            output = root/'failure.har'
            with patch('qawrench.har.core.os.link', side_effect=OSError('publication failed')):
                with self.assertRaisesRegex(doctor.DoctorError, 'cannot write'):
                    atomic_write_json(expected_selection(h), output, protected=[source, wanted])
            self.assertFalse(output.exists())
            self.assertFalse(list(root.glob('.*.tmp')))


if __name__ == '__main__':
    unittest.main()
