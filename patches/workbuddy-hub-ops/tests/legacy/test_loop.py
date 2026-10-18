#!/usr/bin/env python3
"""Tests for the reasoning repetition-loop breaker and honest termination.

Reproduces the user-reported failure: the thinking chain degenerates into a
short phrase rotation ("OK. Let me write. Go. Final. Writing. ...") and never
ends, so the only escape is a manual abort.
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


def run(upstream):
    reason, text, calls, events, failed, incomplete = [], [], [], [], False, None
    for fr in list(wb.stream_responses_events(iter(upstream), "deepseek-v4.1-flash", {})):
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
        if ev == "response.reasoning_summary_text.delta":
            reason.append(dj.get("delta", ""))
        elif ev == "response.output_text.delta":
            text.append(dj.get("delta", ""))
        elif ev == "response.output_item.done":
            it = dj.get("item", {})
            if it.get("type") in ("function_call", "custom_tool_call"):
                calls.append((it.get("name"), it.get("arguments")))
        elif ev == "response.failed":
            failed = True
            incomplete = ((dj.get("response") or {}).get("error") or {}).get("code")
        elif ev in ("response.completed", "response.incomplete"):
            incomplete = ((dj.get("response") or {}).get("incomplete_details") or {}).get("reason")
    return "".join(reason), "".join(text), calls, events, failed, incomplete


print("=" * 78)
print("1. THE REPORTED FAILURE: degenerate thinking rotation must be cut")
print("=" * 78)
# The exact rotation the user pasted.
rotation = ["Let me write.", "Go.", "Final.", "Writing.", "OK.", "Now."]
loop_text = "".join(w + "\n" for w in (rotation * 12))

reason, text, calls, events, failed, reason_detail = run(
    [chunk({"reasoning_content": loop_text})] + [chunk({}, "stop"), b"data: [DONE]\n"])
check("loop is detected and the stream terminates with response.failed", failed,
      "events tail=%s" % events[-4:])
check("incomplete reason is reasoning_loop_detected",
      reason_detail == "reasoning_loop_detected", repr(reason_detail))
# A single chunk is delivered whole, so the guard can only stop the NEXT one.
# The meaningful boundedness claim is for incremental delivery, asserted below.
check("single-chunk loop still terminates with a failure", failed, repr(events[-3:]))

# a rotation split across many chunks must also be caught AND cut early
frames = [chunk({"reasoning_content": w + "\n"}) for w in (rotation * 12)]
reason2, _, _, _, failed2, detail2 = run(frames + [chunk({}, "stop"), b"data: [DONE]\n"])
check("loop caught when split across chunks", failed2 and detail2 == "reasoning_loop_detected",
      repr(detail2))
check("incremental delivery is cut before the whole rotation is forwarded",
      len(reason2) < len(loop_text),
      "emitted=%d total=%d" % (len(reason2), len(loop_text)))

# single-phrase exact repetition
frames = [chunk({"reasoning_content": "I will write the file now. " * 40}),
          chunk({}, "stop"), b"data: [DONE]\n"]
_, _, _, _, failed3, detail3 = run(frames)
check("exact single-phrase repetition caught", failed3, repr(detail3))

print()
print("=" * 78)
print("2. NO FALSE POSITIVES on legitimate reasoning")
print("=" * 78)
legit = {
    "prose": ("The user is asking about the deployment. I need to check the container "
              "status first, then look at the logs to understand what happened. Let me "
              "start by listing the running containers and their health."),
    "code": ("def foo():\n    x = 1\n    return x\n\ndef bar():\n    y = 2\n    return y\n"
             "class A:\n    pass\nclass B:\n    pass\n"),
    "list": "".join("- item %d\n" % i for i in range(1, 40)),
    "numbered": "".join("%d. step\n" % i for i in range(1, 30)),
    "html": "".join("<div class=\"row\">cell %d</div>\n" % i for i in range(1, 30)),
    "short_but_varied": ("OK.\nNow let me check the file.\nGo.\nThe output shows an error.\n"
                         "Right.\nSo the fix is to patch the parser.\nDone.\n"
                         "That resolves it.\n"),
    "repeats_but_few": "yes\nyes\nyes\nyes\n",
    "low_entropy_letters": "A" * 4000,
    "low_entropy_digits": "0" * 4000,
    "dashes": "-" * 2000,
    "base64_blob": ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
                    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==" * 12),
    "repeated_markup": "<div class=\"row\">cell</div>\n" * 5,
    "repeated_tabular": "| a | b | c |\n|---|---|---|\n| 1 | 2 | 3 |\n" * 3,
}
for label, body in legit.items():
    _, _, _, _, failed, detail = run([chunk({"reasoning_content": body}),
                                      chunk({"content": "ok"}), chunk({}, "stop"),
                                      b"data: [DONE]\n"])
    check("no false positive: %s" % label, not failed, repr(detail))

print()
print("=" * 78)
print("3. detector unit behaviour")
print("=" * 78)
g = wb.ReasoningLoopGuard()
check("clean prose -> no hit", not g.feed(legit["prose"]), g.describe())
g2 = wb.ReasoningLoopGuard()
hit = g2.feed("".join(w + "\n" for w in (rotation * 12)))
check("rotation -> hit", hit, g2.describe())
check("describe() is human readable", "repeat" in g2.describe() or "churn" in g2.describe(),
      g2.describe())
g3 = wb.ReasoningLoopGuard()
check("empty feed -> no hit", not g3.feed(""))
check("tail is bounded", len(g3.tail) <= wb.REASON_LOOP_TAIL + 16)

print()
print("=" * 78)
print("4. HONEST TERMINATION: a silent upstream cut is not a clean stop")
print("=" * 78)
# upstream ends with no finish_reason and no output at all
_, _, _, _, failed, detail = run([b"data: [DONE]\n"])
check("no finish_reason + no output -> response.failed", failed, repr(detail))
check("incomplete reason names the truncation", detail == "upstream_truncated", repr(detail))

# normal stop still reports completed
_, _, _, evs, failed2, detail2 = run([chunk({"content": "hello"}), chunk({}, "stop"),
                                      b"data: [DONE]\n"])
check("normal stop -> no failure", not failed2, repr(detail2))
check("normal stop -> response.completed emitted", "response.completed" in evs, repr(evs[-3:]))

# finish_reason=length still reported as max_output_tokens
_, _, _, _, _, detail3 = run([chunk({"content": "x" * 20}), chunk({}, "length"),
                              b"data: [DONE]\n"])
check("finish_reason=length -> max_output_tokens", detail3 == "max_output_tokens", repr(detail3))

print()
print("=" * 78)
print("5. aggregate_stream (non-streaming chat) loop handling")
print("=" * 78)
agg = wb.aggregate_stream(
    iter([chunk({"reasoning_content": w + "\n"}) for w in (rotation * 12)]
         + [chunk({}, "stop"), b"data: [DONE]\n"]), "deepseek-v4.1-flash", None)
fr = ((agg.get("choices") or [{}])[0].get("finish_reason"))
check("non-streaming loop reported as length (not a clean stop)", fr == "length", repr(fr))

agg2 = wb.aggregate_stream(iter([chunk({"content": "answer"}), chunk({}, "stop"),
                                 b"data: [DONE]\n"]), "deepseek-v4.1-flash", None)
fr2 = ((agg2.get("choices") or [{}])[0].get("finish_reason"))
check("normal non-streaming still reports stop", fr2 == "stop", repr(fr2))

print()
print("=" * 78)
print("RESULT:  %d passed, %d failed" % (PASS, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("   FAILED: %s" % f)
    sys.exit(1)
print("ALL GREEN")
