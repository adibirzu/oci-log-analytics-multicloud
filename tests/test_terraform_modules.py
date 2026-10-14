"""Static, offline checks for the per-cloud log pipeline Terraform modules."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULES = [ROOT / "modules" / "logs" / cloud / "terraform" for cloud in ("aws", "azure", "gcp")]
REQUIRED = ("main.tf", "variables.tf", "outputs.tf", "versions.tf")
ACCOUNT_ID = re.compile(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])")
OCID = re.compile(r"ocid1\.[a-z]+\.oc1\.")
FORCE_DESTROY = re.compile(r"force_destroy\s*=\s*true")


def _files(module):
    return [p for p in module.rglob("*") if p.is_file() and ".terraform" not in p.parts
            and p.suffix in {".tf", ".hcl", ".tfvars", ".md"}]


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.parts[-2])
def test_module_has_standard_files(module):
    missing = [name for name in REQUIRED if not (module / name).is_file()]
    assert not missing, missing


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.parts[-2])
def test_module_has_no_embedded_identifiers_or_force_destroy(module):
    offenders = []
    for path in _files(module):
        text = path.read_text(encoding="utf-8")
        for label, pattern in (("account id", ACCOUNT_ID), ("ocid", OCID), ("force_destroy", FORCE_DESTROY)):
            if pattern.search(text):
                offenders.append(f"{path.relative_to(ROOT)}: {label}")
    assert not offenders, offenders
