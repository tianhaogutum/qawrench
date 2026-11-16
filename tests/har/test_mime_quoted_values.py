"""Declaration-only quoted grammar: validate metadata, preserve upload bytes."""
import copy
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import qawrench.har as doctor
from qawrench.har import core
from examples.har.fixtures import RULES, har
from .test_multipart import BODY, MIME, upload
from .test_mime_declaration_limits import inventory_for, with_declaration

CONTROL_ERROR = 'multipart MIME quoted parameter contains forbidden control characters'
VALID_MIME = MIME + '; empty=""; note="é雪😀\t ; printable \\" \\\\ \\;"'


class MimeQuotedValueTests(unittest.TestCase):
    def test_quoted_controls_are_rejected_in_har_and_inventory(self):
        for codepoint in (*range(9), *range(10, 32), 127):
            error = 'malformed MIME parameters' if codepoint in (10, 13) else CONTROL_ERROR
            for location in ('postData.mimeType', 'headers'):
                data = har([upload(), with_declaration(MIME + '; note="bad' + chr(codepoint) + 'value"', location)])
                suffix = 'postData.mimeType' if location == 'postData.mimeType' else 'headers[2].value Content-Type'
                for prefix, value, validate in (('log.entries[1].request.', data, doctor.validate_har),
                                                ('inventory.requests[1].', inventory_for(data), doctor.validate_inventory)):
                    with self.subTest(codepoint=codepoint, location=location, path=prefix), self.assertRaisesRegex(
                            doctor.DoctorError, re.escape(prefix + suffix) + ': .*' + error + '.*re-record'):
                        validate(value)

    def test_late_control_blocks_all_apis_without_mutation_or_filename_reads(self):
        good = har([upload()])
        for location in ('postData.mimeType', 'headers'):
            bad = har([upload(), with_declaration(MIME + '; note="before\x00after"', location)])
            wanted = inventory_for(bad)
            before, before_inventory = copy.deepcopy(bad), copy.deepcopy(wanted)
            suffix = 'postData.mimeType' if location == 'postData.mimeType' else 'headers[2].value Content-Type'
            with patch.object(Path, 'open', side_effect=AssertionError('no filename reads')):
                for operation in (doctor.validate_har, doctor.embed_form_params, doctor.export_inventory,
                                  lambda h: doctor.build_plan(h, RULES),
                                  lambda h: doctor.rewrite_har(h, RULES),
                                  lambda h: doctor.build_preflight(h, inventory_for(good))):
                    with self.subTest(location=location, api=operation), self.assertRaisesRegex(
                            doctor.DoctorError, re.escape('log.entries[1].request.' + suffix) + '.*' + CONTROL_ERROR):
                        operation(bad)
                for operation in (doctor.validate_inventory,
                                  lambda inv: doctor.build_plan(good, RULES, inventory=inv),
                                  lambda inv: doctor.rewrite_har(good, RULES, inventory=inv),
                                  lambda inv: doctor.build_preflight(good, inv)):
                    with self.subTest(location=location, api=operation), self.assertRaisesRegex(
                            doctor.DoctorError, re.escape('inventory.requests[1].' + suffix) + '.*' + CONTROL_ERROR):
                        operation(wanted)
            self.assertEqual(bad, before)
            self.assertEqual(wanted, before_inventory)

    def test_valid_quoted_values_and_payload_bytes_remain_opaque(self):
        # File payload contains every byte, boundary-like text and nested framing.
        payload = bytes(range(256)) * 300 + b'\r\n--nested\r\nnot a header\r\n--nested--\r\n--upload:42x'
        binary = with_declaration(VALID_MIME, 'headers')
        binary['request']['postData'] = upload(BODY.replace(b'\x00\xff', payload), VALID_MIME)['request']['postData']
        empty = with_declaration(VALID_MIME, 'postData.mimeType')
        empty['request']['postData'] = upload(BODY.replace(b'\x00\xff', b''), VALID_MIME)['request']['postData']
        text = with_declaration(VALID_MIME, 'headers')
        text['request']['postData'] = {'mimeType': VALID_MIME, 'text': BODY.replace(b'\x00\xff', 'é\t'.encode()).decode()}
        data = har([binary, empty, text])
        before = copy.deepcopy(data)
        with patch.object(Path, 'open', side_effect=AssertionError('no filename reads')):
            doctor.validate_har(data)
            changed, plan = doctor.rewrite_har(data, RULES)
            self.assertTrue(plan['safeToApply'])
            self.assertEqual(doctor.embed_form_params(data), before)
            self.assertTrue(doctor.build_preflight(data, inventory_for(data))['passed'])
            self.assertEqual([r['postData'] for r in doctor.export_inventory(data)['requests']],
                             [e['request']['postData'] for e in data['log']['entries']])
            for old, new in zip(data['log']['entries'], changed['log']['entries']):
                self.assertEqual(old['request']['postData'], new['request']['postData'])
                self.assertEqual(old['request']['headers'], new['request']['headers'])
                self.assertEqual(doctor.request_key(old['request'])[2], doctor.request_key(new['request'])[2])
        self.assertEqual(data, before)
        for char in map(chr, range(32, 127)):
            self.assertEqual(core._multipart_boundary(MIME + '; p="\\' + char + '"', 'mime'), b'upload:42')

    def test_existing_grammar_scope_and_boundary_diagnostics_survive(self):
        for mime, error in ((MIME + '; p="unterminated', 'malformed MIME parameters'),
                            (MIME + '; p="bad\\é"', 'malformed MIME parameters'),
                            (MIME + '; p="bad\\\t"', 'malformed MIME parameters'),
                            (MIME + '; BOUNDARY=x', 'duplicate declarations'),
                            ('multipart/form-data; p="valid"', 'boundary parameter is required'),
                            ('multipart/form-data; boundary="bad "', 'invalid characters'),
                            ('multipart/form-data; boundary="Upload:42"', 'opening.*boundary')):
            with self.subTest(mime=mime), self.assertRaisesRegex(doctor.DoctorError, error):
                doctor.validate_har(har([upload(mime=mime)]))
        mixed = VALID_MIME.replace('multipart/form-data', 'Multipart/Form-Data').replace('boundary=', 'BoUnDaRy=')
        doctor.validate_har(har([upload(mime=mixed)]))
        # Other MIME types and response MIME declarations retain their existing scope.
        ordinary = upload(b'', 'application/octet-stream; p="\x00\x7f"')
        ordinary['response']['content']['mimeType'] = MIME + '; p="\x00\x7f"'
        doctor.validate_har(har([ordinary]))


if __name__ == '__main__':
    unittest.main()
