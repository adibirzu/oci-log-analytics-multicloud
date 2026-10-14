# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- `lib/target-discovery.sh` — shared target discovery module for normalized target
  records and output rendering, plus a Bash test harness.
- **`discover-cloud-instances.sh`** — multicloud companion to the OCI discoverer;
  enumerates running instances in AWS, Azure, GCP, or OCI (or `all`) and emits
  cloud-labelled Prometheus targets (table / file_sd / config.json merge).
- **Unified multicloud showcase** — `external_labels: {cloud: …}` per VM so all
  clouds land in **one** OCI Monitoring namespace (`prometheus_multicloud`) and
  **one** Grafana, split by the `cloud` dimension. New
  `otel-destination/dashboards/multicloud.json`.
  Validated with one VM each in GCP/Azure/AWS reporting simultaneously.
- **Cross-cloud OCI Monitoring** via `install-oci-agent-linux.sh` — installs the OCI
  Management Agent on any Linux host (GCP/Azure/AWS/on-prem), so non-OCI instances can
  push to OCI Monitoring through a Prometheus `/federate` data source. Installs
  OpenJDK 8, sets `JAVA_HOME`, and waits out first-boot apt locks. Validated
  end-to-end on real VMs in **GCP** (`europe-west1`), **Azure** (`westeurope`), and
  **AWS** (`eu-central-1`) — each feeding **both** OCI Monitoring (its own namespace)
  and a 3rd-party Grafana/Prometheus sink.
- README "Cross-cloud" section + mermaid diagram.
- Disposable end-to-end validation notes for the OTEL data path.
- Fresh 2026-06-25 full disposable deployment validation notes: source
  `node_exporter` through `/federate` and OTEL Collector into the checked-in
  destination Prometheus/Grafana stack.
- TS-24 (Linux agent needs JDK 8 + `JAVA_HOME`), TS-25 (empty `list-data-sources`),
  TS-26 (cross-cloud agent → OCI Monitoring checklist), TS-27 (first-boot apt lock),
  TS-28 (no-SSH → drive via `az vm run-command` / `aws ssm send-command`).
- `discover-oci-instances.sh` — enumerate RUNNING OCI compute instances in a
  compartment (or whole tenancy subtree) and generate Prometheus scrape targets,
  with per-OS exporter ports (Linux 9100 / Windows 9182). Outputs a human table,
  a `discovered-targets.json` (Prometheus `file_sd_config`), or a non-destructive
  merge into `config.json` `TargetNodes`.
- `docs/KNOWLEDGE_BASE.md` — searchable troubleshooting KB (23 entries) derived
  from the end-to-end validation.
- Public-repo scaffolding: `LICENSE` (UPL-1.0), `CONTRIBUTING.md`, `SECURITY.md`,
  `CODE_OF_CONDUCT.md`, GitHub issue/PR templates, and a `lint.yml` CI workflow
  (shellcheck + PSScriptAnalyzer + `docker compose config`).

### Changed
- `discover-oci-instances.sh` and `discover-cloud-instances.sh` now keep their
  public command interfaces while delegating target rendering and config merging
  to the shared target discovery module.
- **OCI Monitoring is now optional.** New `OciMonitoringEnabled` config flag. The
  proxy can run as a pure OpenTelemetry / `remote_write` exporter to a non-OCI
  backend; the Management Agent zip/response-file prompts and install only happen
  when OCI Monitoring is enabled. The final summary reflects the active export
  paths, and the script warns if no export path is enabled.
- `otel-destination/docker-compose.yml` — Grafana admin password is now
  env-overridable (`GF_SECURITY_ADMIN_PASSWORD`).
- Hardened `.gitignore` (secrets, `.env*`, `*.pem`, `*.key`, `*.rsp`,
  `discovered-targets.json`).
- Removed a developer-specific local path from `PROJECT_REVIEW.md`.
- Removed the tracked NSSM `.exe`; `Install-OCI-Prometheus.ps1` now downloads NSSM
  from a public GitHub release first, with `nssm.cc` and Internet Archive fallbacks.
- Updated OTEL Collector configs to use the current `prometheus_remote_write`
  exporter key instead of the deprecated alias.
- Ignored `.e2e-tmp/` local scratch directories created during disposable
  validation runs.
- Quieted the reference Grafana stack by pinning Grafana OSS `11.5.0`, disabling
  update checks, setting error-level logging, and adding valid empty provisioning
  files for alerting/plugins.

### Fixed
- `manage-oci-datasource.sh` no longer crashes with a JSON parse error when an agent
  has zero data sources (the CLI returns empty output) — TS-25.

## [0.1.0] — 2026-06-17

### Added
- Initial validated suite: Windows/Linux exporter installers, Windows Prometheus
  proxy, OCI Management Agent integration, optional GCP `stackdriver_exporter`,
  OpenTelemetry export path, `manage-oci-datasource.sh` lifecycle helper, and the
  `otel-destination/` test sink.
- `PROJECT_REVIEW.md` — end-to-end test report on real OCI infrastructure with 13
  documented bug fixes.
