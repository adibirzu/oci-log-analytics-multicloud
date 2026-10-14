# Metrics-pull collector (starter allowlist; edit or regenerate with
# python -m collectors.otel.generate). Identity: SDK/workload federation, no static keys.
receivers:
%{ if aws_enabled ~}
  awscloudwatch/${aws_region}:
    region: ${aws_region}
    metrics:
      collection_interval: 300s
      period: 300s
      queries:
        - namespace: AWS/EC2
          metric_name: CPUUtilization
          stats: Average
%{ endif ~}
%{ if azure_enabled ~}
  azuremonitor:
    subscription_ids: ["$${env:AZURE_SUBSCRIPTION_ID}"]
    auth:
      authenticator: azureauth
    collection_interval: 300s
    services: ["microsoft.compute/virtualmachines"]
    metrics:
      microsoft.compute/virtualmachines:
        Percentage CPU: []
%{ endif ~}
%{ if gcp_enabled ~}
  googlecloudmonitoring:
    project_id: "$${env:GCP_PROJECT_ID}"
    collection_interval: 300s
    metrics_list:
      - metric_name: compute.googleapis.com/instance/cpu/utilization
%{ endif ~}
%{ if azure_enabled ~}
extensions:
  azureauth:
    workload_identity:
      tenant_id: "$${env:AZURE_TENANT_ID}"
      client_id: "$${env:AZURE_CLIENT_ID}"
      federated_token_file: /var/run/secrets/azure/tokens/azure-identity-token
%{ endif ~}
processors:
  memory_limiter:
    check_interval: 1s
    limit_percentage: 80
    spike_limit_percentage: 20
%{ if aws_enabled ~}
  resource/aws:
    attributes:
      - {key: cloud.provider, value: aws, action: upsert}
%{ endif ~}
%{ if azure_enabled ~}
  resource/azure:
    attributes:
      - {key: cloud.provider, value: azure, action: upsert}
%{ endif ~}
%{ if gcp_enabled ~}
  resource/gcp:
    attributes:
      - {key: cloud.provider, value: gcp, action: upsert}
%{ endif ~}
  batch: {}
exporters:
%{ if sink != "apm" ~}
  otlp_http/bridge:
    endpoint: $${env:METRICS_BRIDGE_ENDPOINT}
%{ endif ~}
%{ if sink != "monitoring" ~}
  otlp_http/apm:
    metrics_endpoint: $${env:OCI_APM_ENDPOINT}/20200101/opentelemetry/v1/metrics
    headers:
      Authorization: "dataKey $${env:OCI_APM_PRIVATE_DATAKEY}"
%{ endif ~}
service:
%{ if azure_enabled ~}
  extensions: [azureauth]
%{ endif ~}
  pipelines:
%{ for cloud, receiver in receivers ~}
    metrics/${cloud}:
      receivers: [${receiver}]
      processors: [memory_limiter, resource/${cloud}, batch]
      exporters: [${exporters}]
%{ endfor ~}
