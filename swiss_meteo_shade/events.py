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

_LEVELS = ("error", "warning")

# EVERY condition active this cycle, message -> {"message","time","last_seen",
# "count"}, in the order they were reported. `time` is when that particular
# message first appeared, not when it last recurred.
#
# Tracking a SET rather than a single latest event is the whole point. Holding
# only the newest looks fine while one thing is broken and collapses when
# several are: with four conditions reported per cycle, each one differs from
# the single stored predecessor, so all four read as new every cycle forever.
# An internet outage produces exactly that -- radar, precipitation, radiation
# and gust all fail together -- and it billed roughly forty notifications an
# hour on 2026-08-14 before this was fixed.
_active = {level: {} for level in _LEVELS}

# What was active in the PREVIOUS cycle, so a returning condition can keep its
# original `time` and a genuinely new one can be told apart.
_prev = {level: {} for level in _LEVELS}

# Conditions that became active THIS cycle. These, and only these, fire the
# `event` entities.
_new = {level: [] for level in _LEVELS}


def start_cycle():
    """Begin a fresh cycle. Anything not re-reported this cycle is resolved.

    The code is told when things go wrong and never when they come right, so
    "absent from a whole cycle" is the only resolved signal available.

    Carrying the previous cycle's records forward is what keeps `time` stable
    across a sustained fault while still giving a NEW time to one that returns
    after a clean spell -- it then reads "since <the recurrence>" instead of
    pointing at an occurrence that already ended, and fires again.
    """
    for level in _LEVELS:
        _prev[level] = _active[level]
        _active[level] = {}
        _new[level] = []




def collapse_warnings(message, detail=None):
    """Replace every active warning with a single one, and return it.

    For a cycle whose warnings are all consequences of ONE upstream fact --
    the add-on has no working internet, so radar, precipitation, gust and
    sunshine all fail together. Reporting four is technically true and
    practically useless: it says four things broke when one did, and the
    "(+3 more)" suffix leaves the reader hunting for what they missed.

    Every individual warning has ALREADY been printed to the Log by the time
    this runs, so nothing is lost -- the Log keeps the full picture and the
    sensor carries the one fact worth acting on.

    The collapsed record keeps the earliest `time` of the warnings it
    replaces, so "since" points at when the outage started rather than when it
    was recognised, and it survives across cycles like any other condition.
    """
    level = "warning"
    recs = list(_active[level].values())
    if not recs:
        return None
    message = str(message)
    now = datetime.now(timezone.utc).isoformat()
    prev = _prev[level].get(message)
    if prev is not None:                     # the same outage, still going
        rec = prev
        rec["last_seen"] = now
        rec["count"] = int(rec.get("count", 1)) + 1
        _new[level] = []
    else:
        rec = {"message": message,
               "time": min(r["time"] for r in recs),
               "last_seen": now, "count": 1}
        _new[level] = [rec]                  # one notification, not four
    _active[level] = {message: rec}
    line = f"{now} WARNING: {message}"
    if detail:
        line += f" -- {detail}"
    print(line, flush=True)
    return rec


def reset():
    """Forget everything. For tests: this module is process-global state, and
    a condition left active by one test reads as "still going" in the next,
    which silently turns a should-fire assertion into a no-fire."""
    for level in _LEVELS:
        _active[level] = {}
        _prev[level] = {}
        _new[level] = []


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
    rec = _active[level].get(message)
    if rec is not None:                      # reported twice in one cycle
        rec["last_seen"] = now
        rec["count"] = int(rec.get("count", 1)) + 1
    else:
        rec = _prev[level].get(message)
        if rec is not None:                  # still going: keep its `time`
            rec["last_seen"] = now
            rec["count"] = int(rec.get("count", 1)) + 1
            _active[level][message] = rec
        else:                                # genuinely new
            rec = {"message": message, "time": now,
                   "last_seen": now, "count": 1}
            _active[level][message] = rec
            _new[level].append(rec)
    line = f"{now} {level.upper()}: {message}"
    if detail:
        line += f" -- {detail}"
    print(line, flush=True)


def error(message, detail=None):
    _record("error", message, detail)


def warn(message, detail=None):
    _record("warning", message, detail)


def _latest(level):
    """The most recently reported ACTIVE event of this level, or None.

    Latest rather than oldest so a single sensor still tracks the newest thing
    to go wrong, which is what it did when only one event was kept.
    """
    recs = list(_active[level].values())
    return recs[-1] if recs else None


def active(level):
    """Every condition currently active at this level, in report order."""
    return list(_active[level].values())


def new_events(level):
    """Conditions that became active THIS cycle -- the ones worth announcing.

    One entry per newly-active condition, so four simultaneous failures fire
    four events ONCE, rather than the same four every cycle until they clear.
    """
    return list(_new[level])


def last_error():
    return _latest("error")


def last_warning():
    return _latest("warning")


def state_text(level):
    """"<message> since <when>", or "none" when nothing is wrong.

    With several conditions active the newest is shown and the rest are
    counted: one sensor cannot carry four messages inside Home Assistant's
    255-character state limit, and the `event` entities have already announced
    each of them individually.
    """
    ev = _latest(level)
    if not ev:
        return "none"
    try:
        stamp = datetime.fromisoformat(ev["time"]).strftime("%Y-%m-%d %H:%M") + "Z"
    except (ValueError, TypeError):
        stamp = ev["time"]
    text = f"{ev['message']} since {stamp}"
    others = len(_active[level]) - 1
    if others > 0:
        text += f" (+{others} more)"
    return text[:255]   # HA caps states at 255


def snapshot():
    """Return the ACTIVE events for inclusion in the published state.

    `last_error`/`last_warning` stay single records for backwards
    compatibility with the sensor attributes; `active_*_count` says how many
    conditions are really live so a dashboard is not misled by seeing one.
    """
    return {"last_error": _latest("error"),
            "last_warning": _latest("warning"),
            "active_error_count": len(_active["error"]),
            "active_warning_count": len(_active["warning"])}
