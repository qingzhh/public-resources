#!/usr/bin/env python3
"""Restore a tested Priority build without uploading or replacing local data."""
import argparse
import datetime
import fcntl
import gzip
import hashlib
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

import priority_mapping

VERSION = '0.2.16-agpriority.1.2.18.sync.2'
UPSTREAM = '0.2.16'
RELEASE = 'sub2api-priority-v0.2.16-sync.2'
ASSET = 'sub2api-priority-linux-amd64.gz'
ASSET_URL = 'https://github.com/qingzhh/public-resources/releases/download/' + RELEASE + '/' + ASSET
ARCHIVE_SHA256 = '1489a6f61aa10183973483eae56839a0c5b0825122df46ee787d9baa3c558948'
BINARY_SHA256 = 'eb9ef8d6bf5e5ffb94bc062e819a8c6a99b642b47b65e012250ad44a53edf722'
PLUGIN_ENV = {
    'ANTIGRAVITY_PRIORITY_ENABLED': 'true',
    'ANTIGRAVITY_PRIORITY_STATE_CACHE': '/app/data/antigravity-priority/cache.json',
    'ANTIGRAVITY_PRIORITY_IDENTITIES': '/app/data/antigravity-priority/identities.json',
}


def ensure(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    with pathlib.Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(args, data=None, timeout=300):
    result = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    ensure(result.returncode == 0, args[0] + ' operation failed (exit ' + str(result.returncode) + '); private output withheld')
    return result.stdout


def compose(directory, file, *args):
    return run(['docker', 'compose', '--project-directory', str(directory), '-f', str(file), *args])


def atomic(path, data):
    path = pathlib.Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.priority-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)


def candidate_compose(text, service, image):
    # Preserve references and private values verbatim instead of serializing
    # `compose config`, which expands passwords into the configuration.
    pattern = r'^  ' + re.escape(service) + r':[^\S\n]*\n(?P<body>.*?)(?=^  [A-Za-z0-9_.-]+:|^\S|\Z)'
    match = re.search(pattern, text, re.M | re.S)
    ensure(match is not None, 'Expected a two-space-indented Compose service; no file changed')
    body = match['body']
    body, count = re.subn(r'^    image:[^\n]*$', '    image: ' + image, body, flags=re.M)
    ensure(count == 1, 'Expected exactly one application image; no file changed')
    ensure(not re.search(r'^    build:', body, re.M), 'Application build configuration is unsupported; use an image-based deployment')
    env = re.search(r'^    environment:[^\S\n]*\n(?P<env>.*?)(?=^    [A-Za-z0-9_.-]+:|\Z)', body, re.M | re.S)
    if env:
        old = env['env']
        nonempty = [line for line in old.splitlines() if line.strip() and not line.lstrip().startswith('#')]
        is_list = not nonempty or nonempty[0].lstrip().startswith('- ')
        remaining = []
        for line in old.splitlines(keepends=True):
            if any(re.match(r'^\s*(?:-\s*)?' + key + r'(?:\s*=|\s*:|\s*$)', line) for key in PLUGIN_ENV):
                continue
            remaining.append(line)
        additions = ''.join(('      - ' + key + '=' + value + '\n') if is_list else ('      ' + key + ': "' + value + '"\n') for key, value in PLUGIN_ENV.items())
        body = body[:env.start('env')] + additions + ''.join(remaining) + body[env.end('env'):]
    else:
        body = '    environment:\n' + ''.join('      - ' + key + '=' + value + '\n' for key, value in PLUGIN_ENV.items()) + body
    return text[:match.start('body')] + body + text[match.end('body'):]


def check_version(output):
    versions = re.findall(r'(?<![0-9.])v?(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._+-]+)?)(?![0-9.])', output)
    accepted = {UPSTREAM, '0.2.16-agpriority.1.2.18', '0.2.16-agpriority.1.2.18.sync.1', VERSION}
    ensure(bool(versions) and versions[0] in accepted,
           'This release supports Sub2API ' + UPSTREAM + ' only. A different version needs a matching adapter release; no downgrade or service change was performed')


def inspect_one(container):
    return json.loads(run(['docker', 'inspect', container]))[0]


