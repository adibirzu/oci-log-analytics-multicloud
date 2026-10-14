"""Redaction gate tests. Every sensitive-looking value is assembled at runtime."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "redaction_gate.sh"


def _j(*parts):
    return "".join(parts)


# (pattern name expected in output, file content)
CASES = {
    "OCI OCID": _j("ocid", "1.tenancy.oc1..", "a" * 30),
    "AWS account id in ARN": _j("arn:aws:iam::", "4", "8", "5", "9", "0", "3", "7", "1", "6", "2", "8", "4", ":role/x"),
    "12-digit account id": _j("account_id = \"", "58", "4920", "1736", "12", "\""),
    "public IPv4": _j("host = ", "8.", "8.", "4.", "4"),
    "AWS access key": _j("AK", "IA", "Q" * 16),
    "PEM private key": _j("-----BEGIN ", "RSA PRIVATE", " KEY-----"),
    "GCP service account key": _j('{"private', '_key": "x"}'),
    "Azure client secret": _j("client", "_secret = \"", "Zq9~", "abcdefghij", "\""),
    "APM dataKey literal": _j("Authorization: data", "Key ", "B" * 24),
    "email address": _j("jane.doe", "@", "corp", ".net"),
}

CLEAN = [
    _j("arn:aws:iam::", "<ACCOUNT_ID>", ":role/x"),
    _j("account_id = \"${", "AWS_ACCOUNT_ID}\""),
    _j("ocid", "1.tenancy.oc1..", "<TENANCY_OCID>"),
    _j("ip = ", "192.0.", "2.10"),
    _j("ip = ", "198.51.", "100.7"),
    _j("ip = ", "203.0.", "113.9"),
    _j("ip = ", "10.", "1.2.3"),
    _j("ip = ", "172.", "16.0.4"),
    _j("ip = ", "192.", "168.1.1"),
    _j("ip = ", "127.", "0.0.1"),
    _j("ip = ", "0.", "0.0.0"),
    _j("version = ", "1.2.3.4.5"),
    _j("mail", "@", "example", ".com"),
    _j("mail", "@", "example", ".org"),
    _j("noreply", "@", "corp", ".net"),
    _j("dataKey ", "<APM_DATAKEY>"),
]


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _gate(repo, mode):
    return subprocess.run(["bash", str(GATE), mode], cwd=repo, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


@pytest.mark.parametrize("name", sorted(CASES))
def test_pattern_is_caught_in_all_mode(repo, name):
    value = CASES[name]
    (repo / "f.txt").write_text("ok line\n" + value + "\n")
    _git(repo, "add", "f.txt")
    result = _gate(repo, "--all")
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert f"f.txt:2: {name}" in out
    assert value not in out


def test_staged_mode_reports_only_added_lines(repo):
    old = CASES["email address"]
    (repo / "f.txt").write_text(old + "\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "seed", "--no-verify")
    (repo / "f.txt").write_text(old + "\nclean line\n" + CASES["AWS access key"] + "\n")
    _git(repo, "add", "f.txt")
    result = _gate(repo, "--staged")
    out = result.stdout + result.stderr
    assert result.returncode == 1
    assert "f.txt:3: AWS access key" in out
    assert "f.txt:1:" not in out
    assert CASES["AWS access key"] not in out


def test_staged_mode_clean_when_nothing_sensitive(repo):
    (repo / "f.txt").write_text("hello\n")
    _git(repo, "add", "f.txt")
    assert _gate(repo, "--staged").returncode == 0


def test_placeholders_documentation_and_private_ranges_pass(repo):
    (repo / "ok.txt").write_text("\n".join(CLEAN) + "\n")
    _git(repo, "add", "ok.txt")
    result = _gate(repo, "--all")
    assert result.returncode == 0, result.stdout + result.stderr


def test_fixture_directory_is_still_scanned(repo):
    fixtures = repo / "tests" / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "x.json").write_text(CASES["AWS access key"] + "\n")
    _git(repo, "add", "-A")
    result = _gate(repo, "--all")
    assert result.returncode == 1
    assert "tests/fixtures/x.json:1: AWS access key" in result.stderr + result.stdout


def test_local_pattern_file_is_honoured_without_echoing_value(repo):
    token = _j("corp", "-internal-", "zebra", "42")
    (repo / ".redaction-patterns.local").write_text("# private\n" + _j("internal-", "zebra", "[0-9]+") + "\n")
    (repo / "f.txt").write_text("see " + token + "\n")
    _git(repo, "add", "f.txt")
    result = _gate(repo, "--all")
    out = result.stdout + result.stderr
    assert result.returncode == 1
    assert "f.txt:1: local pattern" in out
    assert token not in out


def test_usage_error_without_mode(repo):
    assert subprocess.run(["bash", str(GATE)], cwd=repo, capture_output=True).returncode == 2


def test_install_hooks_creates_working_pre_commit(repo):
    (repo / "scripts").mkdir()
    for name in ("redaction_gate.sh", "install_hooks.sh"):
        (repo / "scripts" / name).write_text((ROOT / "scripts" / name).read_text())
    _git(repo, "config", "core.hooksPath", str(repo / ".git" / "hooks"))
    subprocess.run(["bash", "scripts/install_hooks.sh"], cwd=repo, check=True, capture_output=True)
    hook = repo / ".git" / "hooks" / "pre-commit"
    assert hook.exists() and "--staged" in hook.read_text()
    (repo / "f.txt").write_text(CASES["AWS access key"] + "\n")
    _git(repo, "add", "f.txt")
    blocked = subprocess.run(["git", "commit", "-qm", "x"], cwd=repo, capture_output=True, text=True)
    assert blocked.returncode != 0


def test_this_repository_is_clean():
    result = subprocess.run(["bash", str(GATE), "--all"], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
