#!/usr/bin/env bash
set -euo pipefail
command -v curl >/dev/null
command -v sha256sum >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ is required"'
workdir=$(mktemp -d)
trap 'rm -rf -- "$workdir"' EXIT
url=https://raw.githubusercontent.com/qingzhh/public-resources/sub2api-priority-v0.2.16-sync.1/docker/sub2api-antigravity-priority/restore.py
curl --fail --silent --show-error --location --retry 2 --connect-timeout 15 "$url" -o "$workdir/restore.py"
printf '%s  %s\n' a08db7541a515450e2c4aba4f5db22dc672a5b85133b49d98a005e9058c46fdf "$workdir/restore.py" | sha256sum --check --status
python3 -B "$workdir/restore.py" "$@"
