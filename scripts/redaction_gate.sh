#!/usr/bin/env bash
# Redaction gate: fails when real-looking identifiers or secrets are present.
#   scripts/redaction_gate.sh --staged   # added lines in `git diff --cached`
#   scripts/redaction_gate.sh --all      # every tracked (and unignored new) file
# Prints "file:line: <pattern name>" only, never the matched value.
# Extra ERE patterns (one per line, '#' comments) may live in the gitignored
# .redaction-patterns.local at the repo root.
# Allowlist: a line containing a <PLACEHOLDER>-style token or ${...} is skipped.
# Compatible with macOS bash 3.2 and POSIX awk (no gawk extensions).
set -u

mode="${1:-}"
case "$mode" in
  --staged|--all) ;;
  *) echo "usage: $0 --staged|--all" >&2; exit 2 ;;
esac

root="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "not a git repo" >&2; exit 2; }
cd "$root" || exit 2

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# name | kind | ERE   (kind: re = plain match, ip = IPv4 filter, email = email filter,
# acct = 12-digit id filter that tolerates documented sample ids and repeated digits)
names=(); kinds=(); res=()
add() { names[${#names[@]}]="$1"; kinds[${#kinds[@]}]="$2"; res[${#res[@]}]="$3"; }

add "OCI OCID" re 'ocid1\.[a-z0-9]+\.oc1\.[a-z0-9-]*\.[a-z0-9]{20,}'
add "AWS account id in ARN" acct 'arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:[0-9]{12}:'
add "12-digit account id" acct '[Aa][Cc][Cc][Oo][Uu][Nn][Tt][^0-9]{0,24}(^|[^0-9])[0-9]{12}([^0-9]|$)'
add "12-digit account id" acct '(^|[^0-9])[0-9]{12}[^0-9]{0,24}[Aa][Cc][Cc][Oo][Uu][Nn][Tt]'
add "public IPv4" ip '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+'
add "AWS access key" re 'AKIA[0-9A-Z]{16}'
add "PEM private key" re '-----BEGIN [A-Z ]*PRIVATE KEY-----'
add "GCP service account key" re '"private_key"[[:space:]]*:'
add "Azure client secret" re '(client_secret|CLIENT_SECRET)[A-Za-z_]*["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"'$<{ ]{8,}["'"'"']'
add "Azure client secret" re '^[[:space:]]*(export[[:space:]]+)?[A-Z_]*CLIENT_SECRET=[^"'"'"'$<{ ]{8,}'
add "APM dataKey literal" re 'dataKey [A-Za-z0-9]{20,}'
add "email address" email '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'

if [ -f .redaction-patterns.local ]; then
  while IFS= read -r p || [ -n "$p" ]; do
    case "$p" in ''|'#'*) continue ;; esac
    add "local pattern" re "$p"
  done < .redaction-patterns.local
fi

# awk filter over "lineno:text" records. vars: kind, added (space-separated
# line numbers, empty = all lines). Emits matching line numbers only.
filter_awk='
function isdigits(s) { return s ~ /^[0-9]+$/ }
function public_ip(s,   n, o, i) {
  n = split(s, o, ".")
  if (n != 4) return 0
  for (i = 1; i <= 4; i++) { if (!isdigits(o[i]) || o[i] + 0 > 255) return 0 }
  a = o[1] + 0; b = o[2] + 0; c = o[3] + 0
  if (a == 0 || a == 10 || a == 127) return 0
  if (a == 172 && b >= 16 && b <= 31) return 0
  if (a == 192 && b == 168) return 0
  if (a == 169 && b == 254) return 0
  if (a == 192 && b == 0 && c == 2) return 0
  if (a == 198 && b == 51 && c == 100) return 0
  if (a == 203 && b == 0 && c == 113) return 0
  if (a >= 224) return 0
  return 1
}
function ip_hit(t,   rest, pre, post, m) {
  rest = t
  while (match(rest, /[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/)) {
    m = substr(rest, RSTART, RLENGTH)
    pre = (RSTART > 1) ? substr(rest, RSTART - 1, 1) : ""
    post = substr(rest, RSTART + RLENGTH, 1)
    rest = substr(rest, RSTART + RLENGTH)
    if (pre ~ /[0-9.A-Za-z]/ || post ~ /[0-9A-Za-z]/) continue
    if (post == "." && rest ~ /^\.[0-9]/) continue
    if (public_ip(m)) return 1
  }
  return 0
}
function email_hit(t,   rest, m, local, dom) {
  rest = t
  while (match(rest, /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z][A-Za-z]+/)) {
    m = tolower(substr(rest, RSTART, RLENGTH))
    rest = substr(rest, RSTART + RLENGTH)
    split(m, parts, "@"); local = parts[1]; dom = parts[2]
    if (dom == "example.com" || dom == "example.org" || dom ~ /\.example\.(com|org)$/) continue
    if (dom ~ /\.(invalid|test|localhost|example)$/ || dom == "system.gserviceaccount.com") continue
    if (dom ~ /(^|\.)noreply\./) continue
    if (local == "noreply" || local == "no-reply" || dom ~ /^noreply\./) continue
    if (dom ~ /\.(png|jpg|jpeg|gif|svg|webp)$/) continue
    return 1
  }
  return 0
}
function sample_acct(d) {
  if (d == "123456789012" || d == "111122223333" || d == "210987654321") return 1
  return (d ~ /^0+$/ || d ~ /^1+$/ || d ~ /^9+$/)
}
function acct_hit(t,   rest, m) {
  rest = t
  while (match(rest, /[0-9]+/)) {
    m = substr(rest, RSTART, RLENGTH)
    rest = substr(rest, RSTART + RLENGTH)
    if (length(m) == 12 && !sample_acct(m)) return 1
  }
  return 0
}
BEGIN {
  if (mapfile != "") { while ((getline l < mapfile) > 0) ok[l] = 1; close(mapfile); filtered = 1 }
}
{
  p = index($0, ":"); path = substr($0, 1, p - 1); rest = substr($0, p + 1)
  p = index(rest, ":"); ln = substr(rest, 1, p - 1); text = substr(rest, p + 1)
  if (filtered && !((path " " ln) in ok)) next
  if (text ~ /<[A-Za-z_][A-Za-z0-9_-]*>/ || index(text, "${") > 0) next
  if (kind == "ip" && !ip_hit(text)) next
  if (kind == "email" && !email_hit(text)) next
  if (kind == "acct" && !acct_hit(text)) next
  print path ":" ln
}'

fail=0
files="$tmp/files"; : > "$files"; mapfile="$tmp/added.map"; : > "$mapfile"
scandir="."
if [ "$mode" = "--all" ]; then
  git ls-files --cached --others --exclude-standard | while IFS= read -r f; do
    [ -f "$f" ] && [ "$f" != ".redaction-patterns.local" ] && printf '%s\n' "$f"
  done > "$files"
  mapname=""
else
  scandir="$tmp/blobs"; mkdir -p "$scandir"
  git diff --cached --name-only --diff-filter=ACMR | while IFS= read -r f; do
    [ "$f" != ".redaction-patterns.local" ] && printf '%s\n' "$f"
  done > "$files"
  while IFS= read -r f; do
    mkdir -p "$scandir/$(dirname "$f")"
    git show ":$f" > "$scandir/$f" 2>/dev/null || continue
    git diff --cached -U0 --diff-filter=ACMR -- "$f" | awk -v f="$f" '
      /^@@/ { s = $3; sub(/^\+/, "", s); n = split(s, a, ","); st = a[1] + 0; c = (n > 1) ? a[2] + 0 : 1
              for (k = 0; k < c; k++) print f " " (st + k) }' >> "$mapfile"
  done < "$files"
  mapname="$mapfile"
fi

if [ -s "$files" ]; then
  i=0
  while [ "$i" -lt "${#names[@]}" ]; do
    out="$(tr '\n' '\0' < "$files" | (cd "$scandir" && xargs -0 grep -nIHE -e "${res[$i]}" -- 2>/dev/null) \
      | awk -v kind="${kinds[$i]}" -v mapfile="$mapname" "$filter_awk" | sort -u)"
    if [ -n "$out" ]; then
      while IFS= read -r hit; do
        printf '%s: %s\n' "$hit" "${names[$i]}"
      done <<EOF2
$out
EOF2
      fail=1
    fi
    i=$((i + 1))
  done
fi

if [ "$fail" -ne 0 ]; then
  echo "redaction gate FAILED: replace with <PLACEHOLDER> or \${VAR} (values are never printed)." >&2
  exit 1
fi
echo "redaction gate: clean"
