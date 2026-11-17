"""Authored integration example: an HTTP client's shared configuration leaks."""

import os

SETTINGS = {"endpoint": "https://example.invalid", "headers": []}


def test_monkeypatch_is_clean(monkeypatch):
    monkeypatch.setenv("SW_SERVICE_MODE", "temporary")
    assert os.environ["SW_SERVICE_MODE"] == "temporary"


def test_polluter(tmp_path):
    os.environ["SW_SERVICE_MODE"] = "debug"
    os.chdir(tmp_path)
    SETTINGS["headers"].append("X-Debug: enabled")


def test_victim():
    mode = os.environ.get("SW_SERVICE_MODE")
    assert mode is None
    assert SETTINGS["headers"] == []
