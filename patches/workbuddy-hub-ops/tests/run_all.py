#!/usr/bin/env python3
"""Run upstream and ops tests in a copied, patched, offline sandbox."""
import argparse
import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apply import Bundle, digest, safe_relative, source_digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="complete clean v1.7.2 checkout, or this exact patched checkout")
    parser.add_argument("--output", default=str(ROOT / ".work" / "verification"), help="sandbox/log directory")
    args = parser.parse_args()
    bundle = Bundle(ROOT)
    source = bundle.target(args.source)
    status = set(bundle.status(source).values())
    if status not in ({"base"}, {"patched"}):
        raise ValueError("test source is unknown or partially patched")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"base_tag": bundle.meta["base_tag"], "patch_id": bundle.meta["patch_id"],
              "run_id": uuid.uuid4().hex, "started_at_unix": time.time(),
              "tested_payload_sha256": {name: entry["patched_sha256"] for name, entry in bundle.files.items()},
              "upstream_commit": bundle.meta["upstream_commit"], "suites": [],
              "network": "Python subprocesses deny non-loopback networking",
              "node_available": bool(shutil.which("node"))}
    print(json.dumps({"run_started": report["run_id"]}), flush=True)
    with tempfile.TemporaryDirectory(prefix="sandbox-", dir=output) as directory:
        sandbox = Path(directory)
        gateway, state = sandbox / "gateway", sandbox / "state"
        gateway.mkdir()
        state.mkdir()
        home = state / "home"
        home.mkdir()
        for name, entry in bundle.meta["source_tree"].items():
            relative = safe_relative(name)
            original = source / relative
            if original.is_symlink() or not original.resolve().is_relative_to(source):
                raise ValueError("source symlink is not allowed: " + name)
            data = original.read_bytes()
            value = source_digest(data) if entry["text"] else digest(data)
            allowed = {entry["sha256"]}
            if name in bundle.files:
                allowed.add(bundle.files[name]["patched_sha256"])
            if value not in allowed:
                raise ValueError("unknown upstream test/runtime source: " + name)
            destination = gateway / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data.replace(b"\r\n", b"\n") if entry["text"] else data)
        for name, data in bundle.payload.items():
            (gateway / name).write_bytes(data)
        # Explicit isolated packaging profile: official release assertions
        # remain intact, but the checked app now includes our extra module.
        # This does NOT build or certify a trimmed portable Python runtime.
        manifest = gateway / "release/portable.txt"
        manifest.write_text(manifest.read_text(encoding="utf8") + "\nwb_ops.py\n", encoding="utf8")
        runtime = gateway / "release/portable_runtime.py"
        runtime.write_text(runtime.read_text(encoding="utf8") + '\nREQUIRED_FILES += ("Lib/copy.py", "Lib/gzip.py", "Lib/struct.py", "Lib/xml/__init__.py")\n', encoding="utf8")
        # unicodedata is already in REQUIRED_FILES as DLLs/unicodedata.pyd.
        # The official check only recognizes Lib/ files; also recognize the
        # real extension location, without inventing a nonexistent .py file.
        assets = gateway / "tests/_test_release_assets.py"
        original = assets.read_text(encoding="utf8")
        needle = '("Lib/%s/__init__.py" % m) not in covered]'
        assert original.count(needle) == 1
        assets.write_text(original.replace(needle, '("Lib/%s/__init__.py" % m) not in covered and ("DLLs/%s.pyd" % m) not in covered]'), encoding="utf8")
        report["isolated_packaging_profile"] = "wb_ops plus copy/gzip/struct/xml; existing DLLs/unicodedata.pyd recognized by extended assertion; no assertions removed; portable runtime not built"
        for path in gateway.glob("wb_*.py"):
            ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
                   WB_TEST_SOURCE=str(gateway), WB_OPS_TEST_STATE=str(state), WB_OPS_OFFLINE="1",
                   ACCOUNTS_DIR=str(state / "accounts"), API_KEY="", WB_PROXY_KEY="",
                   USAGE_DIR=str(state / "usage"), WB_USAGE_DIR=str(state / "usage"),
                   HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "AppData" / "Roaming"),
                   LOCALAPPDATA=str(home / "AppData" / "Local"),
                   TMP=str(state), TEMP=str(state), TMPDIR=str(state))
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
                    "WB_SEARCH_ENDPOINT", "WB_OPS_BING_FALLBACK"):
            env.pop(key, None)
        env["NO_PROXY"] = "127.0.0.1,localhost,::1"
        env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "tests" / "safety"), str(gateway)])
        suites = [("upstream", gateway / "tests" / "run_all.py")]
        suites += [(p.stem, p) for p in sorted((ROOT / "tests" / "legacy").glob("test_*.py"))]
        suites += [("integration", ROOT / "tests" / "test_integration.py"),
                   ("migration_v1617", ROOT / "tests" / "test_v1617.py"),
                   ("migration_v172", ROOT / "tests" / "test_v172.py"),
                   ("migration_v1620", ROOT / "tests" / "test_v1620.py"),
                   ("review_regressions", ROOT / "tests" / "test_review_regressions.py"),
                   ("reasoning_recovery", ROOT / "tests" / "test_reasoning_recovery.py"),
                   ("loop_recovery", ROOT / "tests" / "test_loop_recovery.py"),
                   ("dated_guards", ROOT / "tests" / "test_dated_guards.py"),
                   ("nullable_buffer", ROOT / "tests" / "test_nullable_buffer.py"),
                   ("metadata_buffer", ROOT / "tests" / "test_metadata_buffer.py"),
                   ("recovery_hint", ROOT / "tests" / "test_recovery_hint.py"),
                   ("extra_fields", ROOT / "tests" / "test_extra_fields.py"),
                   ("sse_framing", ROOT / "tests" / "test_sse_framing.py"),
                   ("observability", ROOT / "tests" / "test_observability.py"),
                   ("compact_buffer", ROOT / "tests" / "test_compact_buffer.py"),
                   ("buffer_records", ROOT / "tests" / "test_buffer_records.py"),
                   ("installer", ROOT / "tests" / "test_installer.py")]
        for label, path in suites:
            started = time.monotonic()
            log = output / (label + ".log")
            with log.open("w", encoding="utf-8") as sink:
                suite_env = dict(env, WB_OPS_LOOP_RETRY="1" if label == "loop_recovery" else "0")
                # The official prompt-mode suite asserts its original 403
                # opt-in behavior; our migration suite checks the safe default.
                suite_env["WB_OPS_ALLOW_PROMPT_DEGRADE"] = "1" if label == "upstream" else "0"
                process = subprocess.run([sys.executable, "-B", str(path)], cwd=gateway, env=suite_env,
                                         stdout=sink, stderr=subprocess.STDOUT, timeout=600)
            text = log.read_text(encoding="utf-8", errors="replace")
            row = {"suite": label, "returncode": process.returncode,
                   "seconds": round(time.monotonic() - started, 2), "log": log.name}
            if label == "upstream":
                matches = re.findall(r"(\d+) passed, (\d+) failed, (\d+) skipped", text)
                if matches:
                    row.update(zip(("passed", "failed", "skipped"), map(int, matches[-1])))
                else:
                    row["summary_missing"] = True
            elif label in ("migration_v172", "review_regressions", "migration_v1620", "migration_v1617", "integration", "reasoning_recovery", "loop_recovery", "dated_guards", "nullable_buffer", "metadata_buffer", "recovery_hint", "extra_fields", "sse_framing", "observability", "compact_buffer", "buffer_records", "installer"):
                count = re.search(r"Ran (\d+) tests", text)
                row["tests"] = int(count.group(1)) if count else None
            else:
                match = re.search(r"RESULT:\s*(\d+) passed,\s*(\d+) failed", text)
                if match:
                    row.update(zip(("assertions_passed", "assertions_failed"), map(int, match.groups())))
            report["suites"].append(row)
            print(json.dumps(row), flush=True)
            if process.returncode:
                print("\n".join(text.splitlines()[-60:]), flush=True)
        report["passed"] = all(row["returncode"] == 0 and not row.get("summary_missing") and not row.get("skipped", 0)
                               for row in report["suites"])
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
