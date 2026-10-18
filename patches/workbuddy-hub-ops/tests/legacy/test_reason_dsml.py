#!/usr/bin/env python3
"""Acceptance tests for reasoning-channel DSML suppression (rev 3).

Adds over advisor findings:
  * 8192-byte over-limit path (unclosed envelope longer than the cap)
  * strong invariant on EVERY emitted reasoning byte
  * chat streaming scrub helper
  * arbitrary chunk-boundary sweep for the over-limit path

NOTE: every marker in this file is assembled from chr() codes on purpose.
Writing the literal sequence has repeatedly truncated the surrounding
tool-call payload in the authoring agent, which is itself part of the failure
being studied.

Runs inside the HUB container:
  docker exec -w /app workbuddy2api-hub python3 /opt/wb-tests/test_reason_dsml.py
"""
import importlib
import json
import re
import sys

import _bootstrap
wb = importlib.import_module("wb_proxy")

FW = chr(0xFF5C) * 2
PLAIN = "||"
BAR = chr(0xFF5C)
LT, GT, SLASH = chr(60), chr(62), chr(47)
WORD = "DSML"

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


def tag(bar):
    return bar + WORD + bar


def dsml(cmd="echo HELLO", bar=FW, close=True):
    t = tag(bar)
    s = (LT + t + " calls" + GT + "\n"
         + LT + t + " invoke name=\"bash\"" + GT + "\n"
         + LT + t + " parameter name=\"command\" string=\"true\"" + GT
         + cmd + LT + SLASH + t + "parameter" + GT + "\n"
         + LT + SLASH + t + "invoke" + GT + "\n")
    if close:
        s += LT + SLASH + t + "calls" + GT
    return s


