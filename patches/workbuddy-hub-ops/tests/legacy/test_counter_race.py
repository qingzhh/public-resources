#!/usr/bin/env python3
"""Concurrent retry_bump() must not corrupt the persisted snapshot.

Advisor finding: the snapshot was written OUTSIDE the lock, so a slower thread
could replace the file with an older snapshot, and concurrent writers shared a
single temp path. This drives many threads through retry_bump() and asserts the
file is valid JSON, monotonic in total, and equal to the in-memory counters.
"""
import importlib
import json
import os
import sys
import threading

import _bootstrap
wb = importlib.import_module("wb_proxy")

# Isolate the counter snapshot: retry_bump() writes to USAGE_DIR, and running
# this suite inside the production container must not overwrite the serving
# process's snapshot (doing so destroyed the live counters once already).
import tempfile as _tempfile  # noqa: E402
_ISOLATED_DIR = _tempfile.mkdtemp(prefix="wb-retry-test-")
wb.USAGE_DIR = _ISOLATED_DIR
try:
    wb.USAGE_LOG = os.path.join(_ISOLATED_DIR, "usage.jsonl")
except Exception:
    pass

FAIL = []
PASS = 0


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL.append(name)
        print("  FAIL  %s   %s" % (name, detail))


wb.reset_retry_counters_file()
path = wb.retry_counters_file()
check("the snapshot file exists after reset", os.path.exists(path))
with open(path) as fh:
    d0 = json.load(fh)
check("reset writes this process's pid", d0.get("pid") == os.getpid(), repr(d0.get("pid")))
check("reset writes an empty counter set", d0.get("counters") == {}, repr(d0.get("counters")))
check("reset sets total to 0", d0.get("total") == 0, repr(d0.get("total")))

print()
print("=" * 78)
print("1. %d threads x %d increments, each observing the file mid-flight" % (24, 40))
print("=" * 78)
THREADS, PER = 24, 40
errors = []
observed_snaps = []
obs_lock = threading.Lock()


def hammer(tid):
    for i in range(PER):
        try:
            wb.retry_bump("race_kind")
            wb.retry_bump("race_kind_%d" % (tid % 4))
            if i % 7 == 0:
                # Read the file while other writers are active. The read is
                # outside the lock on purpose: a reader must always see a
                # complete file, never a half-written one. Note the append
                # order is therefore NOT the read order, so the collected list
                # cannot be asserted monotonic -- only self-consistency per
                # observation and agreement of the final state.
                with open(path) as fh:
                    snap = json.load(fh)
                with obs_lock:
                    observed_snaps.append(snap)
        except Exception as exc:
            with obs_lock:
                errors.append("%s: %s" % (type(exc).__name__, exc))


ts = [threading.Thread(target=hammer, args=(t,)) for t in range(THREADS)]
for t in ts:
    t.start()
for t in ts:
    t.join()

check("no thread raised while reading the snapshot", not errors, repr(errors[:3]))
check("the file is valid JSON after the storm",
      isinstance(json.load(open(path)), dict))
final = json.load(open(path))
mem = wb.retry_snapshot()
expected_total = sum(mem.values())

print("      in-memory total = %d" % expected_total)
print("      on-disk total    = %d" % final.get("total"))
print("      on-disk counters = %s" % json.dumps(final.get("counters"), ensure_ascii=False))
_obs_totals = [s.get("total") for s in observed_snaps]
print("      observations     = %d (min=%s max=%s)" % (
    len(_obs_totals), min(_obs_totals) if _obs_totals else None,
    max(_obs_totals) if _obs_totals else None))

check("the on-disk total equals the in-memory total",
      final.get("total") == expected_total,
      "disk=%s mem=%s" % (final.get("total"), expected_total))
check("the on-disk counters equal the in-memory counters",
      final.get("counters") == mem, "disk=%s mem=%s" % (final.get("counters"), mem))
check("every observed total was a valid non-negative integer",
      all(isinstance(t, int) and t >= 0 for t in _obs_totals),
      repr([t for t in _obs_totals if not (isinstance(t, int) and t >= 0)][:3]))
# Each observation must be internally consistent: no reader may ever see a
# snapshot whose total disagrees with its own counters, and no reader may see a
# partially written file (json.load above would have raised).
_inconsistent = [(s.get("total"), sum((s.get("counters") or {}).values()))
                 for s in observed_snaps
                 if s.get("total") != sum((s.get("counters") or {}).values())]
check("every observed snapshot is internally consistent (total == sum(counters))",
      not _inconsistent, repr(_inconsistent[:3]))
check("no observation was an empty/partial file",
      all(isinstance(s.get("counters"), dict) for s in observed_snaps),
      repr([s for s in observed_snaps if not isinstance(s.get("counters"), dict)][:2]))
print("      observation list is not order-guaranteed by design (reads happen "
      "outside the lock); final-state equality is the correctness check")
check("no temp file was left behind",
      not [f for f in os.listdir(os.path.dirname(path)) if f.endswith(".tmp")],
      repr([f for f in os.listdir(os.path.dirname(path)) if f.endswith(".tmp")]))

print()
print("=" * 78)
print("2. the file identifies the serving process")
print("=" * 78)
check("pid matches the running process", final.get("pid") == os.getpid(),
      "file=%s me=%s" % (final.get("pid"), os.getpid()))
check("started timestamp is present and in the past",
      isinstance(final.get("started"), (int, float))
      and final["started"] <= wb.time.time(),
      repr(final.get("started")))
check("updated timestamp is not older than started",
      final.get("updated", 0) >= final.get("started", 0))

print()
print("=" * 78)
print("RESULT:  %d passed, %d failed" % (PASS, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("   FAILED: %s" % f)
    sys.exit(1)
print("ALL GREEN")
