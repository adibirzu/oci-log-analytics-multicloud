# Agent and worker rules

Read `docs/ARCHITECTURE.md` and the task row in `docs/TASKS.md` before coding. Mirror the existing style.

- Test-driven and offline only: no network, no OCI, AWS, Azure, GCP, kubectl or Terraform apply calls. Inject fakes. `terraform validate`/`fmt` are allowed only where a provider cache already exists.
- Never inline real OCIDs, account/subscription/project IDs, IPs, namespaces, domains, personal names or secret values. Use `<PLACEHOLDER>` or `${VAR}`. Build realistic fixture values from parts (`"ocid" + "1."`).
- No static cloud keys. Cloud side uses OIDC federation; OCI side uses resource/instance principal. Operator auth only behind `INGEST_ALLOW_OPERATOR_AUTH=true`.
- Secrets travel through SDK calls or `0600` temp files with a cleanup trap; never argv, never logs, never Terraform state.
- Destructive steps (destroy, bucket empty) require a saved, reviewed plan and an explicit typed confirmation in the CLI.
- Bash must run on macOS bash 3.2.
- Never `git add`, commit, push, stash or reset; the orchestrator owns git and `docs/TASKS.md`.
- Only touch the files your task names; other workers edit the tree concurrently.
- Tests: `python3.11 -m pytest <tests> -q` from the repo root (venv: `.venv/bin/python` if present).
- Finish with a report under 25 lines: files changed, exact test command and result, open caveats.
