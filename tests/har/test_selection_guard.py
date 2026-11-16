"""Reviewed-plan refusal, typed equality and input preservation contracts."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from qawrench.har.cli import main
from qawrench.har.fixtures import load_fixture
from .selection_zip_fixture import ROOT, fixture, write_zip

ERROR = 'qawrench har: guarded selection refused; validate inputs, plan and output path\n'


class SelectionGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'source.har.zip'
        self.wanted = self.root/'inventory.json'
        self.plan = self.root/'plan.json'
        self.data, self.inv, self.members, _ = fixture()
        self.prepare()
        self.report = load_fixture(self.source).build_selection_plan(self.inv)
        self.save_plan()

    def prepare(self):
        if str(self.source).endswith('.zip'):
            write_zip(self.source, self.data, self.members)
        else:
            self.source.write_text(json.dumps(self.data))
        self.wanted.write_text(json.dumps(self.inv))

    def save_plan(self):
        self.plan.write_text(json.dumps(self.report))

    def snapshot(self):
        return {p.name: ('link', os.readlink(p)) if p.is_symlink() else
                ('directory',) if p.is_dir() else ('file', p.read_bytes()) for p in self.root.iterdir()}

    def invoke(self, output=None, expected=2, plan=None, inventory=None):
        output = output or self.root/('selected.har.zip' if str(self.source).endswith('.zip') else 'selected.har')
        before = self.snapshot()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = main(['select-requests', str(self.source), '--inventory', str(inventory or self.wanted),
                             '--plan', str(plan or self.plan), '--out', str(output)])
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, expected, stderr.getvalue())
        if expected == 2:
            self.assertEqual(stdout.getvalue(), '')
            self.assertEqual(stderr.getvalue(), ERROR)
            self.assertEqual(self.snapshot(), before)
        else:
            self.assertEqual(stderr.getvalue(), '')
            self.assertEqual({k:v for k,v in self.snapshot().items() if k != output.name}, before)
            return output, json.loads(stdout.getvalue())

    def test_reviewed_json_and_shared_zip_success(self):
        for kind in ('zip', 'zero', 'embedded', 'json'):
            with self.subTest(kind=kind):
                self.data, self.inv, self.members, _ = fixture()
                if kind == 'zero':
                    self.members['Payload/é.bin'] = b''
                    self.data['log']['entries'][1]['request']['bodySize'] = 0
                    self.data['log']['entries'][3]['response']['content']['size'] = 0
                    self.inv['requests'][1]['postData']['text'] = ''
                if kind == 'embedded':
                    req = self.data['log']['entries'][4]['request']
                    self.inv['requests'] = [{'id':'only', 'url':req['url'], 'method':req['method']}]
                if kind == 'json':
                    self.source = self.root/'source.har'
                    self.data = json.loads((ROOT/'examples/har/selection-source.har').read_bytes())
                self.prepare()
                self.report = load_fixture(self.source).build_selection_plan(self.inv)
                # Object order/formatting do not matter.
                self.plan.write_text(json.dumps(self.report, sort_keys=True, indent=3))
                output, summary = self.invoke(self.root/(kind+'.har' if kind == 'json' else kind+'.har.zip'), 0)
                indexes = [4] if kind == 'embedded' else [1,3]
                expected = copy.deepcopy(self.data)
                expected['log']['entries'] = [expected['log']['entries'][i] for i in indexes]
                if kind == 'json':
                    self.assertEqual(json.loads(output.read_bytes()), expected)
                else:
                    with zipfile.ZipFile(output) as archive:
                        names = ['capture.har'] if kind == 'embedded' else ['Payload/é.bin','capture.har']
                        self.assertEqual(archive.namelist(), names)
                        self.assertEqual(json.loads(archive.read('capture.har')), expected)
                        for name in names:
                            if name != 'capture.har':
                                self.assertEqual(archive.read(name), self.members[name])
                self.assertEqual(summary['selectedEntries'], len(indexes))

    def test_every_field_is_required_typed_and_equal(self):
        original = copy.deepcopy(self.report)
        cases = [None, [], {}, {**original,'secret-unknown':'private-value'}]
        for key, value in original.items():
            missing = copy.deepcopy(original); del missing[key]; cases.append(missing)
            for bad in (None, True, 1.0, 'private-value', [], {}):
                if type(bad) is type(value) and bad == value:
                    continue
                changed = copy.deepcopy(original); changed[key] = bad; cases.append(changed)
        for key in original['summary']:
            for value in (True, float(original['summary'][key]), -1, original['summary'][key]+1):
                changed = copy.deepcopy(original); changed['summary'][key] = value; cases.append(changed)
            changed = copy.deepcopy(original); del changed['summary'][key]; cases.append(changed)
        changed = copy.deepcopy(original); changed['summary']['private-key'] = 1; cases.append(changed)
        for indexes in ([1.0,3], [True,3], [3,1], [1,1,3], [1,4], [1,3,99]):
            cases.append({**original, 'selectedEntryIndexes':indexes})
        for ids in (['/log/entries/3','/log/entries/1'], ['/log/entries/01','/log/entries/3']):
            cases.append({**original, 'selectedEntryIds':ids})
        for n, value in enumerate(cases):
            with self.subTest(case=n):
                self.plan.write_text(json.dumps(value)); self.invoke()
        for raw in ('{', 'NaN', '{"private-key":1,"private-key":2}',
                    json.dumps(original)[:-1]+',"schemaVersion":1}', '"\\ud800"'):
            with self.subTest(raw=raw):
                self.plan.write_text(raw); self.invoke()

    def test_stale_inputs_and_late_invalid_inputs(self):
        for kind in ('metadata','id','order','membership','missing','ambiguous','late-status',
                     'late-reference','unused-unsafe','late-inventory','member-count','retained-bytes'):
            with self.subTest(kind=kind):
                self.data, self.inv, self.members, _ = fixture()
                if kind == 'metadata': self.data['log']['entries'][5]['comment'] = 'private-value'
                elif kind == 'id': self.inv['requests'][0]['id'] = 'private-id'
                elif kind == 'order': self.inv['requests'].reverse()
                elif kind == 'membership':
                    req = self.data['log']['entries'][4]['request']
                    self.inv['requests'] = [{'id':'new', 'url':req['url'], 'method':req['method']}]
                elif kind == 'missing': self.inv['requests'][0]['url'] += '/private-missing'
                elif kind == 'ambiguous': self.data['log']['entries'].append(copy.deepcopy(self.data['log']['entries'][1]))
                elif kind == 'late-status': self.data['log']['entries'][5]['response']['status'] = 0
                elif kind == 'late-reference': self.data['log']['entries'][5]['response']['content']['_file'] = '/private-path'
                elif kind == 'unused-unsafe': self.members['../private-path'] = b'no read'
                elif kind == 'late-inventory': self.inv['requests'].append({'id':'private-id'})
                elif kind == 'member-count': self.members['added.bin'] = b''
                elif kind == 'retained-bytes': self.members['Payload/é.bin'] = b'abc'
                self.prepare(); self.invoke()
        self.data, self.inv, self.members, _ = fixture(); self.prepare()
        for name in ('MAX_MEMBERS','MAX_ARCHIVE_BYTES','MAX_MEMBER_BYTES','MAX_TOTAL_BYTES'):
            with self.subTest(limit=name), patch('qawrench.har.fixtures.'+name,1): self.invoke()
        self.source = self.root/'source.har'
        self.data = json.loads((ROOT/'examples/har/selection-source.har').read_bytes())
        self.prepare(); self.report = load_fixture(self.source).build_selection_plan(self.inv); self.save_plan()
        self.data['log']['entries'][5]['request']['postData'] = {'mimeType':'text/plain','_file':'/private-path'}
        self.prepare(); self.invoke()

    def test_repeated_selectors_select_each_entry_once(self):
        self.data, self.inv, self.members, _ = fixture()
        self.prepare()
        original = load_fixture(self.source).build_selection_plan(self.inv)
        # Repeated and reordered selectors still select each original entry once.
        self.inv['requests'] = list(reversed(self.inv['requests'])) + [dict(self.inv['requests'][0], id='again')]
        self.prepare()
        self.report = load_fixture(self.source).build_selection_plan(self.inv)
        self.assertEqual(self.report['selectedEntryIndexes'], [1, 3])
        self.save_plan()
        output, summary = self.invoke(self.root/'repeated.har.zip', 0)
        self.assertEqual(summary['selectedEntries'], 2)
        with zipfile.ZipFile(output) as archive:
            entries = json.loads(archive.read('capture.har'))['log']['entries']
        self.assertEqual(entries, [self.data['log']['entries'][i] for i in (1, 3)])
        # The reviewed plan for the earlier inventory order/repetition is stale.
        self.plan.write_text(json.dumps(original))
        self.invoke(self.root/'stale-repeated.har.zip')

    def test_zip_resolved_identity_and_same_count_unused_changes(self):
        self.members['unused.bin'] = b'changed unused bytes'
        self.prepare()
        # Different raw ZIP and unused bytes still satisfy the documented identity.
        with zipfile.ZipFile(self.source, 'a') as archive: archive.comment = b'new metadata'
        self.invoke(expected=0)

    def test_aliases_publication_race_and_no_external_reads(self):
        for label, source in (('har',self.source),('inventory',self.wanted),('plan',self.plan)):
            for kind in ('direct','hard','symlink'):
                target = source if kind == 'direct' else self.root/(label+'-'+kind+'.har.zip')
                if kind == 'hard': os.link(source,target)
                elif kind == 'symlink': target.symlink_to(source)
                with self.subTest(output=(label,kind)): self.invoke(target)
                if kind != 'direct': target.unlink()
            for kind in ('direct','hard','symlink'):
                alias = source if kind == 'direct' else self.root/(label+'-input-'+kind)
                if kind == 'hard': os.link(source,alias)
                elif kind == 'symlink': alias.symlink_to(source)
                if source != self.plan: self.invoke(plan=alias)
                if source == self.source: self.invoke(inventory=alias)
                if kind != 'direct': alias.unlink()
        for kind in ('dangling','existing','directory','missing-parent'):
            target = self.root/(kind+'.har.zip')
            if kind == 'dangling': target.symlink_to(self.root/'absent')
            elif kind == 'existing': target.write_bytes(b'keep')
            elif kind == 'directory': target.mkdir()
            else: target = self.root/'absent'/'out.har.zip'
            self.invoke(target)
        for method in ('os.link','os.fsync'):
            with patch('qawrench.har.core.'+method,side_effect=OSError('private-error')): self.invoke()
        real_open = Path.open
        def guard(path,*args,**kwargs):
            self.assertIn(path,(self.source,self.wanted,self.plan))
            return real_open(path,*args,**kwargs)
        # Only patch the operation, not the snapshot's independent verification reads.
        with patch.object(Path,'open',guard), patch.object(socket,'socket',side_effect=AssertionError('network')), \
                patch.object(zipfile.ZipFile,'extract',side_effect=AssertionError('extract')), \
                patch.object(zipfile.ZipFile,'extractall',side_effect=AssertionError('extractall')):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(main(['select-requests',str(self.source),'--inventory',str(self.wanted),
                                       '--plan',str(self.plan),'--out',str(self.root/'safe.har.zip')]),0,err.getvalue())
        target = self.root/'race.har.zip'; real_link = os.link
        def race(source, output):
            Path(output).write_bytes(b'other writer'); return real_link(source,output)
        with patch('qawrench.har.core.os.link',side_effect=race):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['select-requests',str(self.source),'--inventory',str(self.wanted),
                                      '--plan',str(self.plan),'--out',str(target)]),2)
        self.assertEqual(target.read_bytes(),b'other writer')
        self.assertFalse(list(self.root.glob('.*.tmp')))
