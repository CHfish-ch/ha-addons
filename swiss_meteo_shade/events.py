#!/usr/bin/env python3
"""Tiny in-process event recorder for Swiss Meteo Shade.

Records the most recent ERROR and most recent WARNING (message + UTC time) so
they can be surfaced as two "Last error" / "Last warning" text sensors in Home
Assistant. Deliberately minimal: no log levels to configure, no history beyond
the single latest of each -- the sensor name makes clear it may be stale.

Usage anywhere in the code:
    import events
    events.warn("forecast on backup source")
    events.error("gust file hash changed despite unchanged ETag")

Each call also prints to stdout (so the add-on Log tab still shows everything).
"""

import json
import os
from datetime import datetime, timezone

# most recent event of each level: {"message": str, "time": iso str} or None
_last = {"error": None, "warning": None}

# Optional persistence: if a writable path is set (the add-on points it at
# /data), the latest error/warning survive a restart so the diagnostic sensors
# are not blank after every reboot. In memory only if unset.
_persist_path = None


def set_persist_path(path):
    """Enable persistence to `path` and load any previously saved events."""
    global _persist_path
    _persist_path = path
    try:
        with open(path) as fh:
            saved = json.load(fh)
        for level in ("error", "warning"):
            if isinstance(saved.get(level), dict):
                _last[level] = saved[level]
    except (OSError, ValueError):
        pass                              # no file yet or unreadable -> ignore


def _save():
    if not _persist_path:
        return
    try:
        tmp = _persist_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(_last, fh)
        os.replace(tmp, _persist_path)    # atomic
    except OSError:
        pass


def _record(level, message):
    """Record the latest event of `level`.

    The `time` field is the moment this *particular* message FIRST appeared and
    stays put while the same message keeps recurring. That matters because the
    sensor's state is this timestamp: refreshing it every cycle during a
    sustained condition would change the state every cycle and re-fire any
    notification automation watching it -- a notification every 5 minutes for
    one ongoing problem. A repeat only updates `last_seen` and `count`, which
    ride along as attributes, so an ongoing condition stays visible without
    being noisy.
    """
    now = datetime.now(timezone.utc).isoformat()
    message = str(message)
    prev = _last[level]
    repeat = bool(prev and prev.get("message") == message)
    if repeat:
        prev["last_seen"] = now
        prev["count"] = int(prev.get("count", 1)) + 1
    else:
        _last[level] = {"message": message, "time": now,
                        "last_seen": now, "count": 1}
    print(f"{now} {level.upper()}: {message}", flush=True)
    # C4: only touch /data when the message itself changed, not on every repeat
    if not repeat:
        _save()


def error(message):
    _record("error", message)


def warn(message):
    _record("warning", message)


def last_error():
    return _last["error"]


def last_warning():
    return _last["warning"]


def snapshot():
    """Return the two latest events for inclusion in the published state."""
    return {"last_error": _last["error"], "last_warning": _last["warning"]}
