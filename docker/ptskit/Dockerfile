FROM python:3.11-slim AS base
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai
COPY seedkeep_*.py torrent_meta.py ./
COPY web/ ./web/
EXPOSE 8786

FROM base AS test
COPY test_seedkeep*.py run_tests.py ./
CMD ["python", "run_tests.py"]

FROM base AS runtime
LABEL org.opencontainers.image.title="PTSkit" \
      org.opencontainers.image.source="https://github.com/qingzhh/public-resources"
CMD ["python", "seedkeep_web.py"]
