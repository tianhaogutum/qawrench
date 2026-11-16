"""Bound multipart metadata independently of authoritative upload bytes."""
import copy
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import qawrench.har as doctor
from qawrench.har import core
from examples.har.fixtures import RULES, har
from .test_multipart import BODY, MIME, upload

BYTE_ERROR = 'multipart MIME declaration exceeds 65536-byte UTF-8 limit'
COUNT_ERROR = 'multipart MIME parameter count exceeds 128-parameter limit'


def byte_mime(size, *, unicode=False):
    prefix = MIME + '; note="'
    count = size - len((prefix + '"').encode())
    value = ('é' * (count // 2) + 'a' * (count % 2)) if unicode else 'a' * count
    return prefix + value + '"'


def count_mime(count):
    return MIME + ''.join('; p%d="a;b\\\"c"' % i for i in range(count - 1))


def with_declaration(mime, location):
    item = upload()
    if location == 'postData.mimeType':
        item['request']['postData']['mimeType'] = mime
    else:
        item['request']['headers'] = [
            {'name': 'X-Other', 'value': 'untouched'},
            {'name': 'Content-Type', 'value': MIME},
            {'name': 'cOnTeNt-TyPe', 'value': mime}]
    return item


def inventory_for(data):
    return {'schemaVersion': 1, 'requests': [
        {'id': str(i), **{k: copy.deepcopy(v) for k, v in e['request'].items()
                         if k in ('url', 'method', 'headers', 'postData', 'bodySize')}}
        for i, e in enumerate(data['log']['entries'])]}


class MimeDeclarationLimitTests(unittest.TestCase):
    def test_over_limit_declarations_are_rejected(self):
        for mime, error in ((byte_mime(65537), BYTE_ERROR),
                            (byte_mime(65537, unicode=True), BYTE_ERROR),
                            (count_mime(129), COUNT_ERROR)):
            for location in ('postData.mimeType', 'headers'):
                data = har([upload(), with_declaration(mime, location)])
                with self.subTest(error=error, location=location), self.assertRaisesRegex(
                        doctor.DoctorError, re.escape('log.entries[1].request.') + '.*'
                        + re.escape(error) + '.*re-record'):
                    doctor.validate_har(data)

    def test_exact_limits_preserve_declarations_and_binary_empty_text_bodies(self):
        for mime in (byte_mime(65535), byte_mime(65536), byte_mime(65536, unicode=True),
                     count_mime(127), count_mime(128)):
            binary = with_declaration(mime, 'postData.mimeType')
            empty = with_declaration(mime, 'headers')
            empty['request']['postData'] = upload(BODY.replace(b'\x00\xff', b''))['request']['postData']
            text = with_declaration(mime, 'postData.mimeType')
            text['request']['postData'].pop('encoding')
            text['request']['postData']['text'] = BODY.replace(b'\x00\xff', 'é'.encode()).decode()
            # Body is larger than the metadata cap; only declarations are bounded.
            large_body = BODY.replace(b'\x00\xff', b'\x00\xff' * 40000)
            large = upload(large_body, mime)
            data = har([binary, empty, text, large])
            saved = copy.deepcopy(data)
            with self.subTest(bytes=len(mime.encode()), parameters=mime.count('; p')), patch.object(
                    Path, 'open', side_effect=AssertionError('no filename access')):
                doctor.validate_har(data)
                changed, plan = doctor.rewrite_har(data, RULES)
                self.assertTrue(plan['safeToApply'])
                for old, new in zip(data['log']['entries'], changed['log']['entries']):
                    self.assertEqual(new['request']['postData'], old['request']['postData'])
                    self.assertEqual(new['request']['headers'], old['request']['headers'])
                    self.assertEqual(doctor.request_key(new['request'])[2], doctor.request_key(old['request'])[2])
                self.assertEqual(doctor.embed_form_params(data), saved)
                self.assertTrue(doctor.build_preflight(data, inventory_for(data))['passed'])
                exported = doctor.export_inventory(data)
                self.assertEqual([r['postData'] for r in exported['requests']],
                                 [e['request']['postData'] for e in data['log']['entries']])
                self.assertEqual(data, saved)

    def test_late_violations_block_all_apis_without_mutating_inputs(self):
        good = har([upload()])
        inventory = inventory_for(good)
        for mime, error in ((byte_mime(65537, unicode=True), BYTE_ERROR), (count_mime(129), COUNT_ERROR)):
            for location in ('postData.mimeType', 'headers'):
                bad = har([upload(), with_declaration(mime, location)])
                wanted = inventory_for(bad)
                before, before_inventory = copy.deepcopy(bad), copy.deepcopy(wanted)
                suffix = 'postData.mimeType' if location == 'postData.mimeType' else 'headers[2].value Content-Type'
                for operation in (doctor.validate_har, doctor.embed_form_params, doctor.export_inventory,
                                  lambda h: doctor.build_plan(h, RULES),
                                  lambda h: doctor.rewrite_har(h, RULES),
                                  lambda h: doctor.build_preflight(h, inventory)):
                    with self.subTest(location=location, error=error, api=operation), self.assertRaisesRegex(
                            doctor.DoctorError, re.escape('log.entries[1].request.' + suffix + ': ' + error) + '.*re-record'):
                        operation(bad)
                for operation in (doctor.validate_inventory,
                                  lambda inv: doctor.build_plan(good, RULES, inventory=inv),
                                  lambda inv: doctor.rewrite_har(good, RULES, inventory=inv),
                                  lambda inv: doctor.build_preflight(good, inv)):
                    with self.subTest(inventory=True, location=location, api=operation), self.assertRaisesRegex(
                            doctor.DoctorError, re.escape('inventory.requests[1].' + suffix + ': ' + error) + '.*re-record'):
                        operation(wanted)
                self.assertEqual(bad, before)
                self.assertEqual(wanted, before_inventory)

    def test_bytes_checked_before_parameter_regex_and_count_stops_at_128(self):
        for mime in (byte_mime(65537), byte_mime(65537, unicode=True), MIME + '; x="' + 'a' * 65536):
            with patch.object(core, '_MIME_PARAMETER') as parser:
                with self.assertRaisesRegex(doctor.DoctorError, BYTE_ERROR):
                    core._multipart_boundary(mime, 'declaration')
                parser.match.assert_not_called()
        for tail in ('; boundary=other', '; malformed', '; p128=x'):
            with patch.object(core, '_MIME_PARAMETER', wraps=core._MIME_PARAMETER) as parser:
                with self.assertRaisesRegex(doctor.DoctorError, COUNT_ERROR):
                    core._multipart_boundary(count_mime(128) + tail, 'declaration')
                self.assertEqual(parser.match.call_count, 128)

    def test_existing_grammar_and_duplicate_boundary_diagnostics_survive(self):
        for mime, error in ((MIME + '; BOUNDARY=x', 'duplicate declarations'),
                            (MIME + '; bad', 'malformed MIME parameters'),
                            ('multipart/form-data; x=y', 'boundary parameter is required')):
            with self.subTest(mime=mime), self.assertRaisesRegex(doctor.DoctorError, error):
                doctor.validate_har(har([upload(mime=mime)]))
        self.assertEqual(core._multipart_boundary(count_mime(128), 'declaration'), b'upload:42')
        # Unrelated MIME and response declarations acquire no new size restriction.
        ordinary = upload(b'', 'application/octet-stream; note=' + 'a' * 65536)
        ordinary['response']['content']['mimeType'] = byte_mime(65537)
        doctor.validate_har(har([ordinary]))


if __name__ == '__main__':
    unittest.main()
