"""Static checks for container images and the release workflow. No network, no Docker."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTAINER_DIR = ROOT / "container"
DOCKERFILES = sorted(CONTAINER_DIR.glob("*.Dockerfile"))
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
SECRET_ENV = re.compile(r"(SECRET|PASSWORD|TOKEN|ACCESS_KEY|PRIVATE_KEY|DATAKEY|CREDENTIAL)", re.IGNORECASE)
SHA_USES = re.compile(r"^\s*(?:-\s*)?uses:\s*([\w.-]+/[\w./-]+)@([0-9a-f]{40})\s+#\s*\S+", re.MULTILINE)
ANY_USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)", re.MULTILINE)


def _instructions(path: Path) -> list[tuple[str, str]]:
    """Return (INSTRUCTION, rest) pairs with line continuations joined."""
    joined = re.sub(r"\\\n", " ", path.read_text(encoding="utf-8"))
    out = []
    for line in joined.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        keyword, _, rest = stripped.partition(" ")
        out.append((keyword.upper(), rest.strip()))
    return out


def test_dockerfiles_exist_and_metrics_bridge_moved():
    names = {p.name for p in DOCKERFILES}
    assert {"logs-collector.Dockerfile", "metrics-bridge.Dockerfile"} <= names
    assert not (ROOT / "collectors" / "metrics_bridge" / "Dockerfile").exists()


def test_dockerfiles_run_as_non_root_user():
    for path in DOCKERFILES:
        users = [rest for kw, rest in _instructions(path) if kw == "USER"]
        assert users, f"{path.name} has no USER instruction"
        last = users[-1]
        assert last not in {"root", "0"} and not last.startswith("0:"), f"{path.name} runs as root"


def test_dockerfile_base_images_are_pinned_by_tag():
    for path in DOCKERFILES:
        froms = [rest for kw, rest in _instructions(path) if kw == "FROM"]
        assert froms, f"{path.name} has no FROM"
        for image in froms:
            ref = image.split(" ")[0]
            assert ":" in ref.rsplit("/", 1)[-1], f"{path.name}: untagged base {ref}"
            assert not ref.endswith(":latest"), f"{path.name}: floating base {ref}"
            assert "@" not in ref or re.search(r"@sha256:[0-9a-f]{64}$", ref), ref


def test_dockerfiles_have_no_remote_add_or_credentials():
    for path in DOCKERFILES:
        instructions = _instructions(path)
        for kw, rest in instructions:
            if kw == "ADD":
                assert "http://" not in rest and "https://" not in rest, f"{path.name}: ADD from URL"
            if kw in {"ENV", "ARG"}:
                for name in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=", rest):
                    if kw == "ENV":
                        assert not SECRET_ENV.search(name), f"{path.name}: secret-like ENV {name}"
                    if kw == "ARG":
                        assert not SECRET_ENV.search(name), f"{path.name}: secret-like ARG {name}"
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----)", text)


def test_workflow_triggers_on_version_tags_and_dispatch():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = doc.get("on", doc.get(True))
    assert "workflow_dispatch" in triggers
    assert "v*" in triggers["push"]["tags"]


def test_workflow_permissions_are_scoped():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert doc["permissions"]["contents"] == "write"
    assert doc["permissions"]["packages"] == "write"


def test_workflow_actions_pinned_to_full_sha():
    text = WORKFLOW.read_text(encoding="utf-8")
    all_uses = ANY_USES.findall(text)
    pinned = SHA_USES.findall(text)
    assert all_uses, "workflow has no uses: entries"
    assert len(pinned) == len(all_uses), f"unpinned actions: {set(all_uses) - {f'{a}@{s}' for a, s in pinned}}"


def test_workflow_builds_both_images_for_both_architectures():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "container/logs-collector.Dockerfile" in text
    assert "container/metrics-bridge.Dockerfile" in text
    assert "linux/amd64,linux/arm64" in text
    assert "ghcr.io/${{ steps.owner.outputs.lc }}/oci-la-mc-logs-collector" in text
    assert "ghcr.io/${{ steps.owner.outputs.lc }}/oci-la-mc-metrics-bridge" in text


def test_workflow_publishes_orm_zip_asset():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/build_orm_zip.sh" in text
    assert "dist/oci-log-analytics-multicloud-orm.zip" in text
    assert "softprops/action-gh-release" in text


def test_workflow_gates_images_on_tests():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert doc["jobs"]["images"]["needs"] == "test"
    assert set(doc["jobs"]["release"]["needs"]) == {"test", "images"}
