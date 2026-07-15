import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


display = load("frame_display", ROOT / "frame" / "display.py")
shoot = load("frame_shoot", ROOT / "frame" / "shoot.py")


class DummyResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit):
        return self.body


def test_fetch_recent_requests_calendar_today(monkeypatch):
    requested = []

    def fake_urlopen(req, timeout):
        requested.append((req.full_url, timeout))
        return DummyResponse(json.dumps({"species": [{"sci": "Turdus migratorius"}]}).encode())

    monkeypatch.setattr(display.urllib.request, "urlopen", fake_urlopen)
    species = display.fetch_recent(
        "http://birdnet.local", 24, 12, calendar_today=True
    )
    assert species == [{"sci": "Turdus migratorius"}]
    assert requested == [(
        "http://birdnet.local/avian/api/birdnet-api.php?action=recent&hours=24&calendar=today",
        12,
    )]


def test_fetch_recent_retains_rolling_compatibility(monkeypatch):
    requested = []

    def fake_urlopen(req, timeout):
        requested.append(req.full_url)
        return DummyResponse(b'{"species":[]}')

    monkeypatch.setattr(display.urllib.request, "urlopen", fake_urlopen)
    assert display.fetch_recent("http://birdnet.local", 12, 5) == []
    assert requested == [
        "http://birdnet.local/avian/api/birdnet-api.php?action=recent&hours=12"
    ]
    assert display.DEFAULTS["calendar_today"] is False


def test_screenshot_rewriter_applies_hours_and_calendar_mode():
    url = "http://birdnet.local/avian/api/birdnet-api.php?action=recent&hours=24"
    got = shoot._windowed_recent_url(url, 12, True)
    assert got.endswith("action=recent&hours=12&calendar=today")
    # Intercepting the same URL twice must not duplicate the calendar marker.
    assert shoot._windowed_recent_url(got, 12, True).count("calendar=today") == 1


def test_screenshot_rewriter_leaves_rolling_mode_alone():
    url = "http://birdnet.local/avian/api/birdnet-api.php?action=recent&hours=24"
    assert shoot._windowed_recent_url(url, 24, False) == url
