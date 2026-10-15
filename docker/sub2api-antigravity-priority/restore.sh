#!/usr/bin/env bash
set -euo pipefail
command -v curl >/dev/null
command -v sha256sum >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ is required"'
workdir=$(mktemp -d)
trap 'rm -rf -- "$workdir"' EXIT
base=https://raw.githubusercontent.com/qingzhh/public-resources/sub2api-priority-v0.2.16-sync.2/docker/sub2api-antigravity-priority
curl --fail --silent --show-error --location --retry 2 --connect-timeout 15 --max-time 120 "$base/restore.py" -o "$workdir/restore.py"
curl --fail --silent --show-error --location --retry 2 --connect-timeout 15 --max-time 120 "$base/priority_mapping.py" -o "$workdir/priority_mapping.py"
printf '%s  %s\n' c430651fcdd2049b88c5b32ed719fbee6377cc70d8f1fb1d3b8291046dba7a00 "$workdir/restore.py" 5def7a95cbff7a38d306246e462c2e10dd6fb5c36b5af3d9bf67171d61638d2a "$workdir/priority_mapping.py" | sha256sum --check --status
python3 -B "$workdir/restore.py" "$@"