def inspect_stack(directory, file, service):
    config = json.loads(compose(directory, file, 'config', '--format', 'json'))
    ensure(service in config['services'], 'Application service is missing')
    ensure('build' not in config['services'][service], 'Use an image-based deployment; application build configuration is unsupported')
    ids = compose(directory, file, 'ps', '-q', service).decode().split()
    ensure(len(ids) == 1, 'Expected one running application container')
    current = inspect_one(ids[0])
    ensure(current['State']['Running'], 'Application must be running for version and data checks')
    ensure(current['Config'].get('Labels', {}).get('com.docker.compose.service') == service, 'Unexpected Compose service identity')
    version = run(['docker', 'exec', current['Id'], '/app/sub2api', '-version']).decode()
    check_version(version)
    image = json.loads(run(['docker', 'image', 'inspect', current['Image']]))[0]
    ensure(image['Architecture'] == 'amd64', 'Only linux/amd64 is published')
    mounted = [entry for entry in current['Mounts'] if entry['Destination'] == '/app/data' and entry.get('RW')]
    ensure(len(mounted) == 1, 'A writable persistent /app/data mount is required')
    data_root = pathlib.Path(mounted[0]['Source'])
    state = data_root / 'antigravity-priority'
    ensure(state.is_dir() and not state.is_symlink(), 'Local Priority state is missing. Restore the private state backup first; GitHub contains no accounts or learning data')
    ensure((state / 'cache.json').is_file(), 'Local Priority cache is missing')
    ensure((state / 'identities.json').is_file(), 'Priority identity mapping is missing; restore it with cache.json to retain imported learning history')
    ensure(isinstance(json.loads((state / 'cache.json').read_text()), dict) and isinstance(json.loads((state / 'identities.json').read_text()), dict), 'Invalid local Priority state or identity mapping')
    ensure(all(not entry.is_symlink() for entry in state.rglob('*')), 'State directory contains a symlink; review it before recovery')
    all_ids = compose(directory, file, 'ps', '-aq').decode().split()
    containers = json.loads(run(['docker', 'inspect', *all_ids]))
    databases = [item for item in containers if item['Id'] != current['Id'] and any(value.startswith('POSTGRES_USER=') for value in item['Config'].get('Env', []))]
    ensure(len(databases) == 1 and databases[0]['State']['Running'], 'One running PostgreSQL container in this Compose project is required for the backup')
    priority_mapping.validate_marker(state, priority_mapping.read_offset(run, databases[0]['Id']))
    return config, current, containers, databases[0], state


