"""Boundary validation diagnoses broken captures without choosing upload bytes."""
import base64
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from examples.har.fixtures import RULES, entry, har
import qawrench.har as doctor

ROOT = Path(__file__).resolve().parents[2]
MIME = 'multipart/form-data; boundary="upload:42"'
BODY = (b'--upload:42\r\nContent-Disposition: form-data; name="file"; filename="x.bin"\r\n'
        b'Content-Type: application/octet-stream\r\n\r\n\x00\xff\r\n--upload:42--\r\n')


def upload(body=BODY, mime=MIME):
    item = entry('https://stage.example/upload', 'POST', '')
    item['request']['postData'] = {'mimeType': mime, 'encoding': 'base64',
                                  'text': base64.b64encode(body).decode('ascii'),
                                  'params': [{'name': 'file', 'fileName': 'x.bin',
                                              'contentType': 'application/octet-stream'}]}
    item['request']['bodySize'] = len(body)
    return item


class MultipartTests(unittest.TestCase):
    def test_declared_boundary_must_be_unique_well_formed_and_present(self):
        cases = [('multipart/form-data', 'boundary.*required'),
                 ('multipart/form-data; boundary=Upload', 'opening.*boundary'),
                 ('multipart/form-data; boundary=""', 'boundary.*1.*70'),
                 ('multipart/form-data; boundary=' + 'a' * 71, 'boundary.*1.*70'),
                 ('multipart/form-data; boundary="bad "', 'boundary.*invalid'),
                 ('multipart/form-data; boundary="é"', 'boundary.*invalid'),
                 ('multipart/form-data; boundary="bad;value"', 'boundary.*invalid'),
                 ('multipart/form-data; boundary=a; BOUNDARY=b', 'boundary.*duplicate'),
                 ('multipart/form-data; boundary="unterminated', 'boundary.*malformed'),
                 ('multipart/form-data; boundary=upload:42', 'boundary.*malformed'),
                 ('multipart/form-data; boundary=other', 'opening.*boundary')]
        for mime, error in cases:
            source = har([upload(mime=mime)])
            saved = copy.deepcopy(source)
            with self.subTest(mime=mime), self.assertRaisesRegex(doctor.DoctorError, error):
                doctor.build_plan(source, {})
            self.assertEqual(source, saved)

    def test_framing_requires_real_crlf_delimiters_and_a_closing_boundary(self):
        for body, error in [(b'', 'opening.*boundary'),
                            (b'prefix--upload:42\r\nx\r\n--upload:42--\r\n', 'opening.*boundary'),
                            (BODY.replace(b'\r\n', b'\n'), 'opening.*boundary'),
                            (BODY.replace(b'\r\n--upload:42--\r\n', b''), 'closing.*boundary'),
                            (BODY.replace(b'--upload:42--', b'--upload:42--extra'), 'closing.*boundary'),
                            (b'--upload:42--\r\n', 'opening.*boundary')]:
            with self.subTest(body=body), self.assertRaisesRegex(doctor.DoctorError, error):
                doctor.validate_har(har([upload(body)]))

    def test_request_content_type_must_agree_and_inventory_uses_same_checks(self):
        for header, error in [('multipart/form-data; boundary=wrong', 'boundary.*disagrees'),
                              ('multipart/form-data', 'boundary.*required'),
                              ('application/octet-stream', 'multipart.*disagrees')]:
            item = upload()
            item['request']['headers'].append({'name': 'cOnTeNt-TyPe', 'value': header})
            with self.subTest(header=header), self.assertRaisesRegex(doctor.DoctorError, error):
                doctor.validate_har(har([item]))
        item = upload(mime='application/octet-stream')
        item['request']['headers'] = [{'name': 'Content-Type', 'value': MIME}]
        with self.assertRaisesRegex(doctor.DoctorError, 'multipart.*disagrees'):
            doctor.validate_har(har([item]))
        item = entry('https://stage.example/upload', 'POST')
        item['request']['headers'] = [{'name': 'Content-Type', 'value': MIME}]
        with self.assertRaisesRegex(doctor.DoctorError, 'multipart.*disagrees'):
            doctor.validate_har(har([item]))
        item = upload()
        item['request']['headers'] = [{'name': 'Content-Type', 'value': MIME},
                                      {'name': 'content-type', 'value': 'multipart/form-data; boundary=wrong'}]
        with self.assertRaisesRegex(doctor.DoctorError, 'boundary.*disagrees'):
            doctor.validate_har(har([item]))
        wanted = {'schemaVersion': 1, 'requests': [dict(id='upload', method='POST',
                  url='http://fixture.test/upload', postData=upload(mime='multipart/form-data; boundary=wrong')['request']['postData'])]}
        with self.assertRaisesRegex(doctor.DoctorError, r'inventory.requests\[0\].postData.*opening.*boundary'):
            doctor.build_preflight(har([upload()]), wanted, RULES)

    def test_params_only_files_are_identified_without_reconstruction_or_file_reads(self):
        for fields in [[{'name': 'file', 'fileName': '/unreadable/private.bin'}],
                       [{'name': 'file', 'fileName': '', 'value': ''}]]:
            source = har([upload()])
            payload = source['log']['entries'][0]['request']['postData']
            payload.pop('text')
            payload.pop('encoding')
            payload['params'] = fields
            saved = copy.deepcopy(source)
            for operation in (doctor.validate_har, doctor.embed_form_params):
                with self.subTest(fields=fields, operation=operation.__name__), self.assertRaisesRegex(
                        doctor.DoctorError, r'postData.params\[0\].fileName.*missing file content.*embedded text'):
                    operation(source)
                self.assertEqual(source, saved)
        source['log']['entries'][0]['request']['postData']['params'] = [{'name': 'a', 'value': 'b'}]
        with self.assertRaisesRegex(doctor.DoctorError, 'params-only'):
            doctor.validate_har(source)
        with self.assertRaises(doctor.DoctorError):
            doctor.embed_form_params(source)

    def test_valid_binary_text_empty_file_and_boundary_padding_preserve_every_byte(self):
        text_body = BODY.replace(b'\x00\xff', 'é'.encode())
        text = upload(text_body)
        text['request']['postData'].pop('encoding')
        text['request']['postData']['text'] = text_body.decode()
        empty_file = BODY.replace(b'\x00\xff', b'')
        padded = b'preamble\r\n' + BODY.replace(b'--upload:42\r\n', b'--upload:42 \t\r\n') + b'epilogue'
        source = har([upload(), text, upload(empty_file), upload(padded), upload(BODY[:-2])])
        source['log']['entries'][0]['request']['headers'].append({
            'name': 'Content-Type', 'value': 'MULTIPART/FORM-DATA; charset=utf-8; BOUNDARY="upload:42"'})
        saved = copy.deepcopy(source)
        result, _ = doctor.rewrite_har(source, RULES)
        for old, new in zip(source['log']['entries'], result['log']['entries']):
            self.assertEqual(doctor.request_key(old['request'])[2], doctor.request_key(new['request'])[2])
            new['request']['url'] = old['request']['url']
            self.assertEqual(new, old)
        self.assertEqual(doctor.embed_form_params(source), saved)
        self.assertEqual(source, saved)
        for boundary in ['b' * 70, "a b'()+_,./:=?-", 'a.b']:
            body = BODY.replace(b'upload:42', boundary.encode())
            doctor.validate_har(har([upload(body, 'multipart/form-data; boundary="' + boundary + '"')]))
        doctor.validate_har(har([upload(mime='multipart/form-data; boundary="upload\\:42"')]))
        # Boundary-like payload bytes, including prefix/suffix strings, are data.
        doctor.validate_har(har([upload(BODY.replace(b'\x00\xff', b'\x00\xff\r\n--upload:42-extra\r\n'))]))
        # Same content with different boundary spelling still has different exact keys.
        other = upload(BODY.replace(b'upload:42', b'new'), 'multipart/form-data; boundary=new')
        self.assertNotEqual(doctor.request_key(other['request']), doctor.request_key(upload()['request']))

    def test_cli_rejects_later_broken_upload_before_any_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, rules = root / 'bad.har', root / 'rules.json'
            source.write_text(json.dumps(har([entry('https://stage.example/a'), upload(mime='multipart/form-data; boundary=wrong')])))
            rules.write_text(json.dumps(RULES))
            saved = source.read_bytes()
            for command in ('plan', 'apply', 'embed-forms'):
                out = root / (command + '.json')
                args = [sys.executable, '-m', 'qawrench.har', command, str(source), '--out', str(out)]
                if command != 'embed-forms':
                    args.extend(['--rules', str(rules)])
                run = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=20)
                self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                self.assertIn('log.entries[1].request.postData', run.stderr)
                self.assertIn('boundary', run.stderr)
                self.assertNotIn('Traceback', run.stderr)
                self.assertFalse(out.exists())
            self.assertEqual(source.read_bytes(), saved)
            self.assertFalse(list(root.glob('.*.tmp')))


if __name__ == '__main__':
    unittest.main()
