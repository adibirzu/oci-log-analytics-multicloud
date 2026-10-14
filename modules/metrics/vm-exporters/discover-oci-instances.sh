#!/bin/bash
# SPDX-License-Identifier: UPL-1.0
# Discover RUNNING OCI compute instances and generate Prometheus scrape targets.
#
# For each running instance it resolves the primary VNIC private IP and the OS
# family (from the instance's image), then emits scrape targets with the right
# exporter port per OS (Linux -> node_exporter :9100, Windows -> windows_exporter
# :9182). Run it from any host with the OCI CLI configured; feed the output to the
# Windows proxy's config.json so users never hand-author target IPs.
#
# Subcommands are expressed via --output:
#   table   (default) human-readable list: NAME  OS  IP:PORT  STATE
#   targets            write discovered-targets.json (Prometheus file_sd_config)
#   config             merge IP:PORT into TargetNodes of a config.json (non-destructive)
#
# Usage:
#   ./discover-oci-instances.sh --compartment-id <OCID> [--region R] [--profile P]
#   ./discover-oci-instances.sh --tenancy-scan [--compartment-id <ROOT_OCID>] [--profile P] --output config
#   ./discover-oci-instances.sh --compartment-id <OCID> --output targets --port 9100
#
# Options:
#   --compartment-id <OCID>  Compartment to scan (required unless --tenancy-scan
#                            resolves the tenancy root from your OCI config).
#   --tenancy-scan           Walk the whole compartment subtree (root = --compartment-id
#                            if given, else the profile's tenancy OCID).
#   --region <R>             Override region (default: profile's region).
#   --profile <P>            OCI CLI profile (default: $OCI_CLI_PROFILE or DEFAULT).
#   --port <N>               Linux exporter port (default 9100). Windows is always 9182.
#   --output table|targets|config   (default table)
#   --config-file <path>     config.json to merge into for --output config (default ./config.json)
#
# Required IAM (read-only) for the calling principal/group, in each scanned compartment:
#   ALLOW group <G> to inspect instances        in compartment <C>
#   ALLOW group <G> to read   instance-images   in compartment <C>   (OS detection)
#   ALLOW group <G> to read   virtual-network-family in compartment <C>   (VNIC private IP)
#   ALLOW group <G> to inspect compartments     in tenancy            (only for --tenancy-scan)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/target-discovery.sh
source "$SCRIPT_DIR/lib/target-discovery.sh"

PROFILE="${OCI_CLI_PROFILE:-DEFAULT}"
COMPARTMENT_ID="" REGION="" PORT="9100" OUTPUT="table" CONFIG_FILE="./config.json"
TENANCY_SCAN="false"
WIN_PORT="9182"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --compartment-id) COMPARTMENT_ID="$2"; shift 2;;
    --tenancy-scan)   TENANCY_SCAN="true"; shift;;
    --region)         REGION="$2"; shift 2;;
    --profile)        PROFILE="$2"; shift 2;;
    --port)           PORT="$2"; shift 2;;
    --output)         OUTPUT="$2"; shift 2;;
    --config-file)    CONFIG_FILE="$2"; shift 2;;
    -h|--help)        sed -n '2,40p' "$0"; exit 0;;
    *) echo "Unknown option: $1" >&2; exit 1;;
  esac
done

oci_() {
  if [[ -n "$REGION" ]]; then oci --profile "$PROFILE" --region "$REGION" "$@"
  else oci --profile "$PROFILE" "$@"; fi
}

