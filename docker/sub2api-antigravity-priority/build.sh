#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export GOTOOLCHAIN=go1.27.2
GO=${GO:-go}
LINT=${GOLANGCI_LINT:-golangci-lint}
mkdir -p "$ROOT/build"
"$GO" version | grep -F 'go1.27.2 '
"$LINT" version | grep -F '2.12.2'
cd "$ROOT/engine"
GOTOOLCHAIN=go1.26.8 "$LINT" run ./...
"$GO" test -count=1 ./...
cd "$ROOT/sub2api/backend"
"$GO" run -mod=mod github.com/google/wire/cmd/wire ./cmd/server
"$GO" test -count=1 -p 2 ./internal/service -run 'Antigravity|Priority'
"$GO" test -count=1 -p 2 ./internal/handler/admin ./internal/server/routes ./internal/server/middleware -run 'Antigravity|Priority|SecurityHeaders'
cd "$ROOT/sub2api/frontend"
npx --yes pnpm@9.15.9 install --frozen-lockfile
npx --yes pnpm@9.15.9 build
cd "$ROOT/sub2api/backend"
CGO_ENABLED=0 "$GO" build -tags embed -trimpath \
  -ldflags "-s -w -X main.Version=0.2.16-agpriority.1.2.18.sync.3 -X main.Commit=01842576b3d5c1e2545b5a99ad555d36e28ed306+agpriority-3b4648cb -X main.Date=$(date -u +%Y-%m-%dT%H:%M:%SZ) -X main.BuildType=release" \
  -o "$ROOT/build/sub2api" ./cmd/server
sha256sum "$ROOT/build/sub2api" > "$ROOT/build/sub2api.sha256"
printf '%s\n' 'PASS: lint, engine suite, adapter/gateway regressions, frontend typecheck/build, embedded binary'
