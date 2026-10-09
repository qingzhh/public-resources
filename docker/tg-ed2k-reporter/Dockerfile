FROM python:3.11-slim@sha256:0dd364ba7e10242f07755449e3a3d0e35f9efd987952737b90def6709ab0c5ce
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=65532:65532 ed2k.py state.py collector.py reporter.py plugin_reporter.py ms_plugin_client.py app.py dashboard.py test_pipeline.py test_reporter.py test_dashboard.py test_plugin_reporter.py test_ms_plugin_client.py ./
COPY --chown=65532:65532 web ./web
EXPOSE 8890
USER 65532:65532
ENTRYPOINT ["python", "/app/app.py"]
CMD ["run"]
