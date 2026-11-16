"""Original synthetic LHR projections, never browser measurements.

Field names and units follow the Lighthouse 12.8.2 LHR TypeScript definitions.
Unrelated rendering/category data is intentionally absent. Numeric values and
URLs are synthetic; this is not a complete report for Lighthouse's HTML viewer.
"""

from copy import deepcopy
import json
from pathlib import Path

AUDIT = "largest-contentful-paint"
POLICY = {
    "schema_version": 1,
    "min_samples": 5,
    "noise_multiplier": 2.0,
    "metrics": {AUDIT: {"direction": "lower", "unit": "millisecond",
                         "absolute_tolerance": 100.0, "relative_tolerance": 0.05}},
}


def lhr(value: float | None, url: str = "https://synthetic.example.test/") -> dict:
    return {
        "_fixture": "SYNTHETIC OFFLINE LHR PROJECTION; no page was measured",
        "lighthouseVersion": "12.8.2",
        "gatherMode": "navigation",
        "requestedUrl": url,
        "mainDocumentUrl": url,
        "finalUrl": url,
        "finalDisplayedUrl": url,
        "fetchTime": "2026-01-01T00:00:00.000Z",
        "userAgent": "SyntheticChrome/136.0.0.0",
        "environment": {"hostUserAgent": "SyntheticChrome/136.0.0.0",
                        "networkUserAgent": "SyntheticMobile/136.0.0.0", "benchmarkIndex": 1000},
        "runWarnings": [],
        "configSettings": {
            "output": ["json"], "formFactor": "mobile", "throttlingMethod": "simulate",
            "throttling": {"rttMs": 150, "throughputKbps": 1638.4, "requestLatencyMs": 562.5,
                           "downloadThroughputKbps": 1474.56, "uploadThroughputKbps": 675,
                           "cpuSlowdownMultiplier": 4},
            "screenEmulation": {"mobile": True, "width": 412, "height": 823,
                                "deviceScaleFactor": 1.75, "disabled": False},
            "emulatedUserAgent": "SyntheticMobile/136.0.0.0", "disableStorageReset": False,
            "locale": "en-US", "onlyAudits": [AUDIT],
        },
        "audits": {AUDIT: {"id": AUDIT, "title": "Largest Contentful Paint",
                            "description": "Synthetic LCP fixture", "score": 0.5,
                            "scoreDisplayMode": "numeric", "numericValue": value,
                            "numericUnit": "millisecond"}},
    }


def write_runs(directory: Path, values: list[float], url: str = "https://synthetic.example.test/") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for index, value in enumerate(values, 1):
        (directory / f"run-{index:03d}.json").write_text(
            json.dumps(lhr(value, url), indent=2, allow_nan=False) + "\n", encoding="utf-8")


def policy() -> dict:
    return deepcopy(POLICY)
