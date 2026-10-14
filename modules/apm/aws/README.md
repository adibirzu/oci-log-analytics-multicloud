# APM instrumentation on AWS

Apps on AWS send OTLP to the gateway collector (see `../README.md`). They do not
talk to OCI APM directly.

## Target

```
OTEL_EXPORTER_OTLP_ENDPOINT=http://<GATEWAY_PRIVATE_DNS>:4318
OTEL_SERVICE_NAME=<service-name>
OTEL_RESOURCE_ATTRIBUTES=cloud.provider=aws,cloud.region=<AWS_REGION>
```

No OCI keys and no AWS keys are needed for export. The app sends to the gateway
over the private network.

## Option A: ADOT collector as a sidecar or agent (ECS/EC2/EKS)

1. Run the AWS Distro for OpenTelemetry (ADOT) collector next to the app, or use
   the app's OTel SDK directly (Option B).
2. Configure the sidecar to forward OTLP to the gateway with an `otlphttp`
   exporter pointing at `http://<GATEWAY_PRIVATE_DNS>:4318`. Do not add OCI
   exporters here; OCI APM is reached only from the gateway.
3. Verify: the ADOT image tag and its contrib version against the gateway's
   collector version. Verify the ADOT configuration key names for your release.

## Option B: Lambda layer (ADOT)

1. Add the ADOT Lambda layer for your runtime (verify the current layer ARN for
   your region and runtime; do not hard-code a version you have not checked).
2. Set:
   - `OTEL_EXPORTER_OTLP_ENDPOINT=http://<GATEWAY_PRIVATE_DNS>:4318`
   - `AWS_LAMBDA_EXEC_WRAPPER=/opt/otel-instrument`
   - `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` as above.
3. Lambda must reach the gateway privately: attach the function to the VPC subnets
   that route to the gateway. Verify VPC configuration and cold-start impact.

## Option C: OTel SDK in the app

Use the language SDK with the OTLP/HTTP exporter. The example in
`examples/apm/python-flask/` shows the pattern. Environment variables as above.

## Notes

- Keep the gateway private. Security groups allow 4317/4318 only from app
  security groups.
- CloudWatch metrics are a separate path: `collectors/otel/metrics-pull` and
  `collectors/metrics_bridge`. Do not duplicate them as APM metrics unless you
  need both.
- Verify: the exact ADOT layer ARN and the version compatibility matrix before
  rollout.
