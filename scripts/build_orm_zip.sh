#!/usr/bin/env bash
# Package deploy/orm into dist/oci-log-analytics-multicloud-orm.zip.
# Usage: IMAGE_REGISTRY=ghcr.io/<owner> scripts/build_orm_zip.sh
# Optional: DIST_DIR=<dir> overrides the output directory. Bash 3.2 compatible.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/deploy/orm"
DIST_DIR="${DIST_DIR:-$ROOT/dist}"
ZIP_NAME="oci-log-analytics-multicloud-orm.zip"
TOKEN="__IMAGE_REGISTRY__"

if [ -z "${IMAGE_REGISTRY:-}" ]; then
  echo "error: set IMAGE_REGISTRY (for example ghcr.io/<owner>)" >&2
  exit 2
fi
case "$IMAGE_REGISTRY" in
  *[!A-Za-z0-9./:_-]*) echo "error: IMAGE_REGISTRY has unexpected characters" >&2; exit 2 ;;
  */) echo "error: IMAGE_REGISTRY must not end with a slash" >&2; exit 2 ;;
esac
command -v zip >/dev/null 2>&1 || { echo "error: zip is required" >&2; exit 2; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

(cd "$SRC" && find . -type f \
  ! -path './.terraform/*' ! -name '*.tfstate*' ! -name '.terraform.lock.hcl' \
  ! -name '*.tfvars' ! -name '*.zip' -print) | while IFS= read -r f; do
  mkdir -p "$STAGE/$(dirname "$f")"
  cp "$SRC/$f" "$STAGE/$f"
done

# Image tag: the release passes its tag so the stack pins matching images; "latest" otherwise.
IMAGE_TAG="${IMAGE_TAG:-latest}"
case "$IMAGE_TAG" in
  ""|*[!A-Za-z0-9._-]*) echo "error: IMAGE_TAG has unexpected characters" >&2; exit 2 ;;
esac

# Substitute the registry and tag tokens (| delimiter; values are charset-checked above).
find "$STAGE" -type f \( -name '*.tf' -o -name '*.yaml' -o -name '*.tpl' \) | while IFS= read -r f; do
  sed -e "s|$TOKEN|$IMAGE_REGISTRY|g" -e "s|__IMAGE_TAG__|$IMAGE_TAG|g" "$f" > "$f.new" && mv "$f.new" "$f"
done

# Gate: no real-looking OCIDs or IPv4 addresses, no leftover token.
if grep -rEn 'ocid1\.[a-z]+\.oc[0-9]+\.[a-z0-9-]*\.[a-z0-9]{10,}' "$STAGE" >&2; then
  echo "error: OCID-like value in package" >&2; exit 3
fi
if grep -rEn '(^|[^0-9.])[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}([^0-9]|$)' "$STAGE" | grep -v '0\.0\.0\.0' | grep -v '127\.0\.0\.1' | grep -v '/[0-9][0-9]*"' >&2; then
  echo "error: IPv4 address in package" >&2; exit 3
fi
if grep -rn "$TOKEN" "$STAGE" >&2; then
  echo "error: image registry token left in package" >&2; exit 3
fi

mkdir -p "$DIST_DIR"
rm -f "$DIST_DIR/$ZIP_NAME"
(cd "$STAGE" && zip -qr -X "$DIST_DIR/$ZIP_NAME" .)
echo "$DIST_DIR/$ZIP_NAME"
