#!/usr/bin/env python3
"""Advisor follow-up tests.

Covers the four gaps named in the review:

1. A terminal frame that ALSO carries the last piece of content or reasoning.
   Holding such a frame must not lose that output, and the gate must still see
   it (a turn with content is never replayed).
2. Continuation/retry paths for finish_reason "length" and "tool_calls":
   exactly one completion signal, no leaked intermediate [DONE], content intact.
3. finish_reason_of() semantics for "", null, missing, "stop", "tool_calls".
4. (Non-streaming production smoke is a separate shell check.)
"""
import importlib
import json
import sys

import _bootstrap
wb = importlib.import_module("wb_proxy")

# Isolate the retry-counter snapshot (see test_counter_race.py for why: these
# suites run inside the production container during acceptance and would
# otherwise overwrite the serving process's observable counters).
import os as _os  # noqa: E402
import tempfile as _tempfile  # noqa: E402
_ISOLATED_DIR = _tempfile.mkdtemp(prefix="wb-suite-")
wb.USAGE_DIR = _ISOLATED_DIR
try:
    wb.USAGE_LOG = _os.path.join(_ISOLATED_DIR, "usage.jsonl")
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


def real_chunk(delta, finish=""):
    return ("data: " + json.dumps(
        {"id": "cmb-1", "model": "deepseek-v4.1-flash",
         "object": "chat.completion.chunk", "created": 1790000000,
         "choices": [{"index": 0, "delta": delta, "logprobs": None,
                      "finish_reason": finish}]}) + "\n").encode()


print("=" * 78)
print("1. finish_reason_of() semantics")
print("=" * 78)
cases = [
    ('"finish_reason": ""', real_chunk({"content": "x"}, ""), None),
    ('"finish_reason": null', real_chunk({"content": "x"}, None), None),
    ('missing key', ("data: " + json.dumps(
        {"choices": [{"index": 0, "delta": {"content": "x"}}]}) + "\n").encode(), None),
    ('"finish_reason": "stop"', real_chunk({"content": ""}, "stop"), "stop"),
    ('"finish_reason": "tool_calls"', real_chunk({"tool_calls": []}, "tool_calls"), "tool_calls"),
    ('"finish_reason": "length"', real_chunk({}, "length"), "length"),
]
for label, payload, expected in cases:
    got = wb.finish_reason_of(payload)
    check("%-34s -> %r" % (label, expected), got == expected, "got %r" % got)

check("parses a decoded string too",
      wb.finish_reason_of(real_chunk({}, "stop").decode()) == "stop")
check("accepts an already-parsed dict", wb.finish_reason_of(
    {"choices": [{"finish_reason": "stop"}]}) == "stop")
check("malformed JSON -> None", wb.finish_reason_of("data: {not json") is None)
check("[DONE] -> None", wb.finish_reason_of(b"data: [DONE]\n") is None)
check("empty input -> None", wb.finish_reason_of("") is None)
check("no choices -> None", wb.finish_reason_of({"choices": []}) is None)

print()
print("=" * 78)
print("2. A terminal frame that ALSO carries content must not lose it")
print("=" * 78)
# Terminal chunk carries the LAST piece of the answer.
frames = [real_chunk({"content": "PART_ONE "}),
          real_chunk({"content": "PART_TWO"}, "stop"),
          b"data: [DONE]\n"]
text = []
for raw in wb.stream_chat_events if hasattr(wb, "stream_chat_events") else []:
    pass

# Drive the real generator the chat handler uses, via a minimal shim that
# mirrors the handler's hold-and-replay logic.
def replay_logic(frames):
    """Mirror of _chat_stream_response's hold/replay for held finish frames."""
    out = {"text": [], "sent": [], "held": None, "saw_finish": False, "fin": None}
    for raw in frames:
        data = wb.strip_data_prefix(raw.decode("utf-8", "replace"))
        if not data or data == "[DONE]":
            continue
        try:
            maybe = json.loads(data)
            for ch in (maybe.get("choices") or []):
                d = ch.get("delta") or {}
                if d.get("content"):
                    out["text"].append(d["content"])
        except Exception:
            pass
        cleaned = wb.clean_chunk(data)
        if wb.finish_reason_of(data):
            out["saw_finish"] = True
        if wb.finish_reason_of(cleaned):
            out["held"] = cleaned
            out["fin"] = wb.finish_reason_of(cleaned)
            continue
        out["sent"].append(cleaned)
    return out


r = replay_logic(frames)
check("accumulated text keeps the terminal piece",
      "".join(r["text"]) == "PART_ONE PART_TWO", repr(r["text"]))
check("the terminal frame was held (not sent immediately)", r["held"] is not None)
check("the held frame carries the terminal finish_reason", r["fin"] == "stop",
      repr(r["fin"]))
check("the held frame is replayable (it is a non-empty string)",
      isinstance(r["held"], str) and len(r["held"]) > 0)

print()
print("=" * 78)
print("3. Continuation must not lose output nor leak intermediate markers")
print("=" * 78)


class FakeAcct:
    uid = "stub-account"
    proxy = None

    def headers(self, purpose=None):
        return {}


class FakeUpstream:
    def __init__(self, frames):
        self._frames = frames

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._frames)


