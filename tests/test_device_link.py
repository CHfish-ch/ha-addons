"""Tests for the MQTT device's link back to the add-on page.

The device and the add-on otherwise look like two unrelated things in the UI.
`configuration_url` joins them, but it has to survive two traps: the slug is
NOT the one in config.yaml (repository installs carry a repo-hash prefix,
local ones `local_`), and an unreachable Supervisor must not break discovery
over something purely cosmetic.
"""
import json
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade")))

import requests     # noqa: E402
import run          # noqa: E402
import shade        # noqa: E402


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, retain=False, qos=0):
        self.published.append({"topic": topic, "payload": payload})
        return self

    def wait_for_publish(self, timeout=None):
        return True


_REAL_SUPERVISOR_GET = run._supervisor_get


def setup_function():
    shade.DEVICE.pop("configuration_url", None)
    run._supervisor_get = _REAL_SUPERVISOR_GET
    run.requests = requests


def _devices_announced():
    c = FakeClient()
    shade.announce_discovery(c)
    return [json.loads(p["payload"])["device"] for p in c.published
            if p["topic"].endswith("/config")]


def test_slug_is_read_from_supervisor_not_config_yaml():
    """A repository install's slug is hash-prefixed; hardcoding would 404."""
    class R:
        ok = True
        def raise_for_status(self): pass
        def json(self): return {"data": {"slug": "a1b2c3d4_swiss_meteo_shade"}}
    run.requests = type("M", (), {"get": staticmethod(lambda *a, **k: R()),
                                  "RequestException": requests.RequestException})
    os.environ["SUPERVISOR_TOKEN"] = "x"
    try:
        assert run.addon_slug() == "a1b2c3d4_swiss_meteo_shade"
    finally:
        run.requests = requests


def test_missing_token_yields_no_slug():
    old = os.environ.pop("SUPERVISOR_TOKEN", None)
    try:
        assert run.addon_slug() is None
    finally:
        if old is not None:
            os.environ["SUPERVISOR_TOKEN"] = old


def test_supervisor_failure_is_not_fatal():
    """Cosmetic feature: a Supervisor blip must not stop the add-on."""
    def boom(*a, **k):
        raise requests.ConnectionError("supervisor down")
    run.requests = type("M", (), {"get": staticmethod(boom),
                                  "RequestException": requests.RequestException})
    os.environ["SUPERVISOR_TOKEN"] = "x"
    try:
        assert run.addon_slug() is None      # returns None, does not raise
    finally:
        run.requests = requests


def test_route_matches_the_2026_2_apps_rename():
    """1.2.2 shipped the pre-rename route and landed users on a dead page.

    The FILESYSTEM path /addons did not change in 2026.2, but the UI route
    did, which is exactly why this was easy to get wrong.
    """
    slug = "e9276670_swiss_meteo_shade"
    modern = run.addon_config_url(slug, (2026, 2))
    assert modern == f"homeassistant://config/app/{slug}/config", modern
    assert "/hassio/addon/" not in modern


def test_route_stays_old_on_pre_rename_home_assistant():
    slug = "e9276670_swiss_meteo_shade"
    legacy = run.addon_config_url(slug, (2025, 12))
    assert legacy == f"homeassistant://hassio/addon/{slug}/config", legacy


def test_unknown_version_prefers_the_modern_route():
    """Unreadable version -> assume current; the old route is EOL-only."""
    assert "/config/app/" in run.addon_config_url("s", None)


def test_core_version_parses_real_version_strings():
    for raw, want in (("2026.2.1", (2026, 2)), ("2026.12.0", (2026, 12)),
                      ("2025.7.0b3", (2025, 7)), ("", None), (None, None)):
        run._supervisor_get = lambda p, _r=raw: {"homeassistant": _r}
        assert run.core_version() == want, f"{raw!r} -> {run.core_version()}"


def test_discovery_carries_the_url_when_known():
    shade.DEVICE["configuration_url"] = \
        "homeassistant://hassio/addon/a1b2c3d4_swiss_meteo_shade/config"
    devices = _devices_announced()
    assert devices, "no entities announced"
    for dev in devices:
        assert dev.get("configuration_url", "").startswith("homeassistant://"), \
            f"device missing the internal link: {dev}"


def test_discovery_is_valid_without_the_url():
    """Every entity must still announce cleanly when the slug is unknown."""
    devices = _devices_announced()
    assert devices, "no entities announced"
    for dev in devices:
        assert "configuration_url" not in dev
        assert dev["identifiers"] == ["swiss_meteo_shade"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        setup_function()
        try:
            fn(); print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
