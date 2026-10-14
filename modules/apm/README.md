# APM onboarding (traces and metrics to OCI APM)

Workloads on AWS, Azure and GCP send OpenTelemetry traces (and metrics) to a
**gateway collector** running in each cloud. The gateway forwards them to OCI APM.
Apps never hold the OCI APM data key.

```
 app (AWS / Azure / GCP)
   │  OTLP gRPC :4317 or OTLP/HTTP :4318   (private network only)
   ▼
 gateway collector, one per cloud         collectors/otel/apm-gateway.yaml
   • memory_limiter → health-span drop → probabilistic sampler → batch
   │  OTLP/HTTP, Authorization: dataKey ${env:OCI_APM_PRIVATE_DATAKEY}
   ▼
 OCI APM  (<OCI_APM_ENDPOINT> from the APM domain)
```

The gateway config is owned by `collectors/otel/apm-gateway.yaml`. This directory
documents how to run it and how each cloud's apps point at it.

## Where to run the gateway

| Cloud | Recommended runtime | Notes |
|---|---|---|
| AWS | ECS/Fargate task (or EC2) running the contrib collector image, in the same VPC as the apps | Apps reach it by private DNS name. Verify that the load balancer in front (if any) passes gRPC (HTTP/2); HTTP/4318 is the simpler path. |
| Azure | Azure Container Apps with internal ingress, same VNet as the apps | Verify the target port mapping for 4317 vs 4318 when enabling both. |
| GCP | Cloud Run (HTTP on 4318) or GKE | Cloud Run serves one port per service; use OTLP/HTTP there. Verify h2c before using gRPC. |

The gateway must not be exposed to the internet. Restrict ingress to the
application subnets or security groups.

## Data key handling

- The OCI APM private data key is stored in the cloud's secret store:
  AWS Secrets Manager (or SSM Parameter Store, SecureString), Azure Key Vault,
  GCP Secret Manager.
- The gateway runtime reads it at start and exposes it as the environment variable
  `OCI_APM_PRIVATE_DATAKEY`. The collector config references it as
  `${env:OCI_APM_PRIVATE_DATAKEY}`.
- The key never goes into Terraform state (inject it through the runtime's secret
  reference, not a `sensitive` TF variable), argv, container images, or logs.
- Rotate by creating a new APM key, updating the secret, and restarting the gateway.
- `OCI_APM_ENDPOINT` is not secret; it is the APM domain's data upload endpoint.

## Sampling and noise knobs

| Knob | Where | Default |
|---|---|---|
| `APM_SAMPLING_PERCENT` | gateway `probabilistic_sampler` | 10 |
| Health-check span drop (`/health`, `/healthz`, `/ready`, `/live`) | gateway `filter/health` | on |
| `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | app SDK (head sampling before export) | SDK default |
| `OTEL_METRIC_EXPORT_INTERVAL_MS` | app SDK metric export period | 60000 |

Sample in one place when possible (the gateway). App-side sampling reduces
egress from the app, so use it only for very chatty services. Metrics are not
sampled by the probabilistic sampler.

## Endpoint facts

Verified (official docs, 2026-10-14; see `docs/ARCHITECTURE.md`):
- OCI APM accepts OTLP spans and metrics at
  `<dataUploadEndpoint>/20200101/observations/{private-span|public-span|metric}?dataFormat={otlp-span|otlp-metric}&dataFormatVersion=1`
  with a data key.
- Verified in practice: `Authorization: dataKey <key>` on
  `<dataUploadEndpoint>/20200101/opentelemetry/private/v1/traces`.
- `/observations/public-span?dataFormat=otlp` is the REST API, not the OTLP protocol endpoint.

Unverified (verify before relying on them live):
- The metrics OTLP path `/20200101/opentelemetry/v1/metrics` used in
  `apm-gateway.yaml`, and whether the metric path needs a private or public key.
- Whether a protobuf or JSON body is required for the `metric` observation.

## Per-cloud app instrumentation

- [AWS](aws/README.md): ADOT collector or Lambda layer, OTLP to the gateway.
- [Azure](azure/README.md): Azure Monitor OpenTelemetry distro or plain OTel SDK, OTLP to the gateway.
- [GCP](gcp/README.md): OTel SDK, OTLP to the gateway (Ops Agent OTLP receiver as an alternative).

Runnable sample: `examples/apm/python-flask/` (its README covers the environment
variables). The offline test is `tests/test_apm_examples.py`:

```bash
.venv/bin/python -m pytest tests/test_apm_examples.py -q
```
