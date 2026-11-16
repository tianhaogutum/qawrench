"""Each multipart request declaration has case-insensitively unique names."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

import qawrench.har as doctor
from qawrench.har import core
from examples.har.fixtures import RULES, har
from .test_multipart import BODY, MIME, upload
from .test_mime_declaration_limits import count_mime, inventory_for, with_declaration

ERROR = ('multipart MIME parameter has duplicate declarations; '
         're-record with unique multipart MIME parameter names')


class MimeDuplicateParameterTests(unittest.TestCase):
    def test_duplicate_names_rejected_with_value_free_request_paths(self):
        for mime in (MIME + '; note=a; note=a', MIME + '; note=a; NOTE=b',
                     MIME + '; private-name="secret-one"; distinct=x; PRIVATE-NAME="secret-two"',
                     'multipart/form-data; note=""; NOTE=""; boundary="upload:42"',
                     MIME + '; x*=a; X*=b'):
            for location in ('postData.mimeType', 'headers'):
                data = har([upload(), with_declaration(mime, location)])
                suffix = 'postData.mimeType' if location == 'postData.mimeType' else 'headers[2].value Content-Type'
                for prefix, value, validate in (('log.entries[1].request.', data, doctor.validate_har),
                                                ('inventory.requests[1].', inventory_for(data), doctor.validate_inventory)):
                    before = copy.deepcopy(value)
                    with self.subTest(mime=mime, location=location, prefix=prefix):
                        with self.assertRaises(doctor.DoctorError) as raised:
                            validate(value)
                        self.assertEqual(str(raised.exception), prefix + suffix + ': ' + ERROR)
                        self.assertEqual(value, before)

    def test_late_invalid_source_or_inventory_blocks_all_apis(self):
        good = har([upload()])
        inventory = inventory_for(good)
        for location in ('postData.mimeType', 'headers'):
            bad = har([upload(), with_declaration(MIME + '; note=a; NOTE=b', location)])
            wanted = inventory_for(bad)
            before = copy.deepcopy((bad, wanted))
            with patch.object(Path, 'open', side_effect=AssertionError('no file reads')):
                for operation in (doctor.validate_har, doctor.embed_form_params, doctor.export_inventory,
                                  lambda h: doctor.build_plan(h, RULES),
                                  lambda h: doctor.rewrite_har(h, RULES),
                                  lambda h: doctor.build_preflight(h, inventory),
                                  lambda h: doctor.select_requests(h, inventory)):
                    with self.subTest(location=location, api=operation), self.assertRaisesRegex(
                            doctor.DoctorError, 'log.entries\\[1\\].request.*duplicate declarations'):
                        operation(bad)
                for operation in (doctor.validate_inventory,
                                  lambda inv: doctor.build_plan(good, RULES, inventory=inv),
                                  lambda inv: doctor.rewrite_har(good, RULES, inventory=inv),
                                  lambda inv: doctor.build_preflight(good, inv),
                                  lambda inv: doctor.select_requests(good, inv)):
                    with self.subTest(location=location, api=operation), self.assertRaisesRegex(
                            doctor.DoctorError, 'inventory.requests\\[1\\].*duplicate declarations'):
                        operation(wanted)
            self.assertEqual((bad, wanted), before)

    def test_unique_names_are_local_to_each_declaration_and_preserve_bytes(self):
        mime = MIME + '; note="é雪\t; \\" \\\\"; NOTE2=a; x*=b; x=c; empty=""'
        binary = with_declaration(mime, 'headers')
        binary['request']['postData']['mimeType'] = mime
        empty = upload(BODY.replace(b'\x00\xff', b''), mime)
        text = upload(BODY.replace(b'\x00\xff', 'é'.encode()), mime)
        text['request']['postData'].pop('encoding')
        text['request']['postData']['text'] = BODY.replace(b'\x00\xff', 'é'.encode()).decode()
        data = har([binary, empty, text])
        before = copy.deepcopy(data)
        wanted = inventory_for(data)
        doctor.validate_har(data)
        doctor.validate_inventory(wanted)
        changed, plan = doctor.rewrite_har(data, RULES)
        self.assertTrue(plan['safeToApply'])
        self.assertEqual(doctor.embed_form_params(data), before)
        self.assertTrue(doctor.build_preflight(data, wanted)['passed'])
        self.assertEqual(doctor.select_requests(data, wanted), before)
        for old, new in zip(data['log']['entries'], changed['log']['entries']):
            self.assertEqual(old['request']['postData'], new['request']['postData'])
            self.assertEqual(old['request']['headers'], new['request']['headers'])
            self.assertEqual(doctor.request_key(old['request'])[2], doctor.request_key(new['request'])[2])
        self.assertEqual(data, before)

    def test_boundary_diagnostic_limits_and_unrelated_policy_survive(self):
        for suffix in ('; BOUNDARY="upload:42"', '; BOUNDARY=other'):
            with self.assertRaises(doctor.DoctorError) as raised:
                core._multipart_boundary(MIME + suffix, 'declaration')
            self.assertEqual(str(raised.exception), 'declaration: multipart boundary has duplicate declarations')
        self.assertEqual(core._multipart_boundary(count_mime(128), 'declaration'), b'upload:42')
        with self.assertRaisesRegex(doctor.DoctorError, '128-parameter limit'):
            core._multipart_boundary(count_mime(128) + '; P0=x', 'declaration')
        # Responses and non-multipart request MIME declarations keep their scope.
        item = upload(b'', 'application/octet-stream; note=a; NOTE=b')
        item['request']['headers'] = [{'name': 'Content-Type', 'value': item['request']['postData']['mimeType']}]
        item['response']['content']['mimeType'] = MIME + '; note=a; NOTE=b'
        item['response']['headers'] = [{'name': 'Content-Type', 'value': MIME + '; note=a; NOTE=b'}]
        doctor.validate_har(har([item]))


if __name__ == '__main__':
    unittest.main()
