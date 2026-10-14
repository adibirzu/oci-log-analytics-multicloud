#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/target-discovery.sh
source "$ROOT/lib/target-discovery.sh"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_contains() {
  local haystack="$1"
  local needle="$2"
  [[ "$haystack" == *"$needle"* ]] || fail "expected output to contain: $needle"
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
RECORDS="$TMP/records.tsv"

target_records_init "$RECORDS"
target_record_add "$RECORDS" "oci" "linux-vm" "linux" "10.0.0.10" "9100" "9182"
target_record_add "$RECORDS" "oci" "win-vm" "windows" "10.0.0.11" "9100" "9182"

table="$(target_render_table "$RECORDS" false)"
assert_contains "$table" "linux-vm"
assert_contains "$table" "10.0.0.10:9100"
assert_contains "$table" "win-vm"
assert_contains "$table" "10.0.0.11:9182"

target_render_targets "$RECORDS" "$TMP/oci-targets.json" false
python3 - "$TMP/oci-targets.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data[0]["labels"] == {"os": "linux", "instance": "linux-vm"}
assert "cloud" not in data[0]["labels"]
assert data[1]["targets"] == ["10.0.0.11:9182"]
PY

target_render_targets "$RECORDS" "$TMP/cloud-targets.json" true
python3 - "$TMP/cloud-targets.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data[0]["labels"]["cloud"] == "oci"
assert data[0]["labels"]["os"] == "linux"
assert data[1]["labels"]["instance"] == "win-vm"
PY

cat > "$TMP/config.json" <<'JSON'
{
    "Mode": "Proxy",
    "TargetNodes": [
        "10.0.0.10:9100"
    ],
    "OtelEnabled": true
}
JSON

merge_result="$(target_merge_config "$RECORDS" "$TMP/config.json")"
[[ "$merge_result" == $'2\t2' ]] || fail "unexpected merge result: $merge_result"
python3 - "$TMP/config.json" <<'PY'
import json
import sys

cfg = json.load(open(sys.argv[1], encoding="utf-8"))
assert cfg["Mode"] == "Proxy"
assert cfg["OtelEnabled"] is True
assert cfg["TargetNodes"] == ["10.0.0.10:9100", "10.0.0.11:9182"]
PY

echo "target-discovery tests passed"
