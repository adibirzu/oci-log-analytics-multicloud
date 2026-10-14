"""Static, offline checks for the read-only metric reader Terraform modules."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLOUDS = ["aws", "azure", "gcp"]
FILES = ["main.tf", "variables.tf", "outputs.tf", "versions.tf", "README.md"]
ALLOWED_CW = {
    "cloudwatch:GetMetricData",
    "cloudwatch:ListMetrics",
    "cloudwatch:GetMetricStatistics",
}


def mod_dir(cloud):
    return ROOT / "modules" / "metrics" / cloud / "terraform"


def tf_text(cloud):
    return "\n".join(p.read_text() for p in sorted(mod_dir(cloud).glob("*.tf")))


@pytest.mark.parametrize("cloud", CLOUDS)
def test_module_has_required_files(cloud):
    for name in FILES:
        assert (mod_dir(cloud) / name).is_file(), f"{cloud}: missing {name}"


@pytest.mark.parametrize("cloud", CLOUDS)
def test_no_wildcard_actions(cloud):
    text = tf_text(cloud)
    for m in re.finditer(r"\b(?:actions|Action|not_actions)\b\s*=\s*(\[[^\]]*\])", text):
        assert '"*"' not in m.group(1), f"{cloud}: wildcard action list {m.group(1)}"
    assert not re.search(r'"[a-z0-9-]+:\*"', text), f"{cloud}: service wildcard action"


@pytest.mark.parametrize("cloud", CLOUDS)
def test_no_privileged_roles(cloud):
    text = tf_text(cloud)
    assert not re.search(r"roles/(owner|editor)\b", text)
    assert not re.search(r"\b(Contributor|Owner)\b", text)


def test_aws_policy_actions_exact():
    text = (mod_dir("aws") / "main.tf").read_text()
    quoted = set(re.findall(r'"([a-z0-9-]+:[A-Za-z*]+)"', text))
    cw = {a for a in quoted if a.startswith("cloudwatch:")}
    assert cw == ALLOWED_CW
    assert quoted - cw == {"sts:AssumeRoleWithWebIdentity", "tag:GetResources"}
    assert "var.allow_tag_lookup" in text


def test_azure_and_gcp_roles():
    assert 'role_definition_name = "Monitoring Reader"' in tf_text("azure")
    gcp = tf_text("gcp")
    assert 'role   = "roles/monitoring.viewer"' in gcp or 'role    = "roles/monitoring.viewer"' in gcp
    assert "attribute_condition" in gcp


@pytest.mark.parametrize("cloud", CLOUDS)
def test_no_static_keys(cloud):
    text = tf_text(cloud)
    for forbidden in (
        "aws_iam_access_key",
        "google_service_account_key",
        "azuread_application_password",
        "azuread_service_principal_password",
        "azuread_application_certificate",
    ):
        assert forbidden not in text, f"{cloud}: {forbidden}"


@pytest.mark.parametrize("cloud", CLOUDS)
def test_no_real_identifiers(cloud):
    d = mod_dir(cloud)
    text = "\n".join(p.read_text() for p in d.iterdir() if p.is_file())
    assert not re.search(r"(?<![0-9])[0-9]{12}(?![0-9])", text)
    assert "ocid" + "1." not in text
    assert not re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", text)


@pytest.mark.parametrize("cloud", CLOUDS)
def test_identifiers_have_no_defaults(cloud):
    text = (mod_dir(cloud) / "variables.tf").read_text()
    for var in ("account_id", "subscription_id", "tenant_id", "project_id"):
        m = re.search(r'variable "%s" \{(.*?)\n\}' % var, text, re.S)
        if m:
            assert "default" not in m.group(1), f"{cloud}: {var} has a default"
    assert "validation" in text


def test_terraform_fmt_check():
    if shutil.which("terraform") is None:
        pytest.skip("terraform not installed")
    r = subprocess.run(
        ["terraform", "fmt", "-check", "-recursive", "modules/metrics"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
