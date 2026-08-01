"""Tests for forecast source selection and the temperature-fetch decision.

The temperature file is another ~30 MB download, so "do we need it this cycle"
must follow the call rather than linger in module state. Before 1.2.1 it was a
module global that `gather` only overwrote when the argument was not None, so a
later call that omitted it inherited the previous cycle's answer -- invisible
without a test, which is why these exist.
"""
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade")))

import forecast      # noqa: E402

FETCHED = []


def setup_function():
    FETCHED.clear()
    forecast._find_forecast_point = lambda s, e, n: "42"
    forecast._newest_item_with = lambda s, params: {"x.fu3010h1.csv": {}}

    def fake_fetch(s, point, param, lookahead_h, assets, back_hours=0):
        FETCHED.append(param)
        return [12.0, 15.0]
    forecast._fetch_forecast_param = fake_fetch


def test_temperature_not_fetched_when_not_needed():
    forecast.from_official(2669292, 1211426, session=object(), need_temp=False)
    assert "tre200h0" not in FETCHED, (
        f"temperature downloaded despite need_temp=False: {FETCHED}")


def test_temperature_fetched_when_needed():
    forecast.from_official(2669292, 1211426, session=object(), need_temp=True)
    assert "tre200h0" in FETCHED


def test_gust_and_sunshine_always_fetched():
    forecast.from_official(2669292, 1211426, session=object())
    assert "fu3010h1" in FETCHED and "sre000h0" in FETCHED


def test_need_temp_does_not_leak_between_calls():
    """The regression: a gate-on cycle must not make later cycles fetch it."""
    forecast.from_official(2669292, 1211426, session=object(), need_temp=True)
    assert "tre200h0" in FETCHED
    FETCHED.clear()
    forecast.from_official(2669292, 1211426, session=object())   # default off
    assert "tre200h0" not in FETCHED, (
        "need_temp leaked from the previous call -- it must follow the call")


def test_default_is_off():
    """An omitted need_temp must mean 'no', never 'whatever it was'."""
    import inspect
    sig = inspect.signature(forecast.from_official)
    assert sig.parameters["need_temp"].default is False
    assert inspect.signature(forecast.gather).parameters["need_temp"].default \
        is False


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
