#!/usr/bin/env python3
"""rev 5 tests: truncated DSML envelope must not truncate the visible answer.

Reproduces the exact failure observed in the authoring agent's own live
session: the assistant message stopped mid-sentence while the turn reported
stopReason=stop, because an incomplete DSML envelope was held in the content
buffer and then emitted verbatim as assistant text.
"""
import importlib
import json
import re
import sys

import _bootstrap
wb = importlib.import_module("wb_proxy")

FW = chr(0xFF5C) * 2
BAR = chr(0xFF5C)
LT, GT, SLASH = chr(60), chr(62), chr(47)
WORD = "DSML"
TAG = FW + WORD + FW
TAG_RE = re.compile(LT + "[^" + GT + "]*" + WORD + "[^" + GT + "]*")

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


def chunk(delta, finish=None):
    return ("data: " + json.dumps(
        {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n").encode()


def parse(frames):
    text, calls, events, failed = [], [], [], False
    for fr in frames:
        txt = fr.decode("utf-8", "replace") if isinstance(fr, bytes) else str(fr)
        ev = dj = None
        for line in txt.split("\n"):
            if line.startswith("event: "):
                ev = line[7:].strip()
            elif line.startswith("data: "):
                try:
                    dj = json.loads(line[6:])
                except Exception:
                    pass
        if not dj:
            continue
        events.append(ev)
        if ev == "response.output_text.delta":
            text.append(dj.get("delta", ""))
        elif ev == "response.output_item.done":
            it = dj.get("item", {})
            if it.get("type") in ("function_call", "custom_tool_call"):
                calls.append((it.get("name"), it.get("arguments")))
        elif ev == "response.failed":
            failed = True
    return "".join(text), calls, events, failed


def run(upstream):
    return parse(list(wb.stream_responses_events(iter(upstream), "deepseek-v4.1-flash", {})))


# The exact shape from the session: a sentence, then the envelope opening, then
# the turn ends with no tool call and finish_reason=stop.
BROKEN_TAIL = (
    "Now I see it. parse_dsml_tool_calls requires the complete envelope "
    + LT + TAG + " calls" + GT
)
BROKEN_TAIL2 = (
    "The real cause is that the marker "
    + LT + TAG + " invoke name=\"bash\"" + GT + "\n"
    + LT + TAG + " parameter name=\"command\"" + GT
)

print("=" * 78)
print("1. THE REPORTED FAILURE: truncated envelope must not cut the answer")
print("=" * 78)
for label, tail in (("opening 'calls>'", BROKEN_TAIL), ("opening 'invoke'+'parameter'", BROKEN_TAIL2)):
    t, calls, evs, failed = run([
        chunk({"content": tail}),
        chunk({}, "stop"), b"data: [DONE]\n",
    ])
    check("[%s] visible text keeps the real sentence" % label,
          "Now I see it." in t or "The real cause" in t, repr(t))
    check("[%s] no DSML marker reaches the client" % label,
          BAR not in t and not TAG_RE.search(t), repr(t))
    check("[%s] no bogus tool call" % label, len(calls) == 0, repr(calls))

print()
print("=" * 78)
print("2. control: the SAME prose without a marker is untouched")
print("=" * 78)
plain = "Now I see it. parse_dsml_tool_calls requires the complete envelope"
t, calls, evs, failed = run([chunk({"content": plain}), chunk({}, "stop"), b"data: [DONE]\n"])
check("plain sentence emitted verbatim", t == plain, repr(t))

plain2 = "the condition a < b holds and <div> is markup"
t2, _, _, _ = run([chunk({"content": plain2}), chunk({}, "stop"), b"data: [DONE]\n"])
check("ordinary '<' text emitted verbatim", t2 == plain2, repr(t2))

print()
print("=" * 78)
print("3. a COMPLETE envelope still becomes a tool call (no regression)")
print("=" * 78)
full = (LT + TAG + " calls" + GT + "\n" + LT + TAG + " invoke name=\"bash\"" + GT + "\n"
        + LT + TAG + " parameter name=\"command\" string=\"true\"" + GT
        + "echo hi" + LT + SLASH + TAG + "parameter" + GT + "\n"
        + LT + SLASH + TAG + "invoke" + GT + "\n" + LT + SLASH + TAG + "calls" + GT)
t, calls, evs, failed = run([
    chunk({"content": "Working on it. " + full}),
    chunk({}, "stop"), b"data: [DONE]\n",
])
check("complete envelope -> one tool call", len(calls) == 1 and calls[0][0] == "bash", repr(calls))
check("complete envelope -> no marker in text", not TAG_RE.search(t), repr(t))
check("complete envelope -> prose kept", "Working on it." in t, repr(t))

print()
print("=" * 78)
print("4. quarantine helper unit behaviour")
print("=" * 78)
keep, dropped = wb.quarantine_dsml_text(BROKEN_TAIL)
check("helper keeps the sentence", keep.startswith("Now I see it."), repr(keep))
check("helper drops from the marker", dropped.startswith(LT + FW), repr(dropped[:40]))
keep2, dropped2 = wb.quarantine_dsml_text(plain)
check("helper leaves clean text whole", keep2 == plain and dropped2 == "", repr(keep2))
keep3, dropped3 = wb.quarantine_dsml_text("just a < b comparison")
check("helper leaves '<' prose whole", keep3 == "just a < b comparison" and dropped3 == "")

print()
print("=" * 78)
print("5. non-streaming + chat paths")
print("=" * 78)
obj = {"choices": [{"finish_reason": "stop",
                    "message": {"content": BROKEN_TAIL, "reasoning_content": ""}}]}
res = wb.chat_to_response(obj, "deepseek-v4.1-flash")
mt = "".join(p.get("text", "") for it in res["output"] if it["type"] == "message"
             for p in it.get("content", []))
check("non-streaming keeps sentence", "Now I see it." in mt, repr(mt))
check("non-streaming drops marker", not TAG_RE.search(mt), repr(mt))

agg = wb.aggregate_stream(iter([chunk({"content": BROKEN_TAIL}), chunk({}, "stop"),
                                b"data: [DONE]\n"]), "deepseek-v4.1-flash", None)
ac = ((agg.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
check("chat aggregate keeps sentence", "Now I see it." in ac, repr(ac))
check("chat aggregate drops marker", not TAG_RE.search(ac), repr(ac))

print()
print("=" * 78)
print("RESULT:  %d passed, %d failed" % (PASS, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("   FAILED: %s" % f)
    sys.exit(1)
print("ALL GREEN")