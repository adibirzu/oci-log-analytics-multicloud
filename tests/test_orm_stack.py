"""Static and packaging checks for the Resource Manager stack in deploy/orm."""

import json
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


@pytest.mark.parametrize("image", ["logs-collector", "metrics-bridge", "otel-federation"])
def test_release_builds_orm_images_for_both_architectures(image):
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["images"]
    assert job["runs-on"]
    assert job.get("if", True) is True
    builds = [
        step for step in job["steps"]
        if step.get("uses", "").partition("@")[0] == "docker/build-push-action"
        and step.get("with", {}).get("file") == f"container/{image}.Dockerfile"
    ]
    assert len(builds) == 1
    step = builds[0]
    assert step.get("if", True) is True
    settings = step["with"]
    assert settings["context"] == "."
    assert {platform.strip() for platform in settings["platforms"].split(",")} == {"linux/amd64", "linux/arm64"}
    assert settings["push"] is True
    tags = [tag.strip() for tag in settings["tags"].splitlines() if tag.strip()]
    assert tags == [
        "ghcr.io/${{ steps.owner.outputs.lc }}/oci-la-mc-" + image + ":${{ steps.ver.outputs.version }}",
        "${{ startsWith(github.ref, 'refs/tags/v') && format('ghcr.io/{0}/oci-la-mc-"
        + image + ":latest', steps.owner.outputs.lc) || '' }}",
    ]


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform not installed")
@pytest.mark.parametrize("cloud,missing_identity", [("aws", False), ("azure", False), ("gcp", False),
                                                   ("aws", True), ("azure", True)])
@pytest.mark.parametrize("logs,metrics", [(True, True), (True, False), (False, True)])
def test_signal_environments_render_with_separate_identities(tmp_path, cloud, logs, metrics, missing_identity):
    main_locals = "locals {" + (ORM / "main.tf").read_text().split("locals {", 1)[1]
    compute_locals = (ORM / "compute.tf").read_text().split('\nresource "', 1)[0]
    configuration = main_locals + "\n" + compute_locals
    for external in ("data", "oci_vault_secret", "oci_container_instances_container_instance"):
        configuration = configuration.replace(external + ".", "var." + external + ".")
    (tmp_path / "main.tf").write_text(configuration + '\nlocals {\n  log_group_ocid = "<LOG_GROUP>"\n  apm_endpoint = "<APM_ENDPOINT>"\n  apm_private_key = "<APM_KEY>"\n}\n')
    shutil.copyfile(ORM / "variables.tf", tmp_path / "variables.tf")
    fakes = {
        "data": {
            "oci_objectstorage_namespace": {"this": {"namespace": "<NAMESPACE>"}},
            "oci_identity_availability_domains": {"this": {"availability_domains": [{"name": "<AD>"}]}},
        },
        "oci_vault_secret": {"federation": {cloud: {"id": "<SECRET_ID>"}}},
        "oci_container_instances_container_instance": {"runtime": [{"vnics": [{"private_ip": "<BRIDGE_HOST>"}]}]},
    }
    (tmp_path / "fakes.tf").write_text("\n".join(
        f'variable "{name}" {{ default = {json.dumps(value)} }}' for name, value in fakes.items()
    ))
    shutil.copytree(ORM / "templates", tmp_path / "templates")
    provider = "projects/" + "123" + "/locations/global/workloadIdentityPools/"
    inputs = {
        "region": "<REGION>", "tenancy_ocid": "<TENANCY>", "compartment_ocid": "<COMPARTMENT>",
        f"enable_{cloud}": True, "enable_logs": logs, "enable_metrics": metrics,
        "gcp_logs_wif_provider_resource": provider + "logs/providers/logs",
        "gcp_metrics_wif_provider_resource": provider + "metrics/providers/metrics",
        "gcp_metrics_service_account_email": "reader@" + "example-project" + ".iam.gserviceaccount.com",
    }
    identities = {
        "aws": ("AWS_ROLE_ARN", "aws_{}_role_arn"),
        "azure": ("AZURE_CLIENT_ID", "azure_{}_client_id"),
        "gcp": ("GCP_WIF_PROVIDER_RESOURCE", "gcp_{}_wif_provider_resource"),
    }
    env_key, input_key = identities[cloud]
    for signal, enabled in (("logs", logs), ("metrics", metrics)):
        if enabled and cloud != "gcp":
            inputs[input_key.format(signal)] = f"<SYNTHETIC_{signal.upper()}_IDENTITY>"
    if missing_identity:
        inputs.pop(input_key.format("logs" if logs else "metrics"))
    (tmp_path / "terraform.tfvars.json").write_text(json.dumps(inputs))
    validation = subprocess.run(["terraform", "validate", "-no-color"], cwd=tmp_path, text=True, capture_output=True)
    assert validation.returncode == 0, validation.stderr
    result = subprocess.run(
        ["terraform", "console", "-no-color"], cwd=tmp_path, text=True, capture_output=True,
        input='jsonencode({logs = local.logs_containers, metrics = local.metrics_pullers})\n',
    )
    if missing_identity:
        assert "no non-null, non-empty-string arguments" in result.stderr
        with pytest.raises(json.JSONDecodeError):
            json.loads(result.stdout)
        return
    assert result.returncode == 0, result.stderr
    rendered = json.loads(json.loads(result.stdout))
    assert set(rendered["logs"]) == ({cloud} if logs else set())
    assert set(rendered["metrics"]) == ({cloud} if metrics else set())
    for signal, enabled in (("logs", logs), ("metrics", metrics)):
        if enabled:
            env = rendered[signal][cloud]["env"]
            assert env[env_key] == inputs[input_key.format(signal)]
            if cloud == "gcp" and signal == "metrics":
                assert env["GCP_METRICS_SERVICE_ACCOUNT_EMAIL"] == inputs["gcp_metrics_service_account_email"]
