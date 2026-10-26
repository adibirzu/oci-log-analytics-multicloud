# Container images

Three images are built from this repository. All run as a non-root user (uid 10001)
and carry no credentials. Cloud access comes from the runtime identity (instance,
workload or OIDC federation) and settings come from the environment.

| Image | Dockerfile | Entrypoint | Port |
|-------|------------|------------|------|
| `${IMAGE_REGISTRY}/oci-la-mc-logs-collector` | `container/logs-collector.Dockerfile` | `python -m collectors.logs` (default `--source aws`) | none |
| `${IMAGE_REGISTRY}/oci-la-mc-metrics-bridge` | `container/metrics-bridge.Dockerfile` | `python -m collectors.metrics_bridge` | 4319 (OTLP/HTTP `/v1/metrics`, health `/healthz`) |
| `${IMAGE_REGISTRY}/oci-la-mc-otel-federation` | `container/otel-federation.Dockerfile` | `python -m collectors.otel.bootstrap` | none |

`IMAGE_REGISTRY` is the registry prefix, for example `ghcr.io/<OWNER>`.

## Tags

- `v*` git tag: `<IMAGE>:<TAG>` and `<IMAGE>:latest`.
- `workflow_dispatch` run: `<IMAGE>:sha-<SHORT_SHA>` only.

Both images are built for `linux/amd64` and `linux/arm64`.

## Build locally

Build from the repository root so the `COPY` paths resolve. Use a remote Linux
builder for amd64 when the workstation is ARM.

```bash
docker build -f container/logs-collector.Dockerfile \
  --build-arg SOURCE_URL=<REPO_URL> -t ${IMAGE_REGISTRY}/oci-la-mc-logs-collector:<TAG> .
docker build -f container/metrics-bridge.Dockerfile \
  --build-arg SOURCE_URL=<REPO_URL> -t ${IMAGE_REGISTRY}/oci-la-mc-metrics-bridge:<TAG> .
docker build -f container/otel-federation.Dockerfile \
  --build-arg SOURCE_URL=<REPO_URL> -t ${IMAGE_REGISTRY}/oci-la-mc-otel-federation:<TAG> .
```

## Release workflow

`.github/workflows/release.yml` runs on `v*` tags and on manual dispatch:

1. `test`: `pytest` on Python 3.11.
2. `images`: buildx and QEMU multi-arch build, pushed to GHCR with `GITHUB_TOKEN`.
3. `release`: `scripts/build_orm_zip.sh` writes `dist/oci-log-analytics-multicloud-orm.zip`.
   On a tag, the zip is attached to the GitHub release.

Every action is pinned to a full commit SHA with the tag in a trailing comment.

## Collector configs (OpenTelemetry Collector)

The OTel federation image carries the upstream `otel/opentelemetry-collector-contrib:0.162.0`
binary and runs one cloud identity per process. Supply `--cloud aws|azure|gcp --config <path>`. When
`MC_FEDERATION_SECRET_OCID` is present, it materializes its short-lived Vault assertion only
for that process; the GCP launcher also writes a mode-0600 external-account file. The ORM stack configures this image in isolated per-cloud Container Instances; image
publication and provider validation remain open MC-29 gates. Mount the configs from `collectors/otel/` into the container:

- `collectors/otel/apm-gateway.yaml` at `/etc/otelcol-contrib/config.yaml`.
- The generated metrics-pull config (`collectors/otel/generate.py` output) at the same path
  for the pull pipeline.

Secret-like values are `${env:NAME}` references and are supplied as environment
variables at runtime, never inline.

## Consuming the images from ORM

The ORM stack receives image references as plain strings
`${IMAGE_REGISTRY}/<image>:<TAG>`. Pin a release tag (not `latest`) in production.
