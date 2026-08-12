#!/usr/bin/env python3
"""Tiny in-process event recorder for Swiss Meteo Shade.

Tracks the ACTIVE error and ACTIVE warning -- what is wrong right now -- for
two text sensors in Home Assistant. A cycle that emits nothing clears them, so
a resolved fault stops looking live.

That is only safe because the `event` entities carry the history: they fire
once per new event, so Home Assistant's logbook and any notification keep a
permanent record. The sensors are therefore free to be STATE rather than a
log. Splitting it that way is why nothing is lost by clearing.

A condition that persists keeps its original `time`, so the published state
reads "<message> since <when>" and stays byte-identical between cycles -- a
ticking duration would rewrite it every cycle and flood the recorder.

Usage anywhere in the code:
    import events
    events.warn("forecast on backup source")
    events.error("gust file hash changed despite unchanged ETag")

Keep `message` stable for as long as the condition lasts; put anything that
changes between cycles in `detail` (log only), or the event will re-fire:
    events.warn("radar frames are stale", detail=f"newest is {age} min old")

Each call also prints to stdout (so the add-on Log tab still shows everything).
"""

import json
import os
from datetime import datetime, timezone

# Active event of each level: {"message", "time", "last_seen", "count"}|None.
# `time` is when the CURRENT message first appeared, not when it last recurred.
_last = {"error": None, "warning": None}

# Did this cycle emit anything at each level? A cycle that emits nothing means
# the condition has cleared, which is the only "resolved" signal available --
# the code is told when things go wrong, never when they come right.
_this_cycle = {"error": False, "warning": False}


def start_cycle():
    """Begin a fresh cycle. Anything not re-reported this cycle is resolved.

    Two separate jobs, and conflating them costs a cycle of lag:

    * `_this_cycle` is reset here and read by `snapshot`/`state_text`, so a
      cycle that reports nothing shows clear IMMEDIATELY -- not one cycle
      later.
    * `_last` is dropped only once a whole cycle has passed without the fault.
      That way a fault returning after a clean spell is a NEW occurrence with
      a NEW `time`: it reads "since <the recurrence>" rather than pointing at
      one that already ended, and the event entity fires for it again.
    """
    for level in ("error", "warning"):
        if not _this_cycle[level]:
            _last[level] = None
        _this_cycle[level] = False




def _record(level, message, detail=None):
    """Record the latest event of `level`.

    `message` is the DEDUP KEY as well as the text shown in Home Assistant, so
    it must stay identical while a condition persists. Anything that varies
    between cycles -- a frame age ticking up, an exception repr carrying an
    object address -- belongs in `detail`, which is printed to the add-on Log
    and nowhere else. Putting a moving value in `message` silently defeats the
    deduplication below: every cycle looks like a brand-new event, so the
    sensor timestamp moves and the `event` entity re-fires. During a four-hour
    MeteoSwiss radar outage that is roughly fifty notifications for one fault.

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
    _this_cycle[level] = True
    prev = _last[level]
    repeat = bool(prev and prev.get("message") == message)
    if repeat:
        prev["last_seen"] = now
        prev["count"] = int(prev.get("count", 1)) + 1
    else:
        _last[level] = {"message": message, "time": now,
                        "last_seen": now, "count": 1}
    line = f"{now} {level.upper()}: {message}"
    if detail:
        line += f" -- {detail}"
    print(line, flush=True)


def error(message, detail=None):
    _record("error", message, detail)


def warn(message, detail=None):
    _record("warning", message, detail)


def last_error():
    return _last["error"]


def last_warning():
    return _last["warning"]


def state_text(level):
    """"<message> since <when>", or "none" when nothing is wrong."""
    ev = _last[level] if _this_cycle[level] else None
    if not ev:
        return "none"
    try:
        stamp = datetime.fromisoformat(ev["time"]).strftime("%Y-%m-%d %H:%M") + "Z"
    except (ValueError, TypeError):
        stamp = ev["time"]
    return f"{ev['message']} since {stamp}"[:255]   # HA caps states at 255


def snapshot():
    """Return the two ACTIVE events for inclusion in the published state.

    Gated on this cycle so the attributes agree with the state text: a clean
    cycle reports nothing rather than leaving a resolved fault visible.
    """
    return {"last_error": _last["error"] if _this_cycle["error"] else None,
            "last_warning": (_last["warning"] if _this_cycle["warning"]
                             else None)}
