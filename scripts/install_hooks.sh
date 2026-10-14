#!/usr/bin/env bash
# Install a pre-commit hook that runs the redaction gate on staged lines.
set -eu
root="$(git rev-parse --show-toplevel)"
hooks="$(git rev-parse --git-path hooks)"
case "$hooks" in /*) ;; *) hooks="$root/$hooks" ;; esac
mkdir -p "$hooks"
hook="$hooks/pre-commit"
if [ -e "$hook" ] && ! grep -q 'redaction_gate.sh' "$hook"; then
  echo "refusing to overwrite existing pre-commit hook: $hook" >&2
  exit 1
fi
cat > "$hook" <<'HOOK'
#!/usr/bin/env bash
root="$(git rev-parse --show-toplevel)"
exec bash "$root/scripts/redaction_gate.sh" --staged
HOOK
chmod +x "$hook"
echo "installed pre-commit hook: $hook"
