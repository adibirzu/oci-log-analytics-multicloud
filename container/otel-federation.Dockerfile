# Vault-aware OTel launcher.  Build from repository root.
FROM otel/opentelemetry-collector-contrib:0.162.0 AS otel
FROM python:3.11-slim

ARG SOURCE_URL=""
LABEL org.opencontainers.image.title="oci-la-mc-otel-federation" \
      org.opencontainers.image.description="Vault-aware per-cloud OpenTelemetry Collector launcher" \
      org.opencontainers.image.licenses="UPL-1.0" \
      org.opencontainers.image.source="${SOURCE_URL}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

RUN pip install "oci==2.188.0" \
    && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin collector

WORKDIR /app
COPY --from=otel /otelcol-contrib /usr/local/bin/otelcol-contrib
COPY collectors/__init__.py /app/collectors/__init__.py
COPY collectors/common /app/collectors/common
COPY collectors/otel /app/collectors/otel

USER collector
ENTRYPOINT ["python", "-m", "collectors.otel.bootstrap"]
