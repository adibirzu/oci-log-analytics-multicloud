# Contributing

Thanks for helping. This repository only contains reusable, generic assets.

## Ground rules

- Tests are offline: no network, no cloud or OCI calls, no `kubectl`, no Terraform apply. Inject
  fakes. Build realistic fixture values from parts (for example `"ocid" + "1."`).
- Never commit real OCIDs, account/subscription/project IDs, IPs, namespaces, domains, personal
  names, emails or secrets. Use `<PLACEHOLDER>` or `${VAR}`.
- Do not reference private projects, customers, tenancies or internal knowledge-base numbers.
- Bash must run on macOS bash 3.2.

## Before you push

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'   # or your preferred setup
.venv/bin/python -m pytest -q
bash scripts/redaction_gate.sh --all
bash scripts/install_hooks.sh                               # runs the gate on every commit
```

`tests/test_public_hygiene.py` fails on local paths, private KB references, private project names and
non-example email addresses. `tests/test_redaction_gate.py` covers the gate itself.

## Private terms

Maintainers can add their own organisation-specific terms to the gitignored
`.redaction-patterns.local`; see `.redaction-patterns.local.example`.

## Pull requests

Keep changes focused, add or update tests first, and describe any caveats in the PR.
