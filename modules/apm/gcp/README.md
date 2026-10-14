# APM instrumentation on GCP

Apps on GCP send OTLP to the gateway collector (see `../README.md`). Use the
gateway, not Cloud Trace or Cloud Monitoring, when traces must land in OCI APM.

## Target

```
OTEL_EXPORTER_OTLP_ENDPOINT=http://<GATEWAY_PRIVATE_URL>:4318
OTEL_SERVICE_NAME=<service-name>
OTEL_RESOURCE_ATTRIBUTES=cloud.provider=gcp,cloud.region=<GCP_REGION>
```

## Option A: OTel SDK with OTLP/HTTP exporter (recommended)

Works on Cloud Run, GKE and Compute Engine. Set the environment variables above
and use the SDK's OTLP/HTTP exporter. The Flask example in
`examples/apm/python-flask/` is this pattern.

- Cloud Run: the gateway URL must be reachable from the service. Verify the
  Direct VPC egress or Serverless VPC Access connector needed for a private
  gateway URL.
- Use ADC for any GCP API call the app makes. OTLP export to the gateway needs no
  GCP credentials.

## Option B: Ops Agent OTLP receiver (Compute Engine)

The Ops Agent can receive OTLP and forward it. Verify the current Ops Agent
OTLP receiver configuration and whether its exporter can target a custom
endpoint. If it only exports to Cloud Trace, do not use it for OCI APM; point
the app at the gateway directly (Option A).

## Option C: GKE sidecar or DaemonSet

Run the collector as a DaemonSet or sidecar that forwards to the gateway. Verify
the image and config against your collector version.

## Notes

- Keep the gateway private. Cloud Run ingress for the gateway should be
  `internal` or limited to the VPC; verify the setting for your organization.
- Data key: stored in Secret Manager and mounted into the gateway service as
  `OCI_APM_PRIVATE_DATAKEY`. Do not put it in app environment variables.
- Cloud Monitoring metric pulls are a separate path (`collectors/otel/metrics-pull`,
  `googlecloudmonitoringreceiver`).
- Verify: Ops Agent OTLP receiver options and the Cloud Run h2c requirement before
  choosing gRPC.
