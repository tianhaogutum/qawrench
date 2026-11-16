"""Bounded self-contained archives: exact attachment bytes and refusal paths."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from unittest.mock import patch

from examples.har.fixtures import RULES, entry, har

ROOT = Path(__file__).resolve().parents[2]


def fixture():
    data = har([entry('https://stage.example/json?cacheBust=1'),
                entry('https://stage.example/binary?cacheBust=2', binary=True),
                entry('https://stage.example/post?cacheBust=3', 'POST', 'placeholder')])
    bodies = {'json.txt': b'{"ok":true}', 'binary.bin': b'\x00\xff\r\n\x80',
              'post.bin': b'\x00\xfe\r\n\x81', 'unused.bin': b'unused\x00'}
    for e, name in zip(data['log']['entries'], ('json.txt', 'binary.bin', 'post.bin')):
        c = e['response']['content']
        c.pop('text'); c.pop('encoding', None)
        c.update(_file=name, size=len(bodies[name]))
    req = data['log']['entries'][2]['request']
    req['postData'] = {'mimeType': 'application/octet-stream', 'text': '', 'params': [], '_file': 'post.bin'}
    req['bodySize'] = len(bodies['post.bin'])
    return data, bodies


def write_zip(path, data, bodies, extra=()):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr('har.har', json.dumps(data))
            for name, body in [*bodies.items(), *extra]:
                z.writestr(name, body)


class ZipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source.har.zip'
        self.rules = self.root / 'rules.json'
        self.rules.write_text(json.dumps(RULES))
        self.data, self.bodies = fixture()
        write_zip(self.source, self.data, self.bodies)

    def cli(self, command, out, *options, expected=0, error=None):
        args = [sys.executable, '-m', 'qawrench.har', command, str(self.source), '--out', str(out), *map(str, options)]
        result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, (args, result.stdout, result.stderr))
        self.assertNotIn('Traceback', result.stderr)
        if error:
            self.assertIn(error, result.stderr)
        return result

    def test_rewrite_preserves_manifest_fields_and_all_attachment_bytes(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        out = self.root / 'rewritten.har.zip'
        self.cli('apply', out, '--rules', self.rules)
        with zipfile.ZipFile(out) as z:
            result = json.loads(z.read('har.har'))
            for name, body in self.bodies.items():
                self.assertEqual(hashlib.sha256(z.read(name)).digest(), hashlib.sha256(body).digest())
            self.assertEqual(z.namelist(), ['har.har', *self.bodies])
        expected = copy.deepcopy(self.data)
        for e in expected['log']['entries']:
            req = e['request']
            req['url'] = req['url'].replace('https://stage.example', 'http://fixture.test').partition('?')[0]
            req['queryString'] = []
        self.assertEqual(result, expected)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)

    def test_export_and_preflight_use_exact_attached_post_bytes(self):
        inv = self.root / 'inventory.json'
        self.cli('export-inventory', inv)
        inventory = json.loads(inv.read_text())
        payload = inventory['requests'][2]['postData']
        self.assertNotIn('_file', payload)
        self.assertEqual(base64.b64decode(payload['text']), self.bodies['post.bin'])
        for req in inventory['requests']:
            req['url'] = req['url'].replace('https://stage.example', 'http://fixture.test').partition('?')[0]
        inv.write_text(json.dumps(inventory))
        report = self.root / 'report.json'
        self.cli('preflight', report, '--rules', self.rules, '--inventory', inv)
        self.assertEqual(json.loads(report.read_text())['summary']['matchedRequests'], 3)
        inventory['requests'][2]['postData']['text'] = base64.b64encode(b'wrong').decode()
        inv.write_text(json.dumps(inventory))
        self.cli('apply', self.root / 'blocked.har.zip', '--rules', self.rules, '--inventory', inv,
                 expected=2, error='request preflight failed')
        self.assertFalse((self.root / 'blocked.har.zip').exists())

    def test_attached_collision_uses_body_bytes_and_response_content(self):
        from qawrench.har.fixtures import load_fixture
        from qawrench.har import build_plan
        # Two differently named request attachments with identical bytes collide.
        e = copy.deepcopy(self.data['log']['entries'][2])
        e['request']['postData']['_file'] = 'other.bin'
        e['response']['content']['_file'] = 'different.bin'
        self.data['log']['entries'].append(e)
        self.bodies.update({'other.bin': self.bodies['post.bin'], 'different.bin': b'different'})
        write_zip(self.source, self.data, self.bodies)
        plan = build_plan(load_fixture(self.source).har, RULES)
        self.assertEqual(plan['summary']['conflictingGroups'], 1)
        self.cli('apply', self.root / 'conflict.har.zip', '--rules', self.rules,
                 expected=2, error='conflicting rewritten request keys')
        self.assertFalse((self.root / 'conflict.har.zip').exists())
        # Different request bytes must remain separate keys.
        self.bodies['other.bin'] = b'other request'
        write_zip(self.source, self.data, self.bodies)
        self.assertEqual(build_plan(load_fixture(self.source).har, RULES)['summary']['collisionGroups'], 0)

    def test_invalid_archives_block_every_publication_path(self):
        cases = [
            ('missing', {'post.bin': None}, (), 'missing archive member'),
            ('two-manifests', {}, [('other.har', b'{}')], 'exactly one'),
            ('duplicate', {}, [('json.txt', b'other')], 'duplicate'),
            ('case-ambiguous', {}, [('JSON.TXT', b'other')], 'ambiguous'),
            ('traversal', {}, [('../escape', b'bad')], 'unsafe'),
            ('absolute', {}, [('/absolute', b'bad')], 'unsafe'),
            ('drive', {}, [('C:/outside', b'bad')], 'unsafe'),
            ('backslash', {}, [('a\\b', b'bad')], 'unsafe'),
            ('dot', {}, [('a/./b', b'bad')], 'unsafe'),
            ('empty-segment', {}, [('a//b', b'bad')], 'unsafe'),
            ('nested-har', {}, [('nested/another.har', b'{}')], 'exactly one'),
        ]
        for label, changes, extra, error in cases:
            bodies = {**self.bodies, **changes}
            bodies = {k: v for k, v in bodies.items() if v is not None}
            write_zip(self.source, self.data, bodies, extra)
            before = self.source.read_bytes()
            for command in ('plan', 'apply', 'preflight', 'export-inventory'):
                with self.subTest(case=label, command=command):
                    out = self.root / f'{label}-{command}.har.zip'
                    options = ['--rules', self.rules] if command in ('plan', 'apply') else []
                    if command == 'preflight':
                        options = ['--inventory', self.rules]  # archive fails before inventory parse
                    self.cli(command, out, *options, expected=2, error=error)
                    self.assertFalse(out.exists())
                    self.assertEqual(self.source.read_bytes(), before)
            self.assertEqual(list(self.root.glob('.*.tmp')), [])

    def test_payload_reference_rules_and_loose_file_refusal(self):
        for field, value, error in [('_file', '../post.bin', 'unsafe'), ('_file', 'har.har', 'manifest'),
                                     ('text', 'ambiguous', 'nonempty text'), ('encoding', 'base64', 'encoding'),
                                     ('_sha1', 'abc', '_sha1')]:
            data = copy.deepcopy(self.data)
            data['log']['entries'][2]['request']['postData'][field] = value
            write_zip(self.source, data, self.bodies)
            self.cli('apply', self.root / 'bad.har.zip', '--rules', self.rules, expected=2, error=error)
            self.assertFalse((self.root / 'bad.har.zip').exists())
        loose = self.root / 'loose.har'
        loose.write_text(json.dumps(self.data))
        self.source = loose
        self.cli('apply', self.root / 'loose-output.har', '--rules', self.rules,
                 expected=2, error='attached _file/_sha1')
        self.assertFalse((self.root / 'loose-output.har').exists())

    def test_resource_limits_nonregular_and_corrupt_archive(self):
        from qawrench.har.fixtures import load_fixture
        from qawrench.har import DoctorError
        for setting, value in [('MAX_ARCHIVE_BYTES', 10), ('MAX_MEMBERS', 2),
                               ('MAX_MEMBER_BYTES', 10), ('MAX_TOTAL_BYTES', 10)]:
            with self.subTest(setting=setting), patch('qawrench.har.fixtures.' + setting, value):
                with self.assertRaisesRegex(DoctorError, 'limit'):
                    load_fixture(self.source)
        symlink = zipfile.ZipInfo('symlink')
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        write_zip(self.source, self.data, self.bodies, [(symlink, b'json.txt')])
        self.cli('apply', self.root / 'symlink.har.zip', '--rules', self.rules, expected=2, error='regular')
        self.source.write_bytes(b'PK\x03\x04truncated')
        self.cli('plan', self.root / 'corrupt.json', '--rules', self.rules, expected=2, error='ZIP')
        self.assertEqual(list(self.root.glob('.*.tmp')), [])

    def test_stored_empty_attachments_json_crc_and_manifest_selection(self):
        from qawrench.har.fixtures import load_fixture
        from qawrench.har import DoctorError, request_key
        self.bodies['post.bin'] = b''
        self.data['log']['entries'][2]['request']['bodySize'] = 0
        with zipfile.ZipFile(self.source, 'w', compression=zipfile.ZIP_STORED) as z:
            z.writestr('har.har', json.dumps(self.data))
            for name, body in self.bodies.items():
                z.writestr(name, body)
        self.assertEqual(request_key(load_fixture(self.source).har['log']['entries'][2]['request'])[2], b'')
        self.cli('apply', self.root / 'empty.har.zip', '--rules', self.rules)
        # ZIP_STORED makes payload corruption deterministic without editing headers.
        corrupted = self.source.read_bytes().replace(b'unused\x00', b'broken\x00', 1)
        self.source.write_bytes(corrupted)
        with self.assertRaisesRegex(DoctorError, 'CRC'):
            load_fixture(self.source)
        for name, text, error in [('not-json', '{}', 'exactly one'),
                                  ('nested/only.har', '{}', 'root-level'),
                                  ('HAR.HAR', '{}', 'root-level'),
                                  ('har.har', '{"log": {}, "log": {}}', 'duplicate JSON'),
                                  ('har.har', '{bad', 'cannot read HAR ZIP')]:
            with self.subTest(name=name, text=text):
                with zipfile.ZipFile(self.source, 'w') as z:
                    z.writestr(name, text)
                with self.assertRaisesRegex(DoctorError, error):
                    load_fixture(self.source)

    def test_repeated_references_cannot_bypass_analysis_volume_limit(self):
        from qawrench.har.fixtures import load_fixture
        from qawrench.har import DoctorError
        # Three reads of the same 1 MiB attachment, one stored member. The
        # archive stays below 2 MiB; its resolved attachment volume does not.
        self.bodies['post.bin'] = b'x' * (1024 * 1024)
        self.data['log']['entries'].append(copy.deepcopy(self.data['log']['entries'][2]))
        write_zip(self.source, self.data, self.bodies)
        before = self.source.read_bytes()
        with patch('qawrench.har.fixtures.MAX_TOTAL_BYTES', 2 * 1024 * 1024):
            with self.assertRaisesRegex(DoctorError, 'cumulative referenced attachment byte limit'):
                load_fixture(self.source)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(list(self.root.glob('.*.tmp')), [])

    def test_no_clobber_output_format_and_write_failure_cleanup(self):
        from qawrench.har.fixtures import load_fixture
        from qawrench.har import DoctorError, rewrite_har
        fixture_obj = load_fixture(self.source)
        result, _ = rewrite_har(fixture_obj.har, RULES)
        original = self.source.read_bytes()
        targets = [self.source, self.root / 'existing.har.zip', self.root / 'link.har.zip', self.root / 'dangling.har.zip']
        targets[1].write_bytes(b'existing')
        os.link(self.source, targets[2])
        targets[3].symlink_to(self.root / 'absent')
        for target in targets:
            self.cli('apply', target, '--rules', self.rules, expected=2)
        self.cli('apply', self.root / 'wrong.har', '--rules', self.rules, expected=2, error='.har.zip')
        self.cli('embed-forms', self.root / 'forms.har.zip', expected=2, error='JSON-only')
        with patch('qawrench.har.core.os.link', side_effect=OSError('simulated link failure')):
            with self.assertRaisesRegex(DoctorError, 'cannot write'):
                fixture_obj.write(result, self.root / 'failure.har.zip', protected=[self.source])
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse((self.root / 'failure.har.zip').exists())
        self.assertEqual(list(self.root.glob('.*.tmp')), [])
