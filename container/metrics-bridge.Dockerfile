# Metrics bridge image. Build from the repository root so the COPY paths resolve:
#   docker build -f container/metrics-bridge.Dockerfile -t <IMAGE>:<TAG> --build-arg SOURCE_URL=<REPO_URL> .
FROM python:3.11-slim

ARG SOURCE_URL=""

LABEL org.opencontainers.image.title="oci-la-mc-metrics-bridge" \
      org.opencontainers.image.description="OTLP metrics to OCI Monitoring bridge" \
      org.opencontainers.image.licenses="UPL-1.0" \
      org.opencontainers.image.source="${SOURCE_URL}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    # Inside the container the bridge must be reachable on the published port.
    # Outside containers the code default is loopback (127.0.0.1:4319).
    BRIDGE_LISTEN=0.0.0.0:4319

RUN pip install "oci==2.188.0" "opentelemetry-proto==1.45.1" "protobuf==7.36.2" \
    && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin bridge

WORKDIR /app
COPY collectors/__init__.py /app/collectors/__init__.py
COPY collectors/metrics_bridge /app/collectors/metrics_bridge

USER bridge
EXPOSE 4319

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; p = os.environ.get('BRIDGE_LISTEN', '127.0.0.1:4319').rsplit(':', 1)[1]; urllib.request.urlopen('http://127.0.0.1:' + p + '/healthz', timeout=2)"]

ENTRYPOINT ["python", "-m", "collectors.metrics_bridge"]
