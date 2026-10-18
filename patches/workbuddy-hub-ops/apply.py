#!/usr/bin/env python3
"""Apply this exact patch bundle without importing the gateway or its state."""
import argparse
import ast
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import time
import uuid

STATE = ".ops-patch-state.json"
BACKUPS = ".ops-patch-backups"
LOCK = ".ops-patch.lock"
FILES = {"wb_proxy.py", "wb_webtools.py", "wb_ops.py", "wb_accounts.py"}
DEPENDENCIES = {"wb_catalog.py", "wb_identity.py", "wb_scheduler.py",
                "wb_settings.py", "wb_webagent.py", "dashboard.html", "wb_prompt.py", "wb_pricing.py",
                "wb_activity.py", "wb_agents.py", "wb_atrest.py", "wb_fingerprint.py",
                "wb_ipintel.py", "wb_modelsdev.py", "wb_probes.py", "wb_tasks.py",
                "wb_updates.py", "wb_upstream_pool.py", "wb_egress.py"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_digest(data):
    return digest(data.replace(b"\r\n", b"\n"))


def safe_relative(name):
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(p in ("..", ".") for p in path.parts) or "\\" in name:
        raise ValueError("unsafe bundle path: " + name)
    return path


def checked_path(path):
    """Reject links/reparse points in every existing component, before resolve."""
    path = Path(path).absolute()
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("symlink/reparse point is not allowed: " + str(component))
    return path


def atomic_write(path, data, mode=0o644):
    checked_path(path)
    temporary = path.with_name(".ops-stage-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def target_lock(target):
    path = checked_path(target / LOCK)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (str(os.getpid()) + "\n").encode())
        os.close(fd)
        fd = None
        yield
    finally:
        if fd is not None:
            os.close(fd)
        path.unlink(missing_ok=True)


class Bundle:
    def __init__(self, root):
        self.root = checked_path(root).resolve()
        checked_path(self.root / "MANIFEST.sha256")
        listed = set()
        for line in (self.root / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines():
            expected, name = line.split("  ", 1)
            relative = safe_relative(name)
            if name in listed or not re.fullmatch("[0-9a-f]{64}", expected):
                raise ValueError("invalid checksum entry: " + name)
            listed.add(name)
            path = checked_path(self.root / relative)
            if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                raise ValueError("bundle symlink is not allowed: " + name)
            if digest(path.read_bytes()) != expected:
                raise ValueError("bundle checksum mismatch: " + name)
        if "BASELINE.json" not in listed:
            raise ValueError("BASELINE.json must be covered by MANIFEST.sha256")
        self.meta = json.loads((self.root / "BASELINE.json").read_text(encoding="utf-8"))
        self.files = self.meta["files"]
        self.dependencies = self.meta.get("compatibility", {})
        if set(self.files) != FILES or set(self.dependencies) != DEPENDENCIES or not self.meta.get("patch_id") or not self.meta.get("base_tag"):
            raise ValueError("invalid patch metadata")
        self.payload = {}
        for name, entry in self.files.items():
            payload_name = "files/" + name
            if payload_name not in listed:
                raise ValueError("payload is not covered by checksums: " + name)
            data = (self.root / payload_name).read_bytes()
            if source_digest(data) != entry["patched_sha256"]:
                raise ValueError("payload/source checksum mismatch: " + name)
            ast.parse(data.decode("utf-8"), filename=name)
            self.payload[name] = data

    def target(self, directory):
        target = checked_path(directory).resolve(strict=True)
        if not target.is_dir():
            raise ValueError("target must be an existing gateway directory")
        for name in FILES | DEPENDENCIES | {STATE, BACKUPS, LOCK}:
            checked_path(target / name)
            if (target / name).is_symlink():
                raise ValueError("target symlink is not allowed: " + name)
        for name, expected in self.dependencies.items():
            if source_digest((target / name).read_bytes()) != expected:
                raise ValueError("unchanged dependency is not exact " + self.meta["base_tag"] + ": " + name)
        return target

    def status(self, target):
        result = {}
        for name, entry in self.files.items():
            path = target / name
            actual = source_digest(path.read_bytes()) if path.exists() else None
            if actual == entry["base_sha256"]:
                result[name] = "base"
            elif actual == entry["patched_sha256"]:
                result[name] = "patched"
            else:
                result[name] = "unknown"
        return result

    def commit(self, target, replacement, before, modes):
        attempted = []
        try:
            for name, data in replacement.items():
                path = checked_path(target / name)
                current = path.read_bytes() if path.exists() else None
                if current != before[name]:
                    raise RuntimeError(name + " changed after validation; not overwritten")
                attempted.append(name)
                if data is None:
                    path.unlink()
                else:
                    atomic_write(path, data, modes.get(name, 0o644))
        except BaseException as failure:
            errors = []
            for name in reversed(attempted):
                path = target / name
                original = before[name]
                try:
                    checked_path(path)
                    current = path.read_bytes() if path.exists() else None
                except (OSError, ValueError) as error:
                    errors.append(name + ": " + str(error))
                    continue
                if current == original:
                    continue
                expected = replacement[name]
                if current != expected:
                    errors.append(name + " changed externally; not overwritten")
                    continue
                try:
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_write(path, original, modes.get(name, 0o644))
                except OSError as error:
                    errors.append(name + ": " + str(error))
            if errors:
                raise RuntimeError("write failed; automatic recovery incomplete: " + "; ".join(errors)) from failure
            raise

    def apply(self, directory, dry_run=False):
        target = self.target(directory)
        status = self.status(target)
        if set(status.values()) == {"patched"}:
            return "already applied; no files changed"
        if set(status.values()) != {"base"}:
            raise ValueError("unknown or mixed source; use clean " + self.meta["base_tag"] + ": " + json.dumps(status))
        if (target / STATE).exists():
            raise ValueError("existing patch state; inspect it before applying")
        if dry_run:
            return "dry-run OK: exact " + self.meta["base_tag"] + "; no files changed"
        with target_lock(target):
            self.target(target)
            if set(self.status(target).values()) != {"base"}:
                raise ValueError("target changed during validation")
            if (target / STATE).exists():
                raise ValueError("patch state appeared during validation")
            original, modes, state_files = {}, {}, {}
            backup_name = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:12]
            backup = target / BACKUPS / backup_name
            backup.mkdir(parents=True, mode=0o700)
            for name in self.files:
                path = checked_path(target / name)
                original[name] = path.read_bytes() if path.exists() else None
                actual = source_digest(original[name]) if original[name] is not None else None
                if actual != self.files[name]["base_sha256"]:
                    raise ValueError("target changed while taking backup: " + name)
                modes[name] = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
                if original[name] is not None:
                    atomic_write(backup / name, original[name], modes[name])
                state_files[name] = {"sha256": digest(original[name]) if original[name] is not None else None,
                                     "mode": modes[name]}
            state = {"patch_id": self.meta["patch_id"], "base_tag": self.meta["base_tag"],
                     "backup": (PurePosixPath(BACKUPS) / backup_name).as_posix(), "files": state_files}
            replacement = dict(self.payload)
            replacement[STATE] = (json.dumps(state, indent=2) + "\n").encode()
            original[STATE], modes[STATE] = None, 0o600
            self.commit(target, replacement, original, modes)
        return "applied " + self.meta["patch_id"] + "; backup: " + str(backup)

    def rollback_data(self, target):
        self.target(target)
        state_bytes = (target / STATE).read_bytes()
        state = json.loads(state_bytes)
        if state.get("patch_id") != self.meta["patch_id"] or set(state.get("files", {})) != FILES:
            raise ValueError("backup belongs to a different patch")
        if set(self.status(target).values()) != {"patched"}:
            raise ValueError("patched files changed; rollback refuses to overwrite them")
        relative = safe_relative(state["backup"])
        if relative.parts[0] != BACKUPS:
            raise ValueError("invalid backup directory")
        backup = checked_path(target / relative)
        if backup.is_symlink() or not backup.resolve().is_relative_to(target):
            raise ValueError("backup symlink is not allowed")
        restore, before, modes = {}, {}, {}
        for name, entry in state["files"].items():
            path = checked_path(backup / name)
            if path.is_symlink():
                raise ValueError("backup symlink is not allowed: " + name)
            if (entry["sha256"] is None) != (self.files[name]["base_sha256"] is None):
                raise ValueError("invalid backup absence marker: " + name)
            if type(entry["mode"]) is not int or not 0 <= entry["mode"] <= 0o7777:
                raise ValueError("invalid backup mode: " + name)
            data = path.read_bytes() if entry["sha256"] is not None else None
            if data is not None and (digest(data) != entry["sha256"] or source_digest(data) != self.files[name]["base_sha256"]):
                raise ValueError("backup checksum mismatch: " + name)
            restore[name], before[name], modes[name] = data, (target / name).read_bytes(), entry["mode"]
            if source_digest(before[name]) != self.files[name]["patched_sha256"]:
                raise ValueError("target changed while preparing rollback: " + name)
        restore[STATE], before[STATE], modes[STATE] = None, state_bytes, 0o600
        return restore, before, modes, backup

    def rollback(self, directory, dry_run=False):
        target = self.target(directory)
        restore, before, modes, backup = self.rollback_data(target)
        if dry_run:
            return "rollback dry-run OK; no files changed"
        with target_lock(target):
            restore, before, modes, backup = self.rollback_data(target)
            self.commit(target, restore, before, modes)
        return "rolled back; original backup retained at " + str(backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", help="existing official gateway directory (not accounts/usage)")
    parser.add_argument("--dry-run", action="store_true", help="validate without modifying the target")
    parser.add_argument("--rollback", action="store_true", help="restore this bundle's saved originals")
    parser.add_argument("--verify-package", action="store_true", help="verify bundle checksums only")
    args = parser.parse_args()
    try:
        bundle = Bundle(Path(__file__).resolve().parent)
        if args.verify_package:
            print("package checksums OK: " + bundle.meta["patch_id"])
            return 0
        if not args.target:
            parser.error("--target is required")
        action = bundle.rollback if args.rollback else bundle.apply
        print(action(args.target, dry_run=args.dry_run))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, SyntaxError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