# Resolve tenancy OCID from the OCI config for the active profile (for --tenancy-scan
# without an explicit root). Mirrors the standard preflight pattern.
resolve_tenancy() {
  awk -v p="[$PROFILE]" '
    $0==p {inb=1; next}
    /^\[/ {inb=0}
    inb && /^tenancy[[:space:]]*=/ {sub(/^tenancy[[:space:]]*=[[:space:]]*/,""); print; exit}
  ' ~/.oci/config
}

if [[ "$TENANCY_SCAN" == "true" && -z "$COMPARTMENT_ID" ]]; then
  COMPARTMENT_ID="$(resolve_tenancy)"
  [[ -z "$COMPARTMENT_ID" ]] && { echo "Could not resolve tenancy OCID for profile '$PROFILE'; pass --compartment-id." >&2; exit 1; }
fi
[[ -z "$COMPARTMENT_ID" ]] && { echo "Provide --compartment-id (or --tenancy-scan)." >&2; exit 1; }

# Build the list of compartments to scan.
compartments_to_scan() {
  if [[ "$TENANCY_SCAN" == "true" ]]; then
    echo "$COMPARTMENT_ID"   # include the root itself
    oci_ iam compartment list --compartment-id "$COMPARTMENT_ID" \
         --compartment-id-in-subtree true --access-level ACCESSIBLE \
         --lifecycle-state ACTIVE --all 2>/dev/null \
      | python3 -c "import sys,json;[print(c['id']) for c in (json.load(sys.stdin).get('data') or [])]" 2>/dev/null || true
  else
    echo "$COMPARTMENT_ID"
  fi
}

# OS family from an image OCID, cached on disk (portable; no bash4 assoc arrays).
TMP_DISC="$(mktemp -d)"
trap 'rm -rf "$TMP_DISC"' EXIT
os_family_for_image() {
  local img="$1"
  [[ -z "$img" ]] && { echo "linux"; return; }
  local cache
  cache="$TMP_DISC/$(echo "$img" | tr -c 'a-zA-Z0-9' '_')"
  if [[ -f "$cache" ]]; then cat "$cache"; return; fi
  local os fam
  os="$(oci_ compute image get --image-id "$img" --query 'data."operating-system"' --raw-output 2>/dev/null || echo "")"
  case "$os" in
    *Windows*) fam="windows";;
    *)         fam="linux";;
  esac
  echo "$fam" > "$cache"; echo "$fam"
}

primary_private_ip() {
  oci_ compute instance list-vnics --instance-id "$1" \
       --query 'data[0]."private-ip"' --raw-output 2>/dev/null || echo ""
}

RECORDS="$TMP_DISC/records.tsv"   # name \t os \t ip \t port
target_records_init "$RECORDS"

echo "Scanning for RUNNING instances (profile=$PROFILE${REGION:+, region=$REGION})..." >&2
while read -r CID; do
  [[ -z "$CID" ]] && continue
  INSTANCES="$(oci_ compute instance list -c "$CID" --lifecycle-state RUNNING --all 2>/dev/null || echo '{"data":[]}')"
  while IFS=$'\t' read -r IID NAME IMG; do
    [[ -z "$IID" ]] && continue
    IP="$(primary_private_ip "$IID")"
    [[ -z "$IP" || "$IP" == "null" ]] && continue
    FAM="$(os_family_for_image "$IMG")"
    target_record_add "$RECORDS" "" "$NAME" "$FAM" "$IP" "$PORT" "$WIN_PORT"
  done < <(echo "$INSTANCES" | python3 -c "
import sys, json
for i in (json.load(sys.stdin).get('data') or []):
    img = (i.get('source-details') or {}).get('image-id') or ''
    print('\t'.join([i.get('id',''), i.get('display-name',''), img]))
" 2>/dev/null)
done < <(compartments_to_scan)

COUNT="$(target_records_count "$RECORDS")"
if [[ "$COUNT" == "0" ]]; then
  echo "No RUNNING instances with a private IP found in scope." >&2
fi

case "$OUTPUT" in
  table)
    target_render_table "$RECORDS" false
    echo "($COUNT target(s))" >&2
    ;;

  targets)
    OUT="discovered-targets.json"
    target_render_targets "$RECORDS" "$OUT" false
    echo "Wrote $COUNT target group(s) to $OUT (Prometheus file_sd_config)." >&2
    ;;

  config)
    MERGE_RESULT="$(target_merge_config "$RECORDS" "$CONFIG_FILE")"
    NEW_COUNT="${MERGE_RESULT%%$'\t'*}"
    TOTAL_COUNT="${MERGE_RESULT##*$'\t'}"
    echo "Merged $NEW_COUNT discovered target(s) into $CONFIG_FILE TargetNodes ($TOTAL_COUNT total)." >&2
    ;;

  *) echo "Unknown --output '$OUTPUT' (use table|targets|config)." >&2; exit 1;;
esac
