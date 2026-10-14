# Domain Context

## Terms

- **Target discovery** - finds running cloud instances, normalizes their OS,
  chosen IP, exporter port, labels, and emits proxy-ready targets.
- **Target record** - normalized discovery row with cloud, instance name, OS
  family, IP, and exporter port.
- **Prometheus proxy** - aggregation host that scrapes exporter targets and
  exposes `/federate` for OCI Monitoring and OTEL export paths.
- **Export path** - destination route for proxy metrics: OCI Monitoring through
  the Management Agent, OpenTelemetry, Prometheus `remote_write`, or both.