def backup(directory, file, current, containers, database, state):
    parent = directory / 'priority-restore-backups'
    parent.mkdir(mode=0o700, exist_ok=True)
    parent.chmod(0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    folder = parent / stamp
    folder.mkdir(mode=0o700)
    (folder / 'compose.before.yml').write_bytes(file.read_bytes())
    env = directory / '.env'
    if env.is_file():
        shutil.copyfile(env, folder / 'environment.private.backup')
    with tarfile.open(folder / 'priority-state.private.tar.gz', 'w:gz') as archive:
        archive.add(state, arcname='antigravity-priority')
    with tarfile.open(folder / 'priority-state.private.tar.gz', 'r:gz') as archive:
        for name in ['cache.json', 'identities.json']:
            stream = archive.extractfile('antigravity-priority/' + name)
            ensure(stream is not None and isinstance(json.load(stream), dict), 'Private state backup validation failed')
    dump = run(['docker', 'exec', database['Id'], 'sh', '-c', 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc'])
    (folder / 'database.private.dump').write_bytes(dump)
    run(['docker', 'exec', '-i', database['Id'], 'pg_restore', '--list'], dump)
    rollback_image = 'local/sub2api-priority:before-' + stamp.lower().replace('.', '-')
    run(['docker', 'tag', current['Image'], rollback_image])
    metadata = {'directory': str(directory), 'compose': str(file), 'rollback_image': rollback_image,
                'database_id': database['Id'], 'state': str(state), 'previous_offset': priority_mapping.read_offset(run, database['Id']),
                'containers': {item['Id']: item['Config']['Labels'].get('com.docker.compose.service') for item in containers if item['Id'] != current['Id']}}
    (folder / 'rollback.json').write_text(json.dumps(metadata), encoding='utf-8')
    shutil.copyfile(__file__, folder / 'restore.py')
    shutil.copyfile(pathlib.Path(__file__).with_name('priority_mapping.py'), folder / 'priority_mapping.py')
    for entry in folder.iterdir():
        entry.chmod(0o600)
    return folder, metadata


def wait_healthy(directory, file, service):
    started = time.monotonic()
    deadline = started + 300
    while time.monotonic() < deadline:
        ids = compose(directory, file, 'ps', '-q', service).decode().split()
        if len(ids) == 1:
            item = inspect_one(ids[0])
            if not item['State']['Running']:
                break
            health = item['Config'].get('Healthcheck') or {}
            if health and health.get('Test') != ['NONE']:
                budget = (health.get('StartPeriod', 0) + health.get('Interval', 30000000000) * max(2, health.get('Retries', 3)) + health.get('Timeout', 0)) / 1000000000 + 30
                deadline = started + min(600, max(60, budget))
                if item['State'].get('Health', {}).get('Status') == 'healthy':
                    return item
            else:
                try:
                    run(['docker', 'exec', item['Id'], 'wget', '-q', '-T', '5', '-O', '/dev/null', 'http://127.0.0.1:8080/health'])
                    return item
                except RuntimeError:
                    pass
        time.sleep(1)
    raise RuntimeError('Application did not become healthy within its health-check budget')


def verify_plugin(item):
    actual = run(['docker', 'exec', item['Id'], 'sha256sum', '/app/sub2api']).decode().split()[0]
    ensure(actual == BINARY_SHA256, 'Running program does not match the verified release')
    page = run(['docker', 'exec', item['Id'], 'wget', '-qO-', 'http://127.0.0.1:8080/api/v1/antigravity-priority-ui'])
    ensure(b'sub2apiPriorityMarkup' in page and b'data-tab="overview"' in page, 'Priority UI is missing')
    configured = dict(value.split('=', 1) for value in item['Config']['Env'] if '=' in value)
    ensure(all(configured.get(key) == value for key, value in PLUGIN_ENV.items()), 'Priority environment is not active')


def verify_dependencies(metadata):
    for container_id in metadata['containers']:
        item = inspect_one(container_id)
        ensure(item['Id'] == container_id, 'A dependency container was recreated')


def rollback(folder):
    folder = pathlib.Path(folder).resolve()
    metadata = json.loads((folder / 'rollback.json').read_text())
    directory, file = pathlib.Path(metadata['directory']), pathlib.Path(metadata['compose'])
    expected = (folder / 'compose.applied.yml').read_bytes()
    ensure(file.read_bytes() == expected, 'Compose has changed since recovery; refusing to overwrite later edits')
    run(['docker', 'image', 'inspect', metadata['rollback_image']])
    original = (folder / 'compose.before.yml').read_bytes()
    # The original image could have been a moving tag. The preserved local tag
    # restores its actual image rather than whichever image now owns that tag.
    raw = original.decode('utf-8')
    service = metadata['service']
    pattern = r'(^  ' + re.escape(service) + r':[^\n]*\n.*?^    image:)[^\n]*'
    restored, count = re.subn(pattern, lambda match: match[1] + ' ' + metadata['rollback_image'], raw, count=1, flags=re.M | re.S)
    ensure(count == 1, 'Original application image could not be restored')
    verify_dependencies(metadata)
    compose(directory, file, 'stop', '--timeout', '30', service)
    priority_mapping.migrate(run, metadata['database_id'], metadata['state'], metadata['previous_offset'])
    atomic(file, restored.encode())
    compose(directory, file, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', service)
    wait_healthy(directory, file, service)
    verify_dependencies(metadata)
    print(json.dumps({'rollback': 'passed', 'native_offset': metadata['previous_offset'], 'private_data_restored_from_old_dump': False}))


def interrupted_recovery_hint(directory, file):
    parent = directory / 'priority-restore-backups'
    if parent.is_dir():
        for folder in sorted(parent.iterdir(), reverse=True):
            try:
                if (folder / 'compose.applied.yml').read_bytes() == file.read_bytes() and (folder / 'rollback.json').is_file():
                    return '; interrupted recovery can be reverted with: sudo python3 ' + str(folder / 'restore.py') + ' --rollback ' + str(folder)
            except OSError:
                continue
    return ''


def install(args):
    directory = pathlib.Path(args.directory).resolve()
    file = directory / args.compose_file
    ensure(directory.is_dir() and file.is_file() and not file.is_symlink(), 'Deployment directory or Compose file is missing/unsupported')
    try:
        config, current, containers, database, state = inspect_stack(directory, file, args.service)
    except RuntimeError as error:
        raise RuntimeError(str(error) + interrupted_recovery_hint(directory, file)) from None
    print(json.dumps({'compatible_upstream': UPSTREAM, 'release': VERSION, 'local_state_present': True, 'cpa_required': False, 'mode': 'check' if args.check else 'restore'}))
    if args.check:
        return
    with (directory / '.priority-restore.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original = file.read_bytes()
        current_hash = run(['docker', 'exec', current['Id'], 'sha256sum', '/app/sub2api']).decode().split()[0]
        configured = dict(value.split('=', 1) for value in current['Config']['Env'] if '=' in value)
        compose_environment = config['services'][args.service].get('environment') or {}
        compose_ready = all(compose_environment.get(key) == value for key, value in PLUGIN_ENV.items())
        mapping_ready = priority_mapping.read_offset(run, database['Id']) == priority_mapping.OFFSET
        if not args.reinstall and mapping_ready and compose_ready and current_hash == BINARY_SHA256 and all(configured.get(key) == value for key, value in PLUGIN_ENV.items()):
            verify_plugin(current)
            print(json.dumps({'restore': 'already installed', 'service_changed': False}))
            return
        with tempfile.TemporaryDirectory(prefix='sub2api-priority-') as temporary:
            build = pathlib.Path(temporary)
            compressed = build / ASSET
            if args.binary_file:
                shutil.copyfile(args.binary_file, compressed)
            else:
                with urllib.request.urlopen(ASSET_URL, timeout=90) as response, compressed.open('wb') as output:
                    shutil.copyfileobj(response, output)
            ensure(digest(compressed) == ARCHIVE_SHA256, 'Downloaded binary archive checksum mismatch')
            with gzip.open(compressed, 'rb') as source, (build / 'sub2api').open('wb') as target:
                shutil.copyfileobj(source, target)
            ensure(digest(build / 'sub2api') == BINARY_SHA256, 'Decompressed program checksum mismatch')
            (build / 'sub2api').chmod(0o755)
            folder, metadata = backup(directory, file, current, containers, database, state)
            metadata['service'] = args.service
            (folder / 'rollback.json').write_text(json.dumps(metadata), encoding='utf-8')
            print(json.dumps({'rollback_command': 'sudo python3 ' + str(folder / 'restore.py') + ' --rollback ' + str(folder)}), flush=True)
            image = 'local/sub2api-priority:0.2.16-sync.2'
            (build / 'Dockerfile').write_text('FROM ' + metadata['rollback_image'] + '\nCOPY --chown=1000:1000 sub2api /app/sub2api\n')
            (build / '.dockerignore').write_text('*\n!Dockerfile\n!sub2api\n')
            run(['docker', 'build', '--network=none', '--pull=false', '-t', image, str(build)])
            candidate = candidate_compose(original.decode('utf-8'), args.service, image).encode('utf-8')
            candidate_file = folder / 'compose.applied.yml'
            candidate_file.write_bytes(candidate)
            compose(directory, candidate_file, 'config', '--quiet')
            candidate_config = json.loads(compose(directory, candidate_file, 'config', '--format', 'json'))
            old_other = {key: value for key, value in config['services'].items() if key != args.service}
            new_other = {key: value for key, value in candidate_config['services'].items() if key != args.service}
            ensure(old_other == new_other, 'Candidate unexpectedly changes another service')
            ensure(file.read_bytes() == original, 'Compose changed concurrently; no production file changed')
            atomic(file, candidate)
            try:
                compose(directory, file, 'stop', '--timeout', '30', args.service)
                mapping = priority_mapping.migrate(run, database['Id'], state)
                compose(directory, file, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', args.service)
                updated = wait_healthy(directory, file, args.service)
                verify_plugin(updated)
                verify_dependencies(metadata)
            except BaseException:
                rollback(folder)
                raise
            print(json.dumps({'restore': 'passed', 'version': VERSION, 'backup': str(folder), 'mapping': mapping,
                              'database_dump_verified': True, 'private_data_preserved': True, 'dependency_containers_preserved': True}))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', default='/opt/sub2api')
    parser.add_argument('--compose-file', default='docker-compose.yml')
    parser.add_argument('--service', default='sub2api')
    parser.add_argument('--check', action='store_true', help='Read-only version/dependency/state preflight')
    parser.add_argument('--reinstall', action='store_true', help='Reinstall even when the verified program is active')
    parser.add_argument('--binary-file', help='Use an already downloaded release archive; checksum still required')
    parser.add_argument('--rollback', help='Roll back program/configuration only; preserve current database/state')
    args = parser.parse_args()
    ensure(os.geteuid() == 0, 'Run with sudo/root so local private state can be backed up')
    ensure(platform.system() == 'Linux' and platform.machine() in ['x86_64', 'amd64'], 'Only Linux amd64 is supported')
    if args.rollback:
        rollback(args.rollback)
    else:
        install(args)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('RESTORE_FAILED: ' + (str(error) if isinstance(error, RuntimeError) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
