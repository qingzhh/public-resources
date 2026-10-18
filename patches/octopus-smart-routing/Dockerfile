# Build only from an immutable public upstream plus the reviewed source overlay.
FROM alpine:3.22 AS source
WORKDIR /src
ADD https://github.com/Hureru/octopus/archive/0e1a7ee062c8a24a9c8b95e7d1778f265182a304.tar.gz /tmp/upstream.tar.gz
RUN tar -xzf /tmp/upstream.tar.gz --strip-components=1 -C /src && rm /tmp/upstream.tar.gz

FROM node:22-alpine AS frontend
WORKDIR /src/web
COPY --from=source /src/web/ ./
COPY overlay/web/ ./
ENV NEXT_TELEMETRY_DISABLED=1
RUN corepack enable && corepack prepare pnpm@10.15.1 --activate && pnpm install --frozen-lockfile && pnpm run build

FROM golang:1.25-alpine AS backend
WORKDIR /src
RUN apk add --no-cache git
COPY --from=source /src/ ./
COPY overlay/internal/ ./internal/
COPY --from=frontend /src/web/out/ ./static/out/
RUN CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /out/octopus .

FROM alpine:3.22 AS runtime
ENV TZ=Asia/Shanghai
WORKDIR /app
RUN apk add --no-cache ca-certificates tzdata su-exec
COPY --from=backend /out/octopus /app/octopus
COPY --from=backend /src/scripts/dockerfiles/entrypoint.sh /entrypoint.sh
COPY --from=backend /src/LICENSE /licenses/octopus-LICENSE
RUN chmod 0755 /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
