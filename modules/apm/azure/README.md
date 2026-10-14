# APM instrumentation on Azure

Apps on Azure send OTLP to the gateway collector (see `../README.md`). Azure's own
telemetry stays where it is; only application traces and metrics go to OCI APM
through the gateway.

## Target

```
OTEL_EXPORTER_OTLP_ENDPOINT=http://<GATEWAY_INTERNAL_FQDN>:4318
OTEL_SERVICE_NAME=<service-name>
OTEL_RESOURCE_ATTRIBUTES=cloud.provider=azure,cloud.region=<AZURE_REGION>
```

## Option A: Azure Monitor OpenTelemetry distro

The distro (for example `azure-monitor-opentelemetry` for Python, or the
equivalent for .NET, Java and Node) sends to Application Insights by default.
To send to the gateway instead:

- Verify whether the distro's exporter can be pointed at a custom OTLP endpoint
  for your language and version. If it cannot, use Option B for that service.
- Do not configure an Application Insights connection string for services that
  must only reach OCI APM; otherwise the data goes to two places.

## Option B: Plain OTel SDK with OTLP exporter

Use the OpenTelemetry SDK for your language with the OTLP/HTTP exporter. Set the
environment variables above. The Flask example in `examples/apm/python-flask/`
uses this path.

## Option C: Azure Container Apps or AKS sidecar

If you run the collector as a sidecar, point the app at `localhost:4318` and the
sidecar forwards to the gateway. Verify the sidecar image and config keys against
your collector version.

## Notes

- The gateway runs in Container Apps (internal ingress) in the same VNet. Keep the
  gateway off public ingress; verify the private DNS zone link for the Container
  Apps environment.
- Managed identity is not needed for OTLP export. The data key is delivered to
  the gateway from Key Vault; see `../README.md`.
- Event Hubs-based log and metric paths are separate
  (`collectors/logs`, `collectors/otel/metrics-pull`). Do not add them here.
- Verify: the distro's current custom-endpoint options for each language before
  choosing Option A.