def chunk(delta, finish=None):
    return ("data: " + json.dumps(
        {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n").encode()


def parse(frames):
    reason, content, calls = [], [], []
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
        if ev == "response.reasoning_summary_text.delta":
            reason.append(dj.get("delta", ""))
        elif ev == "response.output_text.delta":
            content.append(dj.get("delta", ""))
        elif ev == "response.output_item.done":
            it = dj.get("item", {})
            if it.get("type") in ("function_call", "custom_tool_call"):
                calls.append((it.get("name"), it.get("arguments")))
    return "".join(reason), "".join(content), calls


def run_stream(upstream):
    return parse(list(wb.stream_responses_events(iter(upstream), "deepseek-v4.1-flash", {})))


TAG_START_RE = re.compile(chr(60) + "[^" + chr(62) + "]*" + WORD + "[^" + chr(62) + "]*")


def assert_no_marker(label, text):
    """Strong invariant (advisor wording): no emitted reasoning byte may carry
    a full DSML MARKER or the fullwidth bar U+FF5C.

    The bare word DSML in ordinary prose is allowed -- deleting it would mangle
    legitimate text, and a word alone is not a marker.
    """
    ok = (BAR not in text) and not TAG_START_RE.search(text)
    check(label, ok, repr(text[:160]) + " ... " + repr(text[-140:]))
    return ok


D = dsml()

print("=" * 78)
print("1. core matrix: DSML in reasoning must be stripped, never a tool call")
print("=" * 78)
for bar, label in ((FW, "fullwidth"), (PLAIN, "ascii")):
    r, c, calls = run_stream([
        chunk({"reasoning_content": "Let me check.\n\n"}),
        chunk({"reasoning_content": dsml(bar=bar)}),
        chunk({"content": "done"}), chunk({}, "stop"), b"data: [DONE]\n"])
    assert_no_marker("reasoning carries no marker [%s]" % label, r)
    check("no reasoning-derived function_call [%s]" % label, len(calls) == 0, repr(calls))
    check("normal reasoning text kept [%s]" % label, "Let me check." in r, repr(r[:80]))

r, c, calls = run_stream([chunk({"content": "text only"}), chunk({}, "stop"), b"data: [DONE]\n"])
check("content channel untouched", c == "text only", repr(c))

print()
print("=" * 78)
print("2. per-character streaming + every binary split point")
print("=" * 78)
payload = "Before. " + D + " After."
r, c, calls = run_stream([chunk({"reasoning_content": ch}) for ch in payload]
                         + [chunk({"content": "ok"}), chunk({}, "stop"), b"data: [DONE]\n"])
assert_no_marker("per-character: no marker", r)
check("per-character: no function_call", len(calls) == 0)
check("per-character: surrounding text kept", "Before." in r and "After." in r, repr(r))

bad = []
for i in range(1, len(D)):
    r, _, calls = run_stream([chunk({"reasoning_content": D[:i]}),
                              chunk({"reasoning_content": D[i:]}),
                              chunk({"content": "x"}), chunk({}, "stop"), b"data: [DONE]\n"])
    if BAR in r or TAG_START_RE.search(r) or calls:
        bad.append(i)
check("all %d split points clean" % (len(D) - 1), not bad, "leaking=%s" % bad[:10])

print()
print("=" * 78)
print("3. malformed / unclosed / quoted / nested / truncated")
print("=" * 78)
cases = {
    "unclosed envelope": LT + tag(FW) + " calls" + GT + "\n" + LT + tag(FW) + " invoke name=\"bash\"" + GT,
    "lone invoke": LT + tag(FW) + " invoke name=\"bash\"" + GT,
    "lone parameter": LT + tag(FW) + " parameter name=\"x\"" + GT + "y" + LT + SLASH + tag(FW) + "parameter" + GT,
    "half marker at tail": "thinking... " + LT + FW + "DSM",
    "nested envelope": D + "\n" + D,
    "braces inside": dsml(cmd='{"a": {"b": [1,2]}}'),
    "escaped quotes": dsml(cmd='echo \\"hi\\"'),
    "bare word": "the " + WORD + " format is broken",
}
for label, pl in cases.items():
    r, _, calls = run_stream([chunk({"reasoning_content": pl}),
                              chunk({"content": "z"}), chunk({}, "stop"), b"data: [DONE]\n"])
    if label == "bare word":
        check("bare word -> no marker", BAR not in r and not TAG_START_RE.search(r), repr(r))
    else:
        assert_no_marker("%s -> no marker" % label, r)
    check("%s -> no function_call" % label, len(calls) == 0, repr(calls))

r, _, calls = run_stream([chunk({"reasoning_content": "the " + WORD + " format is broken"}),
                          chunk({"content": "z"}), chunk({}, "stop"), b"data: [DONE]\n"])
check("prose mentioning the word is preserved", WORD + " format is broken" in r, repr(r))

print()
print("=" * 78)
print("4. ordinary '<' text / HTML / markdown / comparisons must survive")
print("=" * 78)
ordinary = ["a < b and c > d", "<div class=\"x\">hello</div>", "5 < 10 && 3 > 1",
            "Use `<T>` for generics", "```html\n<p>hi</p>\n```", "x<y",
            "if (a<b) { }", "list: <item1>, <item2>"]
for t in ordinary:
    r, _, _ = run_stream([chunk({"reasoning_content": t}),
                          chunk({"content": "z"}), chunk({}, "stop"), b"data: [DONE]\n"])
    check("kept: %r" % t[:32], r == t, "got %r" % r)

print()
print("=" * 78)
print("5. legitimate structured tool_call passes through (regression)")
print("=" * 78)
r, c, calls = run_stream([
    chunk({"reasoning_content": "I will run it.\n"}),
    chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                           "function": {"name": "bash", "arguments": ""}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"command": "echo hi"}'}}]}),
    chunk({}, "tool_calls"), b"data: [DONE]\n"])
check("structured call preserved", calls == [("bash", '{"command": "echo hi"}')], repr(calls))

r, c, calls = run_stream([
    chunk({"reasoning_content": D}),
    chunk({"tool_calls": [{"index": 0, "id": "call_2", "type": "function",
                           "function": {"name": "bash", "arguments": '{"command": "echo real"}'}}]}),
    chunk({}, "tool_calls"), b"data: [DONE]\n"])
check("structured call kept when reasoning had DSML",
      calls == [("bash", '{"command": "echo real"}')], repr(calls))
check("no extra call injected", len(calls) == 1, repr(calls))

print()
print("=" * 78)
print("6. content-channel recovery still works (documented exception)")
print("=" * 78)
r, c, calls = run_stream([chunk({"content": "Here: " + D}),
                          chunk({}, "stop"), b"data: [DONE]\n"])
check("content channel still recovers a call", len(calls) == 1 and calls[0][0] == "bash", repr(calls))
check("content channel emits no marker", not TAG_START_RE.search(c), repr(c[-100:]))

print()
print("=" * 78)
print("7. non-streaming responses path")
print("=" * 78)
obj = {"choices": [{"finish_reason": "stop",
                    "message": {"content": "", "reasoning_content": "plan\n" + D}}]}
res = wb.chat_to_response(obj, "deepseek-v4.1-flash")
rs = "".join(p.get("text", "") for it in res["output"] if it["type"] == "reasoning"
             for p in it.get("summary", []))
check("non-streaming reasoning clean", BAR not in rs and not TAG_START_RE.search(rs), repr(rs[-120:]))
check("non-streaming injects no function_call",
      not [i for i in res["output"] if i["type"] == "function_call"],
      repr([i["type"] for i in res["output"]]))

print()
print("=" * 78)
print("8. inbound reasoning item -> upstream chat messages")
print("=" * 78)
payload = {"model": "deepseek-v4.1-flash", "input": [
    {"role": "user", "content": [{"type": "input_text", "text": "go"}]},
    {"type": "reasoning", "summary": [{"type": "summary_text", "text": "plan\n" + D}]},
    {"role": "assistant", "content": [{"type": "output_text", "text": "ok"}]},
]}
blob = json.dumps(wb._responses_input_to_messages(payload), ensure_ascii=False)
check("replayed input carries no marker", BAR not in blob and not TAG_START_RE.search(blob), blob[-200:])
check("replayed input keeps normal reasoning", "plan" in blob, blob[:200])

print()
print("=" * 78)
print("9. backfill_reasoning_content + sanitize_messages")
print("=" * 78)
out = wb.backfill_reasoning_content(
    [{"role": "user", "content": "hi"},
     {"role": "assistant", "content": "x", "reasoning_content": "think\n" + D}],
    "deepseek-v4.1-flash")
blob = json.dumps(out, ensure_ascii=False)
check("backfill clean", BAR not in blob and not TAG_START_RE.search(blob), blob[-200:])
check("backfill keeps normal reasoning", "think" in blob)

out2 = wb.backfill_reasoning_content(
    [{"role": "assistant", "content": "x", "reasoning": "think2\n" + D}], "deepseek-v4.1-flash")
check("backfill(reasoning field) clean",
      not TAG_START_RE.search(json.dumps(out2, ensure_ascii=False)),
      json.dumps(out2, ensure_ascii=False)[:200])

out3 = wb.sanitize_messages([{"role": "assistant", "content": "x", "reasoning_content": "t\n" + D}])
check("sanitize_messages clean for any model",
      not TAG_START_RE.search(json.dumps(out3, ensure_ascii=False)),
      json.dumps(out3, ensure_ascii=False)[:200])

print()
print("=" * 78)
print("10. chat path: clean_chunk + aggregate_stream + scrub helper")
print("=" * 78)
line = json.dumps({"choices": [{"index": 0, "delta": {"reasoning_content": "t\n" + D},
                                "finish_reason": None}]})
cleaned = wb.clean_chunk(line)
check("clean_chunk strips", BAR not in cleaned and not TAG_START_RE.search(cleaned), cleaned[-160:])
line2 = json.dumps({"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]})
check("clean_chunk leaves ordinary frames identical", wb.clean_chunk(line2) == line2)

agg = wb.aggregate_stream(iter([chunk({"reasoning_content": "t\n" + D}),
                                chunk({"content": "answer"}), chunk({}, "stop"),
                                b"data: [DONE]\n"]), "deepseek-v4.1-flash", None)
blob = json.dumps(agg, ensure_ascii=False)
check("aggregate_stream clean", BAR not in blob and not TAG_START_RE.search(blob), blob[-200:])
check("aggregate_stream keeps content", "answer" in blob)

scrub = wb.Handler._scrub_stream_line(line)
check("_scrub_stream_line strips", BAR not in scrub and not TAG_START_RE.search(scrub), scrub[-160:])
check("_scrub_stream_line leaves ordinary line identical",
      wb.Handler._scrub_stream_line(line2) == line2)

print()
print("=" * 78)
print("11. BOUNDED BUFFER: over-limit path must not leak")
print("=" * 78)
# An unclosed envelope longer than REASON_DSML_MAX_HOLD forces the over-limit flush.
cap = wb.REASON_DSML_MAX_HOLD
big = LT + tag(FW) + " calls" + GT + "\n" + ("A" * (cap + 5000))
r, _, calls = run_stream([chunk({"reasoning_content": big}),
                          chunk({"content": "end"}), chunk({}, "stop"), b"data: [DONE]\n"])
assert_no_marker("over-limit single chunk: no marker", r)
check("over-limit single chunk: no function_call", len(calls) == 0, repr(calls))

# same payload, split at many boundaries around the cap
bad = []
for off in range(-40, 41, 7):
    cut = max(1, cap + off)
    r, _, calls = run_stream([chunk({"reasoning_content": big[:cut]}),
                              chunk({"reasoning_content": big[cut:]}),
                              chunk({"content": "z"}), chunk({}, "stop"), b"data: [DONE]\n"])
    if BAR in r or TAG_START_RE.search(r) or calls:
        bad.append(off)
check("over-limit split sweep clean", not bad, "bad offsets=%s" % bad)

# a complete envelope arriving after a huge clean prefix
r, _, calls = run_stream([chunk({"reasoning_content": "X" * (cap + 3000)}),
                          chunk({"reasoning_content": D}),
                          chunk({"content": "z"}), chunk({}, "stop"), b"data: [DONE]\n"])
assert_no_marker("huge prefix then envelope: no marker", r)
check("huge prefix then envelope: no function_call", len(calls) == 0, repr(calls))
check("huge prefix content preserved", r.startswith("X" * 100), repr(r[:60]))

print()
print("=" * 78)
print("12. stream error / truncation / cancellation")
print("=" * 78)


class Boom(Exception):
    pass


def exploding():
    yield chunk({"reasoning_content": "start\n" + D[:20]})
    raise Boom("upstream died")


leaked = []
raised = False
try:
    for fr in wb.stream_responses_events(exploding(), "deepseek-v4.1-flash", {}):
        txt = fr.decode("utf-8", "replace") if isinstance(fr, bytes) else str(fr)
        if BAR in txt or TAG_START_RE.search(txt):
            leaked.append(txt[:200])
except Boom:
    raised = True
check("upstream error propagates to caller", raised)
check("upstream error: zero leakage before propagation", not leaked, repr(leaked[:2]))

r, _, calls = run_stream([chunk({"reasoning_content": "ok\n" + D}),
                          chunk({"reasoning_content": "tail " + LT + FW + "DSM"}),
                          b"data: [DONE]\n"])
assert_no_marker("truncated mid-marker: no marker", r)
check("truncated: no function_call", len(calls) == 0, repr(calls))
check("truncated: body kept", "ok" in r, repr(r))

for frag in ["<y", "<div", "<a"]:
    rr, _, _ = run_stream([chunk({"reasoning_content": "head " + frag}),
                           chunk({"content": "z"}), chunk({}, "stop"), b"data: [DONE]\n"])
    check("ordinary fragment kept: %r" % frag, frag in rr, repr(rr))

rr, _, _ = run_stream([chunk({"reasoning_content": "head " + LT + FW + FW}),
                       chunk({"content": "z"}), chunk({}, "stop"), b"data: [DONE]\n"])
check("fullwidth-bar fragment dropped", BAR not in rr, repr(rr))

print()
print("=" * 78)
print("13. sentinel: reasoning-only DSML must never execute")
print("=" * 78)
SENTINEL = "SENTINEL_MUST_NEVER_RUN"
r, c, calls = run_stream([chunk({"reasoning_content": "I must call:\n" + dsml(cmd="touch /tmp/" + SENTINEL)}),
                          chunk({"content": "stopping"}), chunk({}, "stop"), b"data: [DONE]\n"])
blob = json.dumps(calls, ensure_ascii=False) + r + c
check("sentinel absent from every outbound artefact", SENTINEL not in blob, blob[:300])
check("no function_call produced", len(calls) == 0, repr(calls))

print()
print("=" * 78)
print("14. long clean reasoning still streams (no unbounded hold)")
print("=" * 78)
r, _, calls = run_stream([chunk({"reasoning_content": "A" * 3000}) for _ in range(12)]
                         + [chunk({"content": "end"}), chunk({}, "stop"), b"data: [DONE]\n"])
check("long reasoning emitted", len(r) >= 36000, "len=%d" % len(r))
check("long reasoning: no function_call", len(calls) == 0)

print()
print("=" * 78)
print("RESULT:  %d passed, %d failed" % (PASS, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("   FAILED: %s" % f)
    sys.exit(1)
print("ALL GREEN")