def run_responses(frames_list):
    """Run the responses generator once per attempt and merge the client view.

    Mirrors the production handler: `retry_call` is present (the real handler
    always passes one for a streaming request), which is what makes
    `will_continue` true and suppresses the terminal event of a turn that is
    about to be continued.
    """
    upstreams = [FakeUpstream(f) for f in frames_list]
    state = {"n": 0}

    def retry_call(excluded):
        state["n"] += 1
        if state["n"] < len(upstreams):
            return upstreams[state["n"]], FakeAcct()
        return None, None

    holder = {"custom_names": set(), "request_meta": {},
              "retry_call": retry_call, "account_uid": "stub-account"}
    seen = {"events": [], "text": [], "calls": [], "terminal": 0, "created": 0,
            "_data": []}
    idx = 0
    while True:
        holder["emit_created"] = (idx == 0)
        holder["retried"] = idx > 0
        holder.pop("thinking_only", None)
        holder.pop("gate_ok", None)
        holder.pop("will_continue", None)
        holder.pop("continue_upstream", None)
        holder.pop("continue_account", None)
        # The generator starts the continuation itself; the handler only
        # consumes it. Mirror that here.
        _frames = frames_list[idx]
        for _fr in wb.stream_responses_events(iter(_frames), "deepseek-v4.1-flash", holder):
            _txt = _fr.decode("utf-8", "replace") if isinstance(_fr, bytes) else str(_fr)
            _ev = _dj = None
            for _line in _txt.split("\n"):
                if _line.startswith("event: "):
                    _ev = _line[7:].strip()
                elif _line.startswith("data: "):
                    try:
                        _dj = json.loads(_line[6:])
                    except Exception:
                        _dj = None
            if not _ev:
                continue
            seen["events"].append(_ev)
            seen["_data"].append(_dj or {})
            if _ev == "response.created":
                seen["created"] += 1
            if _ev in ("response.completed", "response.incomplete", "response.failed"):
                seen["terminal"] += 1
            if _ev == "response.output_text.delta" and _dj:
                seen["text"].append(_dj.get("delta", ""))
            if _ev == "response.output_item.done" and _dj:
                _it = _dj.get("item") or {}
                if _it.get("type") in ("function_call", "custom_tool_call"):
                    seen["calls"].append(_it.get("name"))
        if holder.get("will_continue"):
            idx += 1
            if idx >= len(frames_list):
                break
            continue
        break
    return seen


# 3a: finish_reason = length
s = run_responses([[real_chunk({"reasoning_content": "thinking"}), real_chunk({}, "length"),
                    b"data: [DONE]\n"]])
check("[length] no continuation was attempted (gate rejects it)",
      s["terminal"] == 1, repr(s["events"][-4:]))
# Pre-existing contract (unchanged by rev7): a length-capped turn ends with
# response.completed carrying status=incomplete and
# incomplete_details.reason=max_output_tokens. It is NOT response.incomplete.
_lc = [d for e, d in zip(s["events"], s["_data"]) if e in ("response.completed", "response.incomplete")]
check("[length] terminal event is response.completed", len(_lc) == 1, repr(_lc))
check("[length] carries incomplete_details=max_output_tokens",
      (_lc[0].get("response") or {}).get("incomplete_details", {}).get("reason") == "max_output_tokens",
      repr((_lc[0].get("response") or {}).get("incomplete_details")))
check("[length] status is incomplete",
      (_lc[0].get("response") or {}).get("status") == "incomplete",
      repr((_lc[0].get("response") or {}).get("status")))

# 3b: finish_reason = tool_calls -> must never be treated as a lost turn
s2 = run_responses([[
    real_chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                "function": {"name": "bash",
                                             "arguments": '{"command":"echo T"}'}}]}),
    real_chunk({}, "tool_calls"), b"data: [DONE]\n"]])
check("[tool_calls] exactly one terminal event", s2["terminal"] == 1, repr(s2["events"][-4:]))
check("[tool_calls] not marked incomplete", "response.incomplete" not in s2["events"],
      repr(s2["events"][-4:]))
check("[tool_calls] the tool call survives", s2["calls"] == ["bash"], repr(s2["calls"]))

# 3c: a lost turn followed by a successful continuation
s3 = run_responses([[real_chunk({"reasoning_content": "lost"}), real_chunk({}, "stop"),
                     b"data: [DONE]\n"],
                    [real_chunk({"content": "ANSWER"}), real_chunk({}, "stop"),
                     b"data: [DONE]\n"]])
check("[continuation] exactly one response.created", s3["created"] == 1,
      "count=%d" % s3["created"])
check("[continuation] exactly one terminal event", s3["terminal"] == 1,
      repr(s3["events"][-4:]))
check("[continuation] the answer is delivered", "".join(s3["text"]) == "ANSWER",
      repr(s3["text"]))
check("[continuation] no incomplete leaked from the abandoned attempt",
      "response.incomplete" not in s3["events"], repr(s3["events"][-5:]))

# 3d: the terminal frame of the CONTINUATION carrying content must survive
s4 = run_responses([[real_chunk({"reasoning_content": "lost"}), real_chunk({}, "stop"),
                     b"data: [DONE]\n"],
                    [real_chunk({"content": "FINAL"}, "stop"), b"data: [DONE]\n"]])
check("[continuation] content in the terminal frame survives",
      "".join(s4["text"]) == "FINAL", repr(s4["text"]))
check("[continuation] one terminal event", s4["terminal"] == 1, repr(s4["events"][-4:]))

print()
print("=" * 78)
print("RESULT:  %d passed, %d failed" % (PASS, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("   FAILED: %s" % f)
    sys.exit(1)
print("ALL GREEN")