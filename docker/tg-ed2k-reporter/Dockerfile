FROM python:3.11-slim@sha256:0dd364ba7e10242f07755449e3a3d0e35f9efd987952737b90def6709ab0c5ce
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai
COPY ed2k.py state.py collector.py reporter.py app.py test_pipeline.py test_reporter.py ./
USER 65532:65532
ENTRYPOINT ["python", "/app/app.py"]
CMD ["run"]
