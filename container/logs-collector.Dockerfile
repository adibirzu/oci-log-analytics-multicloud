# Logs collector image (AWS, Azure and GCP queue adapters).
# Build from the repository root so the COPY paths resolve:
#   docker build -f container/logs-collector.Dockerfile -t <IMAGE>:<TAG> --build-arg SOURCE_URL=<REPO_URL> .
# No credentials are baked in. Cloud identity comes from the runtime
# (instance/workload identity or OIDC federation) and settings from the environment.
FROM python:3.11-slim

ARG SOURCE_URL=""

LABEL org.opencontainers.image.title="oci-la-mc-logs-collector" \
      org.opencontainers.image.description="Collects AWS, Azure and GCP logs into OCI Log Analytics" \
      org.opencontainers.image.licenses="UPL-1.0" \
      org.opencontainers.image.source="${SOURCE_URL}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin collector

WORKDIR /app

# Install the runtime dependencies declared in pyproject.toml (base + aws, azure, gcp extras)
# without packaging the project itself. Only pyproject.toml is copied at this step so the
# dependency layer is cached independently of source changes.
COPY pyproject.toml /app/pyproject.toml
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; o = p['optional-dependencies']; print('\n'.join(p['dependencies'] + o['aws'] + o['azure'] + o['gcp']))" > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

COPY collectors /app/collectors
COPY content /app/content

USER collector

ENTRYPOINT ["python", "-m", "collectors.logs"]
CMD ["--source", "aws"]
