"""Authored synthetic fixtures, never captures of a real service."""
import base64
from copy import deepcopy
from urllib.parse import parse_qsl, urlsplit

RULES = {"origins": {"https://stage.example": "http://fixture.test"}, "dropQueryKeys": ["cacheBust"]}


def entry(url, method="GET", body=None, response_text='{"ok":true}', binary=False):
    request = {"method": method, "url": url, "httpVersion": "HTTP/1.1", "cookies": [],
               "headers": [{"name": "Accept", "value": "application/json"}],
               "queryString": [{"name": k, "value": v} for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True)],
               "headersSize": -1, "bodySize": 0, "_syntheticRequest": True}
    if body is not None:
        request["postData"] = {"mimeType": "application/json", "text": body, "params": []}
        request["bodySize"] = len(body.encode("utf-8"))
    payload = bytes([0, 255, 13, 10, 128]) if binary else response_text.encode("utf-8")
    content = {"size": len(payload), "mimeType": "application/octet-stream" if binary else "application/json",
               "text": base64.b64encode(payload).decode("ascii") if binary else response_text}
    if binary:
        content["encoding"] = "base64"
    return {"startedDateTime": "2026-01-01T00:00:00.000Z", "time": 1,
            "request": request,
            "response": {"status": 200, "statusText": "OK", "httpVersion": "HTTP/1.1", "cookies": [],
                         "headers": [{"name": "Content-Type", "value": content["mimeType"]},
                                     {"name": "X-Synthetic", "value": "true"}],
                         "content": content, "redirectURL": "", "headersSize": -1, "bodySize": len(payload)},
            "cache": {}, "timings": {"send": 0, "wait": 1, "receive": 0},
            "_syntheticExtension": {"retained": [1, None, "é"]}}


def har(entries):
    return {"log": {"version": "1.2", "creator": {"name": "har-replay-doctor synthetic fixture", "version": "1"},
                    "comment": "SYNTHETIC OFFLINE DATA; no real requests or production data", "entries": deepcopy(entries)},
            "_synthetic": True}


def example_har():
    return har([
        entry("https://stage.example/items?tag=red&cacheBust=101&tag=blue&empty=&q=a%20b"),
        entry("https://stage.example/items?cacheBust=202", "POST", '{"id":2,"name":"é"}'),
        entry("https://stage.example/asset?cacheBust=303&v=1", binary=True),
        entry("https://stage.example.evil.test/items?tag=red&tag=blue"),
    ])
