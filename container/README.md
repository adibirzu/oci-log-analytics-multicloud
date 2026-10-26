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

All three images are built for `linux/amd64` and `linux/arm64`.

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
binary and runs one cloud identity per process. Mount a metrics-pull config generated
by `collectors/otel/generate.py` for the selected cloud, then supply
`--cloud aws|azure|gcp --config <path>`. See
[Vault federation for metric pullers](../docs/DEPLOY.md#vault-federation-for-metric-pullers-mc-29-configured-provider-and-release-gates-open)
for identity inputs, assertion handling and acceptance limits.

The APM gateway uses the upstream collector image directly with
`collectors/otel/apm-gateway.yaml` mounted at `/etc/otelcol-contrib/config.yaml`.
See [collector configuration](../collectors/otel/README.md) for its runtime variables.

## Consuming the images from ORM

The ORM stack receives image references as plain strings
`${IMAGE_REGISTRY}/<image>:<TAG>`. Pin a release tag (not `latest`) in production.
