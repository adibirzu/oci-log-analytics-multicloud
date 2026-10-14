"""Static checks for the least-privilege AWS operator role module."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "modules" / "bootstrap" / "aws-operator-role"
ACCOUNT = "123456789" + "012"
SUBS = {
    "${partition}": "aws",
    "${account_id}": ACCOUNT,
    "${name_prefix}": "mc-",
    "${operator_role_name}": "mc-operator",
    "${oidc_provider_resources}": json.dumps([f"arn:aws:iam::{ACCOUNT}:oidc-provider/*"]),
}
WRITE_VERBS = re.compile(
    r"^(?:[a-z0-9]+:)(Create|Delete|Put|Update|Set|Tag|Untag|Add|Remove|Start|Stop|Attach|Detach|"
    r"AssumeRole|SendMessage|ReceiveMessage|ChangeMessageVisibility)")
PREFIXED = re.compile(r"(?:[:/]|:::)mc-\*|:trail/mc-\*|mc-\*collector$")


def _statements():
    text = (MODULE / "policy.json.tpl").read_text()
    for key, value in SUBS.items():
        text = text.replace(key, value)
    assert "${" not in text
    return json.loads(text)["Statement"]


def _as_list(value):
    return [value] if isinstance(value, str) else list(value)


def test_no_wildcard_actions():
    for st in _statements():
        for action in _as_list(st["Action"]):
            assert action != "*" and not action.endswith(":*"), (st["Sid"], action)
            assert "*" not in action, (st["Sid"], action)


def test_every_allow_write_statement_is_prefix_scoped():
    checked = 0
    for st in _statements():
        if st["Effect"] != "Allow":
            continue
        if not any(WRITE_VERBS.match(a) for a in _as_list(st["Action"])):
            continue
        checked += 1
        for res in _as_list(st["Resource"]):
            assert res != "*", st["Sid"]
            if "oidc-provider/" in res:  # OIDC providers are named by issuer, not prefix
                assert res.startswith("arn:aws:iam::")
            else:
                assert PREFIXED.search(res), (st["Sid"], res)
    assert checked >= 6


def test_wildcard_resource_only_for_unscopable_reads():
    for st in _statements():
        if st["Effect"] == "Allow" and "*" in _as_list(st.get("Resource", [])):
            for action in _as_list(st["Action"]):
                assert re.match(r"^[a-z0-9]+:(Get|List|Describe)", action), action


def test_explicit_denies_present():
    denies = {st["Sid"]: st for st in _statements() if st["Effect"] == "Deny"}
    assert {"DenyIamWritesOutsidePrefix", "DenyManagedPolicyAttachmentAndBoundaryChanges",
            "DenyOperatorRoleSelfModification"} <= set(denies)
    assert "iam:AttachRolePolicy" in denies["DenyManagedPolicyAttachmentAndBoundaryChanges"]["Action"]
    assert "NotResource" in denies["DenyIamWritesOutsidePrefix"]


def test_required_actions_covered():
    actions = {a for st in _statements() if st["Effect"] == "Allow" for a in _as_list(st["Action"])}
    required = {
        "sts:GetCallerIdentity", "s3:CreateBucket", "s3:DeleteBucket", "s3:ListBucketVersions",
        "s3:DeleteObjectVersion", "s3:GetObject", "sqs:CreateQueue", "sqs:ReceiveMessage",
        "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes",
        "cloudtrail:CreateTrail", "cloudtrail:GetTrailStatus", "iam:CreateRole",
        "iam:PutRolePolicy", "iam:CreateOpenIDConnectProvider", "sts:AssumeRole",
        "cloudwatch:GetMetricData", "cloudwatch:ListMetrics",
    }
    assert required <= actions, required - actions


def test_trust_policy_has_exactly_one_principal_variable():
    text = (MODULE / "main.tf").read_text()
    assert len(re.findall(r"Principal\s*=", text)) == 1
    assert text.count("var.trusted_principal_arn") == 1
    assert "aws:MultiFactorAuthPresent" in text and "var.require_mfa" in text
    assert '"*"' not in text.split("trust_policy")[1].split("data \"aws_caller_identity\"")[0]


def test_variable_defaults():
    text = (MODULE / "variables.tf").read_text()
    for name, default in (("partition", '"aws"'), ("role_name", '"mc-operator"'),
                          ("name_prefix", '"mc-"'), ("require_mfa", "false"),
                          ("max_session_duration", "3600")):
        block = re.search(rf'variable "{name}" \{{(.*?)\n\}}', text, re.S).group(1)
        assert re.search(rf"default\s*=\s*{re.escape(default)}", block), name
    for name in ("account_id", "trusted_principal_arn"):
        block = re.search(rf'variable "{name}" \{{(.*?)\n\}}', text, re.S).group(1)
        assert "default" not in block, name
    for out in ("role_arn", "policy_json"):
        assert f'output "{out}"' in (MODULE / "outputs.tf").read_text()


def test_no_embedded_account_ids():
    for path in MODULE.rglob("*"):
        if path.is_file() and ".terraform" not in path.parts:
            assert not re.search(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])", path.read_text()), path


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform not installed")
def test_terraform_fmt_clean():
    result = subprocess.run(["terraform", "fmt", "-check", "-diff", str(MODULE)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform not installed")
def test_terraform_validate_in_temp_copy(tmp_path):
    work = tmp_path / "m"
    shutil.copytree(MODULE, work, ignore=shutil.ignore_patterns(".terraform*"))
    init = subprocess.run(["terraform", "init", "-backend=false", "-input=false"], cwd=work,
                          capture_output=True, text=True, check=False, timeout=120)
    if init.returncode != 0:
        pytest.skip("provider unavailable offline")
    result = subprocess.run(["terraform", "validate"], cwd=work, capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
