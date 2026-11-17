"""Bounded UTF-8 file input, YAML/JSON parsing and inline JSON payloads."""
import os
import sys

import pytest

from qawrench.msw_contract import CheckError, normalize_cases, read_data


def test_file_input_rejects_directories_invalid_utf8_oversized_files_and_duplicate_keys(tmp_path):
    with pytest.raises(CheckError, match="regular file"):
        read_data(tmp_path)
    path = tmp_path / "cases.yaml"
    path.write_bytes(bytes([255, 254, 0, 0]))
    with pytest.raises(CheckError, match="UTF-8"):
        read_data(path)
    path.write_text("a" * 101)
    with pytest.raises(CheckError, match="exceeds 100 bytes"):
        read_data(path, 100)
    path.write_text("value: 1\nvalue: 2\n")
    with pytest.raises(CheckError, match="(?i)unique|duplicate"):
        read_data(path)
    path.write_text('{"value": 1, "value": 2}')
    with pytest.raises(CheckError, match="(?i)unique|duplicate"):
        read_data(path)
    path.write_text("\ufeff- id: good\n  url: https://example.test/pets\n", encoding="utf-8")
    assert read_data(path)[0]["id"] == "good"
    path.write_text("a: &a [x, x, x, x, x, x, x, x, x, x]\nb: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
                    "c: [*b, *b, *b, *b, *b, *b, *b, *b, *b, *b]\n")
    with pytest.raises(CheckError, match="(?i)alias|resource|excessive"):
        read_data(path)


def test_yaml_uses_the_core_schema_and_string_keys(tmp_path):
    path = tmp_path / "data.yaml"
    path.write_text("200: ok\non: yes\nday: 2024-01-01\noctal: 0o17\nhex: 0x1F\nfloat: 1.5\nnull_value: ~\n"
                    "plain: 010\nflag: true\n")
    assert read_data(path) == {"200": "ok", "on": "yes", "day": "2024-01-01", "octal": 15, "hex": 31,
                               "float": 1.5, "null_value": None, "plain": 10, "flag": True}


@pytest.mark.skipif(sys.platform == "win32" or not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
def test_fifo_input_is_rejected_without_opening_or_waiting_for_a_writer(tmp_path):
    path = tmp_path / "cases.pipe"
    os.mkfifo(path)
    with pytest.raises(CheckError, match="regular file"):
        read_data(path)


def test_inline_json_rejects_unserializable_and_circular_payloads_before_importing_handlers():
    with pytest.raises(CheckError, match="serializable JSON"):
        normalize_cases([{"url": "https://example.test", "method": "POST", "json": object()}])
    circular = {}
    circular["self"] = circular
    with pytest.raises(CheckError, match="serializable JSON"):
        normalize_cases([{"url": "https://example.test", "method": "POST", "json": circular}])


def test_inline_json_uses_javascript_number_formatting():
    [case] = normalize_cases([{"url": "https://example.test", "method": "PUT",
                               "json": {"a": 1.0, "b": 1e-7, "c": 1e21, "d": "\u2028", "e": float("nan")}}])
    assert case["body"] == '{"a":1,"b":1e-7,"c":1e+21,"d":"\u2028","e":null}'
