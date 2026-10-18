# Rebuild the pinned public source and apply the checksum-verified bundle.
FROM python:3.11-alpine AS patched
WORKDIR /upstream
ADD https://github.com/ardeyouxipianyi/workbuddy2api-hub/archive/906ca6a097e3e002415db53ea84c9b89c1589bed.tar.gz /tmp/upstream.tar.gz
RUN tar -xzf /tmp/upstream.tar.gz --strip-components=1 -C /upstream && rm /tmp/upstream.tar.gz
COPY . /bundle/
RUN python -B /bundle/apply.py --target /upstream --dry-run && python -B /bundle/apply.py --target /upstream

FROM python:3.11-alpine AS runtime
ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8788 API_KEY= TZ=Asia/Shanghai
WORKDIR /app
RUN apk add --no-cache tzdata ca-certificates && mkdir -p /app/accounts /app/usage
COPY --from=patched /upstream/wb_*.py /upstream/dashboard.html ./
COPY THIRD-PARTY-zh.md /licenses/THIRD-PARTY-zh.md
VOLUME ["/app/accounts", "/app/usage"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8788'), timeout=3)" || exit 1
CMD ["python", "wb_proxy.py", "--host", "0.0.0.0", "--lan"]
