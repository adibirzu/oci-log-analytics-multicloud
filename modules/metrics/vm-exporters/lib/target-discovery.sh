#!/usr/bin/env bash
# Shared target discovery records and output rendering.

target_port_for_os() {
  local os_family="${1:-linux}"
  local linux_port="${2:-9100}"
  local windows_port="${3:-9182}"
  if [[ "$os_family" == "windows" ]]; then
    echo "$windows_port"
  else
    echo "$linux_port"
  fi
}

target_records_init() {
  local records_file="$1"
  : > "$records_file"
}

target_record_add() {
  local records_file="$1"
  local cloud="$2"
  local name="$3"
  local os_family="$4"
  local ip="$5"
  local linux_port="${6:-9100}"
  local windows_port="${7:-9182}"

  [[ -z "$ip" || "$ip" == "null" ]] && return 0
  [[ -z "$os_family" ]] && os_family="linux"
  local port
  port="$(target_port_for_os "$os_family" "$linux_port" "$windows_port")"
  printf '%s\t%s\t%s\t%s\t%s\n' "$cloud" "$name" "$os_family" "$ip" "$port" >> "$records_file"
}

target_records_count() {
  local records_file="$1"
  wc -l < "$records_file" | tr -d ' '
}

target_render_table() {
  local records_file="$1"
  local include_cloud_label="${2:-false}"

  if [[ "$include_cloud_label" == "true" ]]; then
    printf '%-7s %-28s %-8s %-22s\n' "CLOUD" "NAME" "OS" "TARGET"
    printf '%-7s %-28s %-8s %-22s\n' "-----" "----" "--" "------"
    while IFS=$'\t' read -r cloud name os_family ip port; do
      printf '%-7s %-28s %-8s %-22s\n' "$cloud" "$name" "$os_family" "$ip:$port"
    done < "$records_file"
  else
    printf '%-30s %-8s %-22s\n' "NAME" "OS" "TARGET"
    printf '%-30s %-8s %-22s\n' "----" "--" "------"
    while IFS=$'\t' read -r _cloud name os_family ip port; do
      printf '%-30s %-8s %-22s\n' "$name" "$os_family" "$ip:$port"
    done < "$records_file"
  fi
}

target_render_targets() {
  local records_file="$1"
  local output_file="${2:-discovered-targets.json}"
  local include_cloud_label="${3:-false}"

  python3 - "$records_file" "$output_file" "$include_cloud_label" <<'PY'
import json
import sys

records_file, output_file, include_cloud_label = sys.argv[1:4]
groups = []
with open(records_file, encoding="utf-8") as handle:
    for line in handle:
        if not line.strip():
            continue
        cloud, name, os_family, ip, port = line.rstrip("\n").split("\t")
        labels = {"os": os_family, "instance": name}
        if include_cloud_label == "true":
            labels = {"cloud": cloud, **labels}
        groups.append({"targets": [f"{ip}:{port}"], "labels": labels})

with open(output_file, "w", encoding="utf-8") as handle:
    json.dump(groups, handle, indent=2)
    handle.write("\n")
PY
}

target_merge_config() {
  local records_file="$1"
  local config_file="$2"

  python3 - "$records_file" "$config_file" <<'PY'
import json
import os
import sys

records_file, config_file = sys.argv[1:3]
cfg = {}
if os.path.exists(config_file):
    try:
        with open(config_file, encoding="utf-8") as handle:
            cfg = json.load(handle)
    except Exception:
        cfg = {}

existing = cfg.get("TargetNodes") or []
new_targets = []
with open(records_file, encoding="utf-8") as handle:
    for line in handle:
        if not line.strip():
            continue
        _cloud, _name, _os_family, ip, port = line.rstrip("\n").split("\t")
        new_targets.append(f"{ip}:{port}")

cfg["TargetNodes"] = list(dict.fromkeys([*existing, *new_targets]))
with open(config_file, "w", encoding="utf-8") as handle:
    json.dump(cfg, handle, indent=4)
    handle.write("\n")

print(f'{len(new_targets)}\t{len(cfg["TargetNodes"])}')
PY
}
