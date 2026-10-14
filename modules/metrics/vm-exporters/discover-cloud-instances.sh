#!/bin/bash
# SPDX-License-Identifier: UPL-1.0
# Discover running instances across clouds (AWS, Azure, GCP, OCI) and generate
# Prometheus scrape targets — the multicloud companion to discover-oci-instances.sh.
#
# For each running instance it resolves a private IP and OS family, then emits a
# scrape target on the right exporter port (Linux node_exporter :9100, Windows
# windows_exporter :9182) labelled with the source cloud. Run it from a host with
# the relevant cloud CLI configured (aws / az / gcloud / oci).
#
# Usage:
#   ./discover-cloud-instances.sh --cloud aws   [--region R]            [--output table|targets|config]
#   ./discover-cloud-instances.sh --cloud azure [--resource-group RG]   [--output ...]
#   ./discover-cloud-instances.sh --cloud gcp   --project P [--zone Z]  [--output ...]
#   ./discover-cloud-instances.sh --cloud oci   --compartment-id <OCID> [--profile P] [--output ...]
#   ./discover-cloud-instances.sh --cloud all   [cloud-specific opts...] [--output ...]
#
# Options:
#   --cloud aws|azure|gcp|oci|all   (required)
#   --region <R>        AWS region / OCI region override
#   --resource-group    Azure resource group (default: all)
#   --project <P>       GCP project (required for gcp)
#   --zone <Z>          GCP zone (default: all zones)
#   --compartment-id    OCI compartment OCID (for --cloud oci/all)
#   --profile <P>       OCI CLI profile (default DEFAULT)
#   --port <N>          Linux exporter port (default 9100; Windows always 9182)
#   --public            Use public IP instead of private (cross-cloud scraping)
#   --output table|targets|config   (default table)
#   --config-file       config.json to merge for --output config (default ./config.json)
#
# Output records carry a `cloud` label so one Prometheus/OCI-Monitoring view can
# split metrics by provider. discovered-targets.json contains private IPs and is
# git-ignored.

# Note: nounset (-u) is intentionally NOT set — empty optional arrays like
# "${zflag[@]}" trip "unbound variable" on macOS bash 3.2 when no --zone/--region
# is given. We guard our own variables explicitly instead.
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/target-discovery.sh
source "$SCRIPT_DIR/lib/target-discovery.sh"

CLOUD="" REGION="" RG="" PROJECT="" ZONE="" COMPARTMENT_ID="" PROFILE="${OCI_CLI_PROFILE:-DEFAULT}"
PORT="9100" WIN_PORT="9182" OUTPUT="table" CONFIG_FILE="./config.json" USE_PUBLIC="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cloud)           CLOUD="$2"; shift 2;;
    --region)          REGION="$2"; shift 2;;
    --resource-group)  RG="$2"; shift 2;;
    --project)         PROJECT="$2"; shift 2;;
    --zone)            ZONE="$2"; shift 2;;
    --compartment-id)  COMPARTMENT_ID="$2"; shift 2;;
    --profile)         PROFILE="$2"; shift 2;;
    --port)            PORT="$2"; shift 2;;
    --public)          USE_PUBLIC="true"; shift;;
    --output)          OUTPUT="$2"; shift 2;;
    --config-file)     CONFIG_FILE="$2"; shift 2;;
    -h|--help)         sed -n '2,38p' "$0"; exit 0;;
    *) echo "Unknown option: $1" >&2; exit 1;;
  esac
