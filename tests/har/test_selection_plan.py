"""Value-free selection membership plans for JSON and fully checked HAR ZIPs."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from qawrench.har.cli import main
from qawrench.har.fixtures import load_fixture
from .selection_zip_fixture import ROOT, fixture, write_zip


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    separators=(',', ':'), allow_nan=False).encode('ascii')).hexdigest()


class SelectionPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data, self.inv, self.members, _ = fixture()
        self.source = self.root/'source.har.zip'
        self.wanted = self.root/'inventory.json'
        self.prepare()

    def prepare(self):
        write_zip(self.source, self.data, self.members)
        self.wanted.write_text(json.dumps(self.inv))

    def snapshot(self):
        return {p.name: ('link', os.readlink(p)) if p.is_symlink() else ('directory',) if p.is_dir() else ('file', p.read_bytes())
                for p in self.root.iterdir()}

    def invoke(self, output, expected=2, error=None, inventory=None):
        before = self.snapshot()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(['plan-selection', str(self.source), '--inventory',
                         str(inventory or self.wanted), '--out', str(output)])
        self.assertEqual(code, expected, stderr.getvalue())
        self.assertNotIn('Traceback', stderr.getvalue())
        if error:
            self.assertIn(error, stderr.getvalue())
        if expected == 2:
            self.assertEqual(stdout.getvalue(), '')
            self.assertEqual(self.snapshot(), before)
        else:
            self.assertEqual(stderr.getvalue(), '')
            self.assertEqual(self.source.read_bytes(), before[self.source.name][1])
            self.assertEqual(self.wanted.read_bytes(), before[self.wanted.name][1])
            return json.loads(output.read_bytes())

    def test_zip_cli_capture_order_value_free_report(self):
        output = self.root/'plan.json'
        before = self.snapshot()
        run = subprocess.run([sys.executable, '-m', 'qawrench.har', 'plan-selection', str(self.source),
                              '--inventory', str(self.wanted), '--out', str(output)],
                             cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        summary = {'totalEntries': 6, 'selectedEntries': 2, 'totalPayloadMembers': 4,
                   'retainedPayloadMembers': 1, 'discardedPayloadMembers': 3}
        self.assertEqual(json.loads(run.stdout), {'command': 'plan-selection', 'output': str(output), **summary})
        self.assertEqual(json.loads(output.read_bytes()), {
            'schemaVersion': 1, 'tool': 'har-replay-doctor', 'matchModel': 'exact-url-method-body-v1',
            'harSHA256': digest(load_fixture(self.source).har), 'inventorySHA256': digest(self.inv),
            'selectedEntryIndexes': [1, 3], 'selectedEntryIds': ['/log/entries/1', '/log/entries/3'],
            'summary': summary})
        self.assertEqual({k:v for k,v in self.snapshot().items() if k != 'plan.json'}, before)

    def test_json_plan_and_digests_bind_original_ids_without_mutation(self):
        self.source = self.root/'source.har'
        raw = (ROOT/'examples/har/selection-source.har').read_bytes()
        self.source.write_bytes(raw)
        plan = self.invoke(self.root/'json-plan.json', expected=0)
        self.assertEqual(plan['selectedEntryIndexes'], [1, 3])
        self.assertEqual(plan['summary'], {'totalEntries': 6, 'selectedEntries': 2})
        data = json.loads(raw)
        self.assertEqual(plan['harSHA256'], digest(data))
        self.assertEqual(plan['inventorySHA256'], digest(self.inv))
        self.source.write_text(json.dumps(data, sort_keys=True, indent=3))
        again = self.invoke(self.root/'format-plan.json', expected=0)
        self.assertEqual(plan, again)
        data['log']['entries'][-1]['comment'] = 'unselected metadata changes identity'
        self.source.write_text(json.dumps(data))
        self.inv['requests'][0]['id'] = 'different private identifier'
        self.wanted.write_text(json.dumps(self.inv))
        changed = self.invoke(self.root/'changed-plan.json', expected=0)
        self.assertNotEqual(plan['harSHA256'], changed['harSHA256'])
        self.assertNotEqual(plan['inventorySHA256'], changed['inventorySHA256'])
        self.assertEqual(plan['selectedEntryIds'], changed['selectedEntryIds'])
        self.assertEqual(self.source.read_bytes(), json.dumps(data).encode())

    def test_zero_byte_shared_and_embedded_only_members(self):
        self.members['Payload/é.bin'] = b''
        self.data['log']['entries'][1]['request']['bodySize'] = 0
        self.data['log']['entries'][3]['response']['content']['size'] = 0
        self.inv['requests'][1]['postData']['text'] = ''
        self.prepare()
        plan = self.invoke(self.root/'zero.json', expected=0)
        self.assertEqual(plan['summary']['retainedPayloadMembers'], 1)
        self.assertEqual(plan['summary']['discardedPayloadMembers'], 3)
        self.inv['requests'] = [{'id': 'embedded', **{k: self.data['log']['entries'][4]['request'][k]
                                                    for k in ('url','method')}}]
        self.prepare()
        plan = self.invoke(self.root/'embedded.json', expected=0)
        self.assertEqual(plan['selectedEntryIndexes'], [4])
        self.assertEqual(plan['summary']['retainedPayloadMembers'], 0)
        self.assertEqual(plan['summary']['discardedPayloadMembers'], 4)
        # Public adapter method is read-only even for in-memory arguments.
        loaded = load_fixture(self.source)
        before = copy.deepcopy((loaded.har, loaded._manifest, loaded._members, self.inv))
        self.assertEqual(loaded.build_selection_plan(self.inv), plan)
        self.assertEqual((loaded.har, loaded._manifest, loaded._members, self.inv), before)

    def test_missing_ambiguous_and_invalid_inventory_never_publish(self):
        for kind in ('missing', 'duplicate-same', 'duplicate-different'):
            self.data, self.inv, self.members, _ = fixture()
            if kind == 'missing':
                self.inv['requests'][-1]['url'] += '-missing'
            else:
                duplicate = copy.deepcopy(self.data['log']['entries'][1])
                if kind == 'duplicate-different':
                    duplicate['response']['status'] = 201
                self.data['log']['entries'].append(duplicate)
            self.prepare()
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(['plan-selection', str(self.source), '--inventory', str(self.wanted),
                                       '--out', str(self.root/'blocked.json')]), 2)
            self.assertEqual(stdout.getvalue(), '')
            self.assertFalse((self.root/'blocked.json').exists())
            for request in self.inv['requests']:
                self.assertNotIn(request['id'], stderr.getvalue())
                self.assertNotIn(request['url'], stderr.getvalue())
            self.invoke(self.root/'blocked.json', error='missing exact' if kind == 'missing' else 'ambiguous exact')
        for value in (None, {}, {'schemaVersion': 1, 'requests': []},
                      {**self.inv, 'extra': True},
                      {'schemaVersion':1, 'requests':[self.inv['requests'][0], {'id':'late'}]}):
            self.wanted.write_text(json.dumps(value))
            self.invoke(self.root/'invalid.json', error='inventory')
        for raw in ('{', 'NaN', '{"requests":[],"requests":[]}'):
            self.wanted.write_text(raw)
            self.invoke(self.root/'invalid.json')

    def test_late_unselected_validation_and_full_archive_limits(self):
        for field, value in (('_file', 'missing.bin'), ('_file', '../never-read'),
                             ('_file', 'capture.har'), ('_file', 'LATE.BIN'),
                             ('_sha1', '/never-read'), ('text', 'conflict'), ('encoding','base64')):
            self.data, self.inv, self.members, _ = fixture()
            self.data['log']['entries'][5]['response']['content'][field] = value
            self.prepare()
            self.invoke(self.root/'late.json', error='log.entries[5]')
        self.data, self.inv, self.members, _ = fixture()
        self.data['log']['entries'][5]['response']['status'] = 0
        self.prepare()
        self.invoke(self.root/'status.json', error='log.entries[5]')
        for name in ('../unsafe.bin', 'PAYLOAD/É.BIN', 'extra.har'):
            self.data, self.inv, self.members, _ = fixture()
            self.members[name] = b'bad'
            self.prepare()
            self.invoke(self.root/'member.json')
        self.data, self.inv, self.members, _ = fixture()
        self.prepare()
        self.source.write_bytes(self.source.read_bytes().replace(b'never retained', b'badly corrupt!', 1))
        self.invoke(self.root/'crc.json', error='CRC')
        self.prepare()
        for name, value in (('MAX_ARCHIVE_BYTES',1), ('MAX_MEMBERS',2),
                            ('MAX_MEMBER_BYTES',1), ('MAX_TOTAL_BYTES',1)):
            with self.subTest(limit=name), patch('qawrench.har.fixtures.'+name, value):
                self.invoke(self.root/'limit.json', error='limit')
        self.members['late.bin'] = b'x' * (1024 * 1024)
        for _ in range(2):
            self.data['log']['entries'].append(copy.deepcopy(self.data['log']['entries'][5]))
        self.prepare()
        with patch('qawrench.har.fixtures.MAX_TOTAL_BYTES', 2*1024*1024):
            self.invoke(self.root/'reference-limit.json', error='cumulative referenced')

    def test_json_late_unselected_and_loose_paths_fail(self):
        self.source = self.root/'source.har'
        original = json.loads((ROOT/'examples/har/selection-source.har').read_bytes())
        for side, field, value in (('response','status',0),
                ('response','content',{'mimeType':'text/plain','text':'!', 'encoding':'base64'}),
                ('request','postData',{'mimeType':'text/plain','params':[]}),
                ('request','postData',{'mimeType':'text/plain','_file':'/never-read'})):
            data = copy.deepcopy(original)
            data['log']['entries'][5][side][field] = value
            self.source.write_text(json.dumps(data))
            self.invoke(self.root/'json-invalid.json', error='log.entries[5]')

    def test_aliases_output_failures_race_and_no_capture_reads(self):
        for kind in ('source','inventory','hard-source','hard-inventory','symlink-source',
                     'symlink-inventory','dangling','existing','directory','missing-parent'):
            output = self.root/(kind+'.json')
            if kind in ('source','inventory'):
                output = self.source if kind == 'source' else self.wanted
            elif kind.startswith('hard-'):
                os.link(self.source if kind == 'hard-source' else self.wanted, output)
            elif kind.startswith('symlink-'):
                output.symlink_to(self.source if kind == 'symlink-source' else self.wanted)
            elif kind == 'dangling':
                output.symlink_to(self.root/'missing')
            elif kind == 'existing':
                output.write_bytes(b'keep')
            elif kind == 'directory':
                output.mkdir()
            elif kind == 'missing-parent':
                output = self.root/'missing-parent'/'out.json'
            self.invoke(output)
        for name in ('source','hard-source','symlink-source'):
            alias = self.source if name == 'source' else self.root/(name+'.json')
            self.invoke(self.root/'alias.json', inventory=alias, error='input and inventory must differ')
        for method in ('os.link','os.fsync'):
            with patch('qawrench.har.core.'+method, side_effect=OSError('injected failure')):
                self.invoke(self.root/'failure.json', error='cannot write')
        real_open = Path.open
        def guard(path, *args, **kwargs):
            self.assertIn(path, (self.source, self.wanted))
            return real_open(path, *args, **kwargs)
        with patch.object(Path,'open',guard), patch.object(socket,'socket',side_effect=AssertionError('network')), \
                patch.object(zipfile.ZipFile,'extract',side_effect=AssertionError('extract')), \
                patch.object(zipfile.ZipFile,'extractall',side_effect=AssertionError('extractall')):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(['plan-selection',str(self.source),'--inventory',str(self.wanted),
                                       '--out',str(self.root/'guarded.json')]),0,stderr.getvalue())
        target = self.root/'race.json'
        real_link = os.link
        def race(source, output):
            Path(output).write_bytes(b'other writer')
            return real_link(source, output)
        with patch('qawrench.har.core.os.link', side_effect=race):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(['plan-selection',str(self.source),'--inventory',str(self.wanted),
                                       '--out',str(target)]),2)
        self.assertEqual(target.read_bytes(),b'other writer')
        self.assertFalse(list(self.root.glob('.*.tmp')))
