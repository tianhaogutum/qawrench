"""ZIP selection preserves complete manifest entries and selected member bytes."""
import contextlib
import copy
import io
import json
import os
import stat
import warnings
from unittest.mock import patch
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from qawrench.har import DoctorError
from qawrench.har.cli import main
from qawrench.har.fixtures import load_fixture

from .selection_zip_fixture import ROOT, fixture, write_zip


class ZipSelectionTests(unittest.TestCase):
    def test_mixed_shared_payload_selection_then_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, inv, members, expected = fixture()
            source, wanted, output = root/'source.har.zip', root/'inventory.json', root/'selected.har.zip'
            write_zip(source, data, members)
            wanted.write_text(json.dumps(inv))
            before = source.read_bytes(), wanted.read_bytes()
            run = subprocess.run([sys.executable, '-m', 'qawrench.har', 'select-requests', str(source),
                                  '--inventory', str(wanted), '--out', str(output)],
                                 cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout), {'command': 'select-requests', 'output': str(output),
                             'selectedEntries': 2, 'totalEntries': 6, 'retainedPayloadMembers': 1})
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.namelist(), ['Payload/é.bin', 'capture.har'])
                self.assertEqual(archive.read('Payload/é.bin'), members['Payload/é.bin'])
                self.assertEqual(json.loads(archive.read('capture.har')), expected)
            report = root/'preflight.json'
            run = subprocess.run([sys.executable, '-m', 'qawrench.har', 'preflight', str(output),
                                  '--inventory', str(wanted), '--out', str(report)],
                                 cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(json.loads(report.read_bytes())['passed'])
            self.assertEqual((source.read_bytes(), wanted.read_bytes()), before)
            self.assertFalse(list(root.glob('.*.tmp')))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data, self.inv, self.members, self.expected = fixture()
        self.source, self.wanted = self.root/'source.har.zip', self.root/'inventory.json'
        self.prepare()

    def prepare(self):
        write_zip(self.source, self.data, self.members)
        self.wanted.write_text(json.dumps(self.inv))

    def snapshot(self):
        return {p.name: ('link', os.readlink(p)) if p.is_symlink() else ('file', p.read_bytes())
                for p in self.root.iterdir()}

    def invoke(self, output, expected=2, error=None, inventory=None):
        before = self.snapshot()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(['select-requests', str(self.source), '--inventory',
                         str(inventory or self.wanted), '--out', str(output)])
        self.assertEqual(code, expected, stderr.getvalue())
        self.assertNotIn('Traceback', stderr.getvalue())
        if error:
            self.assertIn(error, stderr.getvalue())
        if expected == 2:
            self.assertEqual(stdout.getvalue(), '')
            self.assertEqual(self.snapshot(), before)
        return stdout.getvalue(), stderr.getvalue()

    def test_empty_attached_and_manifest_only_selection(self):
        # Zero-byte attachments remain real referenced members. Three references
        # (both sides and a second entry) retain the shared member exactly once.
        shared = 'Payload/é.bin'
        self.members[shared] = b''
        post = self.data['log']['entries'][1]['request']
        post['bodySize'] = 0
        content = self.data['log']['entries'][1]['response']['content']
        content.pop('text')
        content.update(_file=shared, size=0)
        self.data['log']['entries'][3]['response']['content']['size'] = 0
        self.inv['requests'][1]['postData']['text'] = ''
        self.prepare()
        output = self.root/'empty.har.zip'
        stdout, _ = self.invoke(output, expected=0)
        self.assertEqual(json.loads(stdout)['retainedPayloadMembers'], 1)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.namelist(), [shared, 'capture.har'])
            self.assertEqual(archive.read(shared), b'')
        self.inv['requests'] = [{'id': 'embedded', **{k: self.data['log']['entries'][4]['request'][k]
                                                   for k in ('url', 'method')}}]
        self.prepare()
        output = self.root/'manifest-only.har.zip'
        stdout, _ = self.invoke(output, expected=0)
        self.assertEqual(json.loads(stdout)['retainedPayloadMembers'], 0)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.namelist(), ['capture.har'])

    def test_missing_and_ambiguous_selectors_and_invalid_inventory(self):
        for mode in ('missing', 'attached-duplicate', 'embedded-duplicate', 'different-response'):
            self.data, self.inv, self.members, _ = fixture()
            if mode == 'missing':
                self.inv['requests'][-1]['url'] += '-missing'
            else:
                duplicate = copy.deepcopy(self.data['log']['entries'][1])
                if mode == 'embedded-duplicate':
                    duplicate['request']['postData'].pop('_file')
                    duplicate['request']['postData']['text'] = 'é\x00'
                if mode == 'different-response':
                    duplicate['response']['status'] = 201
                self.data['log']['entries'].append(duplicate)
            self.prepare()
            with self.subTest(mode=mode):
                _, stderr = self.invoke(self.root/'blocked.har.zip', error='missing' if mode == 'missing' else 'ambiguous')
                for req in self.inv['requests']:
                    self.assertNotIn(req['url'], stderr)
                    self.assertNotIn(req['id'], stderr)
        for value in (None, {}, {'schemaVersion': 1, 'requests': []},
                      {**self.inv, 'extra': True},
                      {'schemaVersion': 1, 'requests': [self.inv['requests'][0], {'id': 'late'}]}):
            self.data, _, self.members, _ = fixture()
            self.wanted.write_text(json.dumps(value))
            write_zip(self.source, self.data, self.members)
            with self.subTest(inventory=value):
                self.invoke(self.root/'invalid.har.zip', error='inventory')
        for raw in ('{', 'NaN', '{"requests":[],"requests":[]}'):
            self.wanted.write_text(raw)
            self.invoke(self.root/'invalid-json.har.zip')

    def test_late_unselected_and_selected_invalid_references_fail_before_output(self):
        for index in (1, 5):
            for field, value, error in (('_file', 'missing.bin', 'missing archive member'),
                                        ('_file', '../never-read', 'unsafe'),
                                        ('_file', '/never-read', 'unsafe'),
                                        ('_file', 'capture.har', 'manifest'),
                                        ('_file', 'CAPTURE.HAR', 'missing archive member'),
                                        ('_file', 'payload/é.bin', 'missing archive member'),
                                        ('_sha1', '/never-read', '_sha1'),
                                        ('text', 'ambiguous', 'nonempty text'),
                                        ('encoding', 'base64', 'encoding')):
                self.data, self.inv, self.members, _ = fixture()
                payload = (self.data['log']['entries'][1]['request']['postData'] if index == 1 else
                           self.data['log']['entries'][5]['response']['content'])
                payload[field] = value
                self.prepare()
                with self.subTest(index=index, field=field, value=value):
                    self.invoke(self.root/'reference.har.zip', error=error)
        for side, field, value in (('response', 'status', 0),
                                   ('request', 'postData', {'mimeType': 'text/plain', 'params': []}),
                                   ('response', 'content', {'mimeType': 'text/plain', 'text': '!', 'encoding': 'base64'})):
            self.data, self.inv, self.members, _ = fixture()
            self.data['log']['entries'][5][side][field] = value
            self.prepare()
            self.invoke(self.root/'late.har.zip', error='log.entries[5]')

    def test_invalid_unselected_archive_members_and_resource_limits(self):
        for name, error in (('../unsafe.bin', 'unsafe'), ('PAYLOAD/É.BIN', 'case-ambiguous'),
                            ('nested/extra.har', 'exactly one')):
            self.data, self.inv, self.members, _ = fixture()
            self.members[name] = b'bad'
            self.prepare()
            with self.subTest(name=name):
                self.invoke(self.root/'members.har.zip', error=error)
        self.data, self.inv, self.members, _ = fixture()
        self.prepare()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(self.source, 'a') as archive:
                archive.writestr('unused.bin', b'duplicate')
        self.invoke(self.root/'duplicate.har.zip', error='duplicate archive member')
        for kind in ('symlink', 'compression', 'crc'):
            self.prepare()
            if kind == 'crc':
                self.source.write_bytes(self.source.read_bytes().replace(b'never retained', b'badly corrupt!', 1))
            else:
                with zipfile.ZipFile(self.source, 'a') as archive:
                    info = zipfile.ZipInfo('bad.bin')
                    if kind == 'symlink':
                        info.create_system = 3
                        info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    else:
                        info.compress_type = zipfile.ZIP_BZIP2
                    archive.writestr(info, b'bad')
            self.invoke(self.root/'invalid-member.har.zip', error={'crc': 'CRC', 'symlink': 'regular',
                                                                  'compression': 'compression'}[kind])
        self.prepare()
        for setting, value in (('MAX_ARCHIVE_BYTES', 1), ('MAX_MEMBERS', 2),
                               ('MAX_MEMBER_BYTES', 1), ('MAX_TOTAL_BYTES', 1)):
            with self.subTest(setting=setting), patch('qawrench.har.fixtures.'+setting, value):
                self.invoke(self.root/'limit.har.zip', error='limit')
        # Input archive stays under 2 MiB, resolved references exceed it even
        # though the repeated selected/unselected payloads would be discarded.
        self.data, self.inv, self.members, _ = fixture()
        self.members['late.bin'] = b'x' * (1024 * 1024)
        for _ in range(2):
            self.data['log']['entries'].append(copy.deepcopy(self.data['log']['entries'][5]))
        self.prepare()
        with patch('qawrench.har.fixtures.MAX_TOTAL_BYTES', 2 * 1024 * 1024):
            self.invoke(self.root/'reference-limit.har.zip', error='cumulative referenced')

    def test_paths_output_failures_race_and_no_capture_reads(self):
        for kind in ('source', 'inventory', 'hard-source', 'hard-inventory', 'symlink-source',
                     'symlink-inventory', 'dangling', 'existing', 'wrong-suffix', 'missing-directory'):
            output = self.root/(kind+'.har.zip')
            if kind == 'source':
                output = self.source
            elif kind == 'inventory':
                output = self.wanted
            elif kind.startswith('hard-'):
                os.link(self.source if kind == 'hard-source' else self.wanted, output)
            elif kind.startswith('symlink-'):
                output.symlink_to(self.source if kind == 'symlink-source' else self.wanted)
            elif kind == 'dangling':
                output.symlink_to(self.root/'missing')
            elif kind == 'existing':
                output.write_bytes(b'keep')
            elif kind == 'wrong-suffix':
                output = self.root/'out.zip'
            elif kind == 'missing-directory':
                output = self.root/'missing'/'out.har.zip'
            with self.subTest(kind=kind):
                self.invoke(output)
        for name in ('source', 'hard-source', 'symlink-source'):
            alias = self.source if name == 'source' else self.root/(name+'.har.zip')
            self.invoke(self.root/'inputs-alias.har.zip', inventory=alias, error='input and inventory must differ')
        for method in ('os.link', 'os.fsync'):
            with patch('qawrench.har.core.'+method, side_effect=OSError('injected failure')):
                self.invoke(self.root/'failure.har.zip', error='cannot write')
        with patch('qawrench.har.fixtures.zipfile.ZipFile.writestr', side_effect=OSError('disk full')):
            self.invoke(self.root/'disk-full.har.zip', error='cannot write')
        loaded = load_fixture(self.source)
        for setting, value in (('MAX_MEMBER_BYTES', 1), ('MAX_TOTAL_BYTES', 1), ('MAX_ARCHIVE_BYTES', 1)):
            before = self.snapshot()
            with patch('qawrench.har.fixtures.'+setting, value):
                with self.assertRaisesRegex(DoctorError, 'limit'):
                    loaded.write_selection(self.inv, self.root/'output-limit.har.zip')
            self.assertEqual(self.snapshot(), before)
        real_open = Path.open
        def guard(path, *args, **kwargs):
            self.assertIn(path, (self.source, self.wanted))
            return real_open(path, *args, **kwargs)
        with patch.object(Path, 'open', guard), patch('socket.socket', side_effect=AssertionError('network')), \
                patch.object(zipfile.ZipFile, 'extract', side_effect=AssertionError('extract')), \
                patch.object(zipfile.ZipFile, 'extractall', side_effect=AssertionError('extractall')):
            # Writing uses the existing tempfile stream, never a capture path.
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(['select-requests', str(self.source), '--inventory', str(self.wanted),
                                       '--out', str(self.root/'guarded.har.zip')]), 0, stderr.getvalue())
        target = self.root/'racing.har.zip'
        real_link = os.link
        def race(source, output):
            Path(output).write_bytes(b'other writer')
            return real_link(source, output)
        with patch('qawrench.har.core.os.link', side_effect=race):
            with self.assertRaisesRegex(DoctorError, 'cannot write'):
                loaded.write_selection(self.inv, target, protected=[self.source, self.wanted])
        self.assertEqual(target.read_bytes(), b'other writer')
        self.assertFalse(list(self.root.glob('.*.tmp')))

    def test_distinct_payloads_keep_source_order_around_manifest(self):
        content = self.data['log']['entries'][1]['response']['content']
        content.pop('text')
        content.update(_file='late.bin', size=len(self.members['late.bin']))
        self.prepare()
        output = self.root/'distinct.har.zip'
        stdout, _ = self.invoke(output, expected=0)
        self.assertEqual(json.loads(stdout)['retainedPayloadMembers'], 2)
        expected = copy.deepcopy(self.data)
        expected['log']['entries'] = [expected['log']['entries'][i] for i in (1, 3)]
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.namelist(), ['Payload/é.bin', 'capture.har', 'late.bin'])
            self.assertEqual(json.loads(archive.read('capture.har')), expected)
            for name in ('Payload/é.bin', 'late.bin'):
                self.assertEqual(archive.read(name), self.members[name])
