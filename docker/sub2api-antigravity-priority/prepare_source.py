#!/usr/bin/env python3
"""Assemble the pinned source tree from public upstream code and hash-checked overlays."""
import argparse
import hashlib
import io
import json
import pathlib
import tarfile
import urllib.request


def sha(data):
    return hashlib.sha256(data).hexdigest()


def prepare(package, destination, archive=None):
    metadata = json.loads((package / 'source-manifest.json').read_text())
    if destination.exists():
        raise RuntimeError('Destination already exists; refusing to replace source files')
    for section in ['overlay', 'engine']:
        for name in metadata[section]:
            path = pathlib.PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or not path.parts or '\\' in name or ':' in name:
                raise RuntimeError('Invalid source manifest path')
        actual = set()
        for path in (package / section).rglob('*'):
            if path.is_symlink():
                raise RuntimeError('Unexpected source symlink')
            if path.is_file():
                actual.add(path.relative_to(package / section).as_posix())
        if actual != set(metadata[section]):
            raise RuntimeError('Source file set differs from the manifest: ' + section)
    if archive is None:
        url = 'https://codeload.github.com/Wei-Shaw/sub2api/tar.gz/' + metadata['upstream_commit']
        with urllib.request.urlopen(url, timeout=90) as response:
            archive = response.read()
    members = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as source:
        for member in source.getmembers():
            parts = pathlib.PurePosixPath(member.name).parts[1:]
            if not parts or member.isdir():
                continue
            if member.issym() or member.islnk():
                continue
            if not member.isfile() or any(part in ['', '.', '..'] for part in parts):
                raise RuntimeError('Unexpected upstream archive entry')
            stream = source.extractfile(member)
            if stream is None:
                raise RuntimeError('Unreadable upstream source')
            members['/'.join(parts)] = stream.read()
    for name, entry in metadata['overlay'].items():
        original = members.get(name)
        actual = None if original is None else sha(original.replace(b'\r\n', b'\n'))
        if actual != entry['upstream_sha256']:
            raise RuntimeError('Upstream overlay contract changed: ' + name)
        if sha((package / 'overlay' / name).read_bytes()) != entry['sha256']:
            raise RuntimeError('Overlay checksum mismatch: ' + name)
    for name, expected in metadata['engine'].items():
        if sha((package / 'engine' / name).read_bytes()) != expected:
            raise RuntimeError('Engine checksum mismatch: ' + name)
    destination.mkdir(parents=True)
    upstream = destination / 'sub2api'
    for name, data in members.items():
        path = upstream / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for name in metadata['overlay']:
        path = upstream / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((package / 'overlay' / name).read_bytes())
    for name in metadata['engine']:
        path = destination / 'engine' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((package / 'engine' / name).read_bytes())
    for name in ['build.sh', 'Dockerfile', '.dockerignore', 'sources.json']:
        (destination / name).write_bytes((package / name).read_bytes())
    print(json.dumps({'prepared': True, 'upstream_commit': metadata['upstream_commit'], 'overlay_files': len(metadata['overlay']), 'engine_files': len(metadata['engine'])}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=pathlib.Path)
    parser.add_argument('--upstream-archive', type=pathlib.Path)
    args = parser.parse_args()
    prepare(pathlib.Path(__file__).resolve().parent, args.destination.resolve(), args.upstream_archive.read_bytes() if args.upstream_archive else None)
