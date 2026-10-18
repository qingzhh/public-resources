"""Installer failure, integrity, idempotency and byte-exact rollback tests."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import apply as installer


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=os.environ["WB_OPS_TEST_STATE"])))
        self.bundle, self.target = self.root / "bundle", self.root / "target"
        (self.bundle / "files").mkdir(parents=True)
        self.target.mkdir()
        self.original = {name: ("# original " + name + "\n").encode() for name in installer.FILES - {"wb_ops.py"}}
        self.meta = {"patch_id": "test-ops", "base_tag": "v1.6.10", "files": {}, "compatibility": {}}
        for name in sorted(installer.FILES):
            patched = ("# patched " + name + "\n").encode()
            (self.bundle / "files" / name).write_bytes(patched)
            if name in self.original:
                (self.target / name).write_bytes(self.original[name])
            self.meta["files"][name] = {"base_sha256": installer.source_digest(self.original[name]) if name in self.original else None,
                                         "patched_sha256": installer.source_digest(patched)}
        for name in installer.DEPENDENCIES:
            content = ("unchanged " + name + "\n").encode()
            (self.target / name).write_bytes(content)
            self.meta["compatibility"][name] = installer.source_digest(content)
        (self.target / "accounts").mkdir()
        (self.target / "accounts" / "settings.json").write_text('{"key":"test-only-secret"}')
        (self.target / "usage").mkdir()
        (self.target / "usage" / "usage.jsonl").write_text('{"synthetic":true}\n')
        self.save_meta()
        self.regenerate_manifest()

    def save_meta(self):
        (self.bundle / "BASELINE.json").write_text(json.dumps(self.meta), encoding="utf-8")

    def regenerate_manifest(self):
        lines = []
        for path in sorted(self.bundle.rglob("*")):
            if path.is_file() and path.name != "MANIFEST.sha256":
                lines.append(installer.digest(path.read_bytes()) + "  " + path.relative_to(self.bundle).as_posix())
        (self.bundle / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def bundle_object(self):
        return installer.Bundle(self.bundle)

    def snapshot(self):
        return {p.relative_to(self.target).as_posix(): p.read_bytes() for p in self.target.rglob("*") if p.is_file()}

    def original_sources(self):
        return {name: (self.target / name).read_bytes() if (self.target / name).exists() else None for name in installer.FILES}

    def test_dry_run_is_read_only(self):
        before = self.snapshot()
        self.assertIn("dry-run OK", self.bundle_object().apply(self.target, dry_run=True))
        self.assertEqual(before, self.snapshot())

    def test_apply_and_byte_exact_rollback_preserve_user_data(self):
        bundle = self.bundle_object()
        before = self.snapshot()
        bundle.apply(self.target)
        self.assertEqual(set(bundle.status(self.target).values()), {"patched"})
        self.assertEqual((self.target / "accounts" / "settings.json").read_bytes(), before["accounts/settings.json"])
        self.assertEqual((self.target / "usage" / "usage.jsonl").read_bytes(), before["usage/usage.jsonl"])
        bundle.rollback(self.target)
        self.assertEqual(set(bundle.status(self.target).values()), {"base"})
        self.assertFalse((self.target / "wb_ops.py").exists())
        self.assertFalse((self.target / installer.STATE).exists())
        for name, content in before.items():
            self.assertEqual((self.target / name).read_bytes(), content)

    def test_idempotent_second_apply_writes_nothing(self):
        bundle = self.bundle_object()
        bundle.apply(self.target)
        before = self.snapshot()
        self.assertIn("already applied", bundle.apply(self.target))
        self.assertEqual(before, self.snapshot())

    def test_crlf_is_accepted_and_restored_byte_exactly(self):
        original = self.original["wb_proxy.py"].replace(b"\n", b"\r\n")
        (self.target / "wb_proxy.py").write_bytes(original)
        bundle = self.bundle_object()
        bundle.apply(self.target)
        bundle.rollback(self.target)
        self.assertEqual((self.target / "wb_proxy.py").read_bytes(), original)

    def test_unknown_source_is_rejected_before_writes(self):
        (self.target / "wb_proxy.py").write_bytes(b"# custom\n")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.bundle_object().apply(self.target)
        self.assertEqual(before, self.snapshot())

    def test_wrong_unchanged_dependency_is_rejected(self):
        (self.target / sorted(installer.DEPENDENCIES)[0]).write_bytes(b"# old unchanged module\n")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "unchanged dependency"):
            self.bundle_object().apply(self.target)
        self.assertEqual(before, self.snapshot())

    def test_mixed_manual_overlay_is_rejected(self):
        (self.target / "wb_ops.py").write_bytes((self.bundle / "files" / "wb_ops.py").read_bytes())
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.bundle_object().apply(self.target)
        self.assertEqual(before, self.snapshot())

    def test_payload_checksum_mismatch_is_rejected(self):
        (self.bundle / "files" / "wb_proxy.py").write_bytes(b"# corrupt\n")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.bundle_object()

    def test_payload_metadata_hash_mismatch_is_rejected(self):
        (self.bundle / "files" / "wb_proxy.py").write_bytes(b"# corrupt\n")
        self.regenerate_manifest()
        with self.assertRaisesRegex(ValueError, "source checksum mismatch"):
            self.bundle_object()

    def test_manifest_traversal_is_rejected(self):
        with (self.bundle / "MANIFEST.sha256").open("a") as stream:
            stream.write("0" * 64 + "  ../outside.py\n")
        with self.assertRaisesRegex(ValueError, "unsafe bundle path"):
            self.bundle_object()

    def test_payload_syntax_error_is_rejected(self):
        name = "wb_ops.py"
        data = b"def invalid(\n"
        (self.bundle / "files" / name).write_bytes(data)
        self.meta["files"][name]["patched_sha256"] = installer.source_digest(data)
        self.save_meta()
        self.regenerate_manifest()
        with self.assertRaises(SyntaxError):
            self.bundle_object()

    def test_existing_lock_is_not_deleted(self):
        lock = self.target / installer.LOCK
        lock.write_bytes(b"99999\n")
        before = self.snapshot()
        with self.assertRaises(FileExistsError):
            self.bundle_object().apply(self.target)
        self.assertEqual(before, self.snapshot())

    def test_write_failure_restores_existing_sources(self):
        bundle = self.bundle_object()
        before = self.original_sources()
        actual_replace = os.replace
        calls = []

        def fail_once(source, target):
            target = Path(target)
            if target.parent == self.target and target.name in installer.FILES:
                calls.append(target.name)
                if len(calls) == 2:
                    raise OSError("injected replacement failure")
            return actual_replace(source, target)

        with mock.patch.object(installer.os, "replace", side_effect=fail_once):
            with self.assertRaises(OSError):
                bundle.apply(self.target)
        self.assertEqual(before, self.original_sources())
        self.assertFalse((self.target / installer.STATE).exists())
        self.assertFalse((self.target / installer.LOCK).exists())

    def test_state_commit_failure_recovers_all_sources(self):
        bundle = self.bundle_object()
        before = self.original_sources()
        actual_replace = os.replace

        def fail_state(source, target):
            if Path(target).name == installer.STATE:
                raise OSError("injected state failure")
            return actual_replace(source, target)

        with mock.patch.object(installer.os, "replace", side_effect=fail_state):
            with self.assertRaises(OSError):
                bundle.apply(self.target)
        self.assertEqual(before, self.original_sources())

    def test_edited_patched_file_blocks_rollback(self):
        bundle = self.bundle_object()
        bundle.apply(self.target)
        (self.target / "wb_proxy.py").write_bytes(b"# subsequent user edit\n")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "refuses to overwrite"):
            bundle.rollback(self.target)
        self.assertEqual(before, self.snapshot())

    def test_corrupt_backup_blocks_rollback(self):
        bundle = self.bundle_object()
        bundle.apply(self.target)
        state = json.loads((self.target / installer.STATE).read_text())
        (self.target / state["backup"] / "wb_proxy.py").write_bytes(b"# corrupt\n")
        before = self.original_sources()
        with self.assertRaisesRegex(ValueError, "backup checksum"):
            bundle.rollback(self.target)
        self.assertEqual(before, self.original_sources())

    def test_backup_null_marker_cannot_delete_existing_module(self):
        bundle = self.bundle_object()
        bundle.apply(self.target)
        path = self.target / installer.STATE
        state = json.loads(path.read_text())
        state["files"]["wb_proxy.py"]["sha256"] = None
        path.write_text(json.dumps(state))
        before = self.original_sources()
        with self.assertRaisesRegex(ValueError, "absence marker"):
            bundle.rollback(self.target)
        self.assertEqual(before, self.original_sources())

    def test_rollback_dry_run_writes_nothing(self):
        bundle = self.bundle_object()
        bundle.apply(self.target)
        before = self.snapshot()
        self.assertIn("dry-run OK", bundle.rollback(self.target, dry_run=True))
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main(verbosity=2)
