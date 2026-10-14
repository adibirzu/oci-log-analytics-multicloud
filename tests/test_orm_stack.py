"""Static and packaging checks for the Resource Manager stack in deploy/orm."""

import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ORM = ROOT / "deploy" / "orm"
BUILD = ROOT / "scripts" / "build_orm_zip.sh"
BUILTINS = {"region", "tenancy_ocid"}
EXPECTED = ("provider.tf", "versions.tf", "variables.tf", "main.tf", "iam.tf", "logging_analytics.tf",
            "apm.tf", "vault.tf", "compute.tf", "outputs.tf", "schema.yaml")


def _tf_text():
    return {p.name: p.read_text(encoding="utf-8") for p in ORM.glob("*.tf")}


def _tf_variables():
    return set(re.findall(r'^variable "(\w+)"', "\n".join(_tf_text().values()), re.M))


def test_expected_files_exist():
    assert not [n for n in EXPECTED if not (ORM / n).is_file()]
    assert (ORM / "templates").is_dir()


def test_schema_variables_match_variables_tf():
    schema = yaml.safe_load((ORM / "schema.yaml").read_text(encoding="utf-8"))
    declared = set(schema["variables"])
    grouped = {v for g in schema["variableGroups"] for v in g["variables"]}
    tf_vars = _tf_variables()
    assert declared == tf_vars
    assert grouped == tf_vars
    for name, spec in schema["variables"].items():
        if name not in BUILTINS:
            assert spec.get("title") and spec.get("description"), name


def test_cloud_identifiers_visible_only_when_cloud_enabled():
    schema = yaml.safe_load((ORM / "schema.yaml").read_text(encoding="utf-8"))["variables"]
    for name, spec in schema.items():
        for cloud in ("aws", "azure", "gcp"):
            if name.startswith(cloud + "_"):
                assert spec["visible"] == f"enable_{cloud}", name


def test_no_secret_inputs():
    pattern = re.compile(r"secret|password|token|access_key|private_key|datakey", re.I)
    assert not [v for v in _tf_variables() if pattern.search(v)]


def test_every_resource_is_gated_by_a_toggle():
    ungated = []
    for name, text in _tf_text().items():
        for match in re.finditer(r'^resource "(\w+)" "(\w+)" \{(.*?)^\}', text, re.M | re.S):
            body = match.group(3)
            if not re.search(r"^\s*(count|for_each)\s*=", body, re.M):
                ungated.append(f"{name}:{match.group(1)}.{match.group(2)}")
    assert not ungated


def test_no_hardcoded_ocids_or_regions():
    region = re.compile(r"\b(?:us|eu|ap|sa|me|af|ca|uk|il|mx)-[a-z]+-\d\b")
    ocid = re.compile(r"ocid1\.")
    offenders = []
    files = list(ORM.glob("*.tf")) + list((ORM / "templates").glob("*")) + [ORM / "schema.yaml"]
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            if ocid.search(line) or region.search(line):
                offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders


def test_policies_are_compartment_scoped():
    statements = re.findall(r'"(allow [^"]+)"', _tf_text()["iam.tf"])
    assert statements
    for stmt in statements:
        assert " in compartment id ${var.compartment_ocid}" in stmt, stmt
        assert "tenancy" not in stmt
    assert "custom_" in _tf_text()["iam.tf"]
    assert "computecontainerinstance" in _tf_text()["iam.tf"]


def test_otel_image_pinned():
    assert "opentelemetry-collector-contrib:0.162.0" in _tf_text()["variables.tf"]


def _build(tmp_path, registry="ghcr.io/example-owner"):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "DIST_DIR": str(tmp_path)}
    if registry is not None:
        env["IMAGE_REGISTRY"] = registry
    return subprocess.run(["bash", str(BUILD)], env=env, capture_output=True, text=True)


def test_zip_build_substitutes_registry_token(tmp_path):
    result = _build(tmp_path)
    assert result.returncode == 0, result.stderr
    archive = zipfile.ZipFile(tmp_path / "oci-log-analytics-multicloud-orm.zip")
    names = archive.namelist()
    assert "schema.yaml" in names and "templates/apm-gateway.yaml.tpl" in names
    assert not [n for n in names if ".terraform" in n or n.endswith(".tfstate")]
    variables = archive.read("variables.tf").decode()
    assert "ghcr.io/example-owner/oci-la-mc-logs-collector:latest" in variables
    assert "__IMAGE_REGISTRY__" not in "".join(archive.read(n).decode(errors="ignore") for n in names if not n.endswith("/"))


def test_zip_build_requires_registry(tmp_path):
    result = _build(tmp_path, registry=None)
    assert result.returncode != 0
    assert not list(tmp_path.glob("*.zip"))


def test_zip_build_rejects_bad_registry(tmp_path):
    assert _build(tmp_path, registry="ghcr.io/x y").returncode != 0
    assert _build(tmp_path, registry="ghcr.io/x/").returncode != 0


def test_zip_gate_rejects_ocid_like_value(tmp_path):
    probe = ORM / "zz_probe.tf"
    probe.write_text('locals { x = "' + "ocid" + "1.compartment.oc1..aaaaaaaaaaaaaaaaaaaa" + '" }\n', encoding="utf-8")
    try:
        result = _build(tmp_path)
    finally:
        probe.unlink()
    assert result.returncode == 3
    assert not list(tmp_path.glob("*.zip"))


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform not installed")
def test_terraform_validates_in_temp_copy(tmp_path):
    work = tmp_path / "orm"
    shutil.copytree(ORM, work, ignore=shutil.ignore_patterns(".terraform", ".terraform.lock.hcl"))
    init = subprocess.run(["terraform", "init", "-backend=false", "-input=false", "-no-color"],
                          cwd=work, capture_output=True, text=True)
    if init.returncode != 0:
        pytest.skip("provider registry unreachable: " + init.stderr[-200:])
    check = subprocess.run(["terraform", "validate", "-no-color"], cwd=work, capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr
    fmt = subprocess.run(["terraform", "fmt", "-check", "-recursive", "-no-color"], cwd=work, capture_output=True, text=True)
    assert fmt.returncode == 0, fmt.stdout


def test_orm_image_names_match_published_images():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
    published = set(re.findall(r"/(oci-la-mc-[a-z-]+):", workflow))
    referenced = set(re.findall(r"__IMAGE_REGISTRY__/([a-z0-9-]+):__IMAGE_TAG__",
                                (root / "deploy/orm/variables.tf").read_text(encoding="utf-8")))
    assert referenced and referenced <= published, (referenced, published)