done
[[ -z "$CLOUD" ]] && { echo "Provide --cloud aws|azure|gcp|oci|all" >&2; exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
RECORDS="$TMP/records.tsv"   # cloud \t name \t os \t ip \t port
target_records_init "$RECORDS"

discover_aws() {
  command -v aws >/dev/null || { echo "aws CLI not found" >&2; return; }
  local rflag=(); [[ -n "$REGION" ]] && rflag=(--region "$REGION")
  aws ec2 describe-instances "${rflag[@]}" --filters "Name=instance-state-name,Values=running" \
    --query 'Reservations[].Instances[].{name: Tags[?Key==`Name`]|[0].Value, priv: PrivateIpAddress, pub: PublicIpAddress, plat: PlatformDetails}' \
    --output json 2>/dev/null | python3 -c "
import sys, json, os
pub = '$USE_PUBLIC' == 'true'
for i in json.load(sys.stdin) or []:
    ip = (i.get('pub') if pub else i.get('priv')) or ''
    if not ip: continue
    osf = 'windows' if 'Windows' in (i.get('plat') or '') else 'linux'
    print('\t'.join(['aws', i.get('name') or 'ec2', osf, ip]))
"
}

discover_azure() {
  command -v az >/dev/null || { echo "az CLI not found" >&2; return; }
  local gflag=(); [[ -n "$RG" ]] && gflag=(--resource-group "$RG")
  az vm list -d "${gflag[@]}" --query "[?powerState=='VM running'].{name:name, priv:privateIps, pub:publicIps, os:storageProfile.osDisk.osType}" \
    --output json 2>/dev/null | python3 -c "
import sys, json
pub = '$USE_PUBLIC' == 'true'
for i in json.load(sys.stdin) or []:
    ips = (i.get('pub') if pub else i.get('priv')) or ''
    ip = ips.split(',')[0].strip() if ips else ''
    if not ip: continue
    osf = 'windows' if (i.get('os') or '').lower().startswith('win') else 'linux'
    print('\t'.join(['azure', i.get('name') or 'vm', osf, ip]))
"
}

discover_gcp() {
  command -v gcloud >/dev/null || { echo "gcloud CLI not found" >&2; return; }
  [[ -z "$PROJECT" ]] && { echo "gcp needs --project" >&2; return; }
  local zflag=(); [[ -n "$ZONE" ]] && zflag=(--zones "$ZONE")
  gcloud compute instances list --project="$PROJECT" "${zflag[@]}" --filter="status=RUNNING" --format=json 2>/dev/null | python3 -c "
import sys, json
pub = '$USE_PUBLIC' == 'true'
for i in json.load(sys.stdin) or []:
    nic = (i.get('networkInterfaces') or [{}])[0]
    ip = ''
    if pub:
        ip = ((nic.get('accessConfigs') or [{}])[0].get('natIP')) or ''
    else:
        ip = nic.get('networkIP') or ''
    if not ip: continue
    lic = ' '.join(i.get('disks',[{}])[0].get('licenses',[])) if i.get('disks') else ''
    osf = 'windows' if 'windows' in lic.lower() else 'linux'
    print('\t'.join(['gcp', i.get('name') or 'gce', osf, ip]))
"
}

discover_oci() {
  [[ -z "$COMPARTMENT_ID" ]] && { echo "oci needs --compartment-id" >&2; return; }
  local args=(--compartment-id "$COMPARTMENT_ID" --profile "$PROFILE" --output targets --port "$PORT")
  [[ -n "$REGION" ]] && args+=(--region "$REGION")
  # Reuse the OCI discoverer, then re-tag its file_sd output as cloud=oci.
  ( cd "$SCRIPT_DIR" && ./discover-oci-instances.sh "${args[@]}" >/dev/null 2>&1 ) || true
  local f; f="$SCRIPT_DIR/discovered-targets.json"
  [[ -f "$f" ]] && python3 -c "
import json
for g in json.load(open('$f')):
    for t in g['targets']:
        ip,_,p = t.partition(':')
        print('\t'.join(['oci', g['labels'].get('instance','oci'), g['labels'].get('os','linux'), ip]))
" 2>/dev/null || true
}

run_cloud() {
  case "$1" in
    aws) discover_aws;; azure) discover_azure;; gcp) discover_gcp;; oci) discover_oci;;
    *) echo "Unknown cloud: $1" >&2;;
  esac
}

echo "Discovering ($CLOUD)..." >&2
if [[ "$CLOUD" == "all" ]]; then
  for c in aws azure gcp oci; do run_cloud "$c"; done
else
  run_cloud "$CLOUD"
fi > "$TMP/raw.tsv" 2>/dev/null || true
# attach the correct port per OS
while IFS=$'\t' read -r CL NAME OSF IP; do
  target_record_add "$RECORDS" "$CL" "$NAME" "$OSF" "$IP" "$PORT" "$WIN_PORT"
done < "$TMP/raw.tsv"

COUNT="$(target_records_count "$RECORDS")"
case "$OUTPUT" in
  table)
    target_render_table "$RECORDS" true
    echo "($COUNT target(s))" >&2;;
  targets)
    target_render_targets "$RECORDS" "discovered-targets.json" true
    echo "Wrote $COUNT target group(s) to discovered-targets.json (cloud-labelled)." >&2;;
  config)
    MERGE_RESULT="$(target_merge_config "$RECORDS" "$CONFIG_FILE")"
    NEW_COUNT="${MERGE_RESULT%%$'\t'*}"
    TOTAL_COUNT="${MERGE_RESULT##*$'\t'}"
    echo "Merged $NEW_COUNT target(s) into $CONFIG_FILE ($TOTAL_COUNT total)." >&2;;
  *) echo "Unknown --output '$OUTPUT'" >&2; exit 1;;
esac
