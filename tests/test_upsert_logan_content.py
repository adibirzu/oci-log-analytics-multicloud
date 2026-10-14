"""Offline tests for scripts/upsert_logan_content.py using a stateful fake LA client."""

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("upsert_logan_content", ROOT / "scripts" / "upsert_logan_content.py")
upsert = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = upsert
SPEC.loader.exec_module(upsert)

NS = "fake-namespace"
COMPARTMENT = "fake-compartment"
WRITE_CALLS = {"upsert_field", "upsert_parser", "upsert_source"}


class FakeLAClient:
    """In-memory Log Analytics stand-in. Records every call; writes mutate state."""

    def __init__(self, fields=(), parsers=(), sources=()):
        self.fields = {x["display_name"]: dict(x) for x in fields}
        self.parsers = {x["display_name"]: dict(x) for x in parsers}
        self.sources = {x["display_name"]: dict(x) for x in sources}
        self.calls: list[tuple[str, dict]] = []
        self._etag = 0

    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))

    def _next_etag(self):
        self._etag += 1
        return f"etag-{self._etag}"

    # reads
    def list_fields(self, namespace, **kwargs):
        self._record("list_fields")
        return [dict(x) for x in self.fields.values()]

    def list_parsers(self, namespace, **kwargs):
        self._record("list_parsers")
        return [dict(x) for x in self.parsers.values()]

    def list_sources(self, namespace, compartment, **kwargs):
        self._record("list_sources")
        return [dict(x) for x in self.sources.values()]

    def get_field(self, namespace, name, **kwargs):
        self._record("get_field")
        return next(dict(x) for x in self.fields.values() if x["name"] == name)

    def get_parser(self, namespace, name, **kwargs):
        self._record("get_parser")
        return next(dict(x) for x in self.parsers.values() if x["name"] == name)

    def get_source(self, namespace, name, compartment, **kwargs):
        self._record("get_source")
        return next(dict(x) for x in self.sources.values() if x["name"] == name)

    # writes
    def _upsert(self, store, kind, namespace, details, if_match=None, opc_retry_token=None, **kwargs):
        self._record(f"upsert_{kind}", if_match=if_match, opc_retry_token=opc_retry_token)
        body = upsert._plain(details)
        display = body["display_name"]
        existing = store.get(display)
        if existing is not None and if_match != existing["etag"]:
            raise RuntimeError("etag mismatch")
        if existing is None and if_match is not None:
            raise RuntimeError("if_match without existing resource")
        name = existing["name"] if existing else (body.get("name") or f"GEN_{display.upper().replace(' ', '_')}")
        stored = {**body, "name": name, "etag": self._next_etag()}
        if kind in {"parser", "source"}:
            stored["edit_version"] = (existing or {}).get("edit_version", 0) + 1
        store[display] = stored
        return SimpleNamespace(data={"name": name}, headers={})

    def upsert_field(self, namespace, details, **kwargs):
        return self._upsert(self.fields, "field", namespace, details, **kwargs)

    def upsert_parser(self, namespace, details, **kwargs):
        return self._upsert(self.parsers, "parser", namespace, details, **kwargs)

    def upsert_source(self, namespace, details, **kwargs):
        return self._upsert(self.sources, "source", namespace, details, **kwargs)

    def write_calls(self):
        return [name for name, _ in self.calls if name in WRITE_CALLS]


def _external_refs(resources):
    """Parser storage fields the bundle does not define; these must be built-ins."""
    own = {x.display_name for x in resources if x.kind == "field"} | {x.name for x in resources if x.kind == "field" and x.name}
    refs = []
    for item in resources:
        if item.kind != "parser":
            continue
        for mapping in item.body.get("field_maps", []):
            ref = mapping.get("storage_field_name")
            if ref and ref not in own and ref not in refs:
                refs.append(ref)
    return refs


def _builtin_fields(resources):
    fields = [{"display_name": "Time", "name": "SYS_TIME", "data_type": "TIMESTAMP", "is_system": True, "etag": "sys"}]
    for ref in _external_refs(resources):
        if ref != "Time":
            fields.append({"display_name": ref, "name": f"SYS_{ref.upper().replace(' ', '_')}", "data_type": "STRING", "is_system": True, "etag": "sys"})
    return fields


def _empty_state(resources):
    return FakeLAClient(fields=_builtin_fields(resources))


def _resources(bundle="aws_cloudtrail_ocsf"):
    return upsert.load_content(bundle=bundle)


def _actions(result):
    return {(x["kind"], x["display_name"]): x["action"] for x in result["actions"]}


def test_plan_on_empty_state_creates_every_item():
    resources = _resources()
    client = _empty_state(resources)

    result = upsert.plan_content(client, NS, COMPARTMENT, resources)

    assert result["counts"] == {"create": len(resources), "update": 0, "unchanged": 0}
    assert set(_actions(result).values()) == {"create"}


def test_plan_reports_update_when_source_field_differs_and_nothing_else():
    resources = _resources()
    client = _empty_state(resources)
    upsert.apply_content(client, NS, COMPARTMENT, resources)
    source_name = next(x.display_name for x in resources if x.kind == "source")
    client.sources[source_name]["description"] = "stale description"

    result = upsert.plan_content(client, NS, COMPARTMENT, resources)

    actions = _actions(result)
    assert actions[("source", source_name)] == "update"
    assert {v for k, v in actions.items() if k != ("source", source_name)} == {"unchanged"}


def test_plan_is_no_op_on_identical_state():
    resources = _resources()
    client = _empty_state(resources)
    upsert.apply_content(client, NS, COMPARTMENT, resources)

    result = upsert.plan_content(client, NS, COMPARTMENT, resources)

    assert result["counts"] == {"create": 0, "update": 0, "unchanged": len(resources)}


def test_second_apply_run_writes_nothing():
    resources = _resources()
    client = _empty_state(resources)
    upsert.apply_content(client, NS, COMPARTMENT, resources)
    client.calls.clear()

    result = upsert.apply_content(client, NS, COMPARTMENT, resources)

    assert result["counts"] == {"created": 0, "updated": 0, "unchanged": len(resources)}
    assert client.write_calls() == []


def test_apply_writes_only_planned_updates():
    resources = _resources()
    client = _empty_state(resources)
    upsert.apply_content(client, NS, COMPARTMENT, resources)
    source_name = next(x.display_name for x in resources if x.kind == "source")
    client.sources[source_name]["description"] = "stale description"
    pre_write_etag = client.sources[source_name]["etag"]
    client.calls.clear()

    result = upsert.apply_content(client, NS, COMPARTMENT, resources)

    assert result["counts"]["updated"] == 1
    assert client.write_calls() == ["upsert_source"]
    write_kwargs = [kw for name, kw in client.calls if name == "upsert_source"][0]
    assert write_kwargs["if_match"] == pre_write_etag
    assert write_kwargs["opc_retry_token"]


def test_apply_on_empty_state_creates_each_item_once_in_kind_order():
    resources = _resources()
    client = _empty_state(resources)

    result = upsert.apply_content(client, NS, COMPARTMENT, resources)

    assert result["counts"]["created"] == len(resources)
    kinds = [name.replace("upsert_", "") for name in client.write_calls()]
    assert kinds == [x.kind for x in resources]


def test_dry_run_makes_zero_write_calls():
    resources = _resources()
    client = _empty_state(resources)

    upsert.plan_content(client, NS, COMPARTMENT, resources)

    assert client.write_calls() == []
    assert client.calls  # reads did happen


def test_main_defaults_to_dry_run_and_makes_no_writes(monkeypatch, capsys):
    client = _empty_state(_resources())
    monkeypatch.setattr(upsert, "build_client", lambda: client)

    assert upsert.main(["--namespace", NS, "--compartment-id", COMPARTMENT]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry-run"
    assert client.write_calls() == []
    assert NS not in capsys.readouterr().out and COMPARTMENT not in json.dumps(out)


def test_main_apply_flag_performs_writes(monkeypatch, capsys):
    client = _empty_state(_resources())
    monkeypatch.setattr(upsert, "build_client", lambda: client)

    assert upsert.main(["--apply", "--namespace", NS, "--compartment-id", COMPARTMENT]) == 0

    assert json.loads(capsys.readouterr().out)["mode"] == "apply"
    assert client.write_calls()


def test_bundle_filter_limits_plan_to_selected_bundle(monkeypatch, capsys):
    client = _empty_state(_resources("azure_activity_ocsf"))
    monkeypatch.setattr(upsert, "build_client", lambda: client)

    assert upsert.main(["--bundle", "azure_activity_ocsf", "--namespace", NS, "--compartment-id", COMPARTMENT]) == 0

    out = json.loads(capsys.readouterr().out)
    expected = _resources("azure_activity_ocsf")
    assert out["bundle"] == "azure_activity_ocsf"
    assert len(out["actions"]) == len(expected)
    assert {x["display_name"] for x in out["actions"]} == {x.display_name for x in expected}


def test_load_content_returns_only_requested_bundle():
    for bundle in upsert.BUNDLES:
        content = json.loads((ROOT / "content" / "logan" / f"{bundle}.source.json").read_text())["source"]
        parser = json.loads((ROOT / "content" / "logan" / f"{bundle}.parser.json").read_text())["parser"]
        loaded = [x.display_name for x in _resources(bundle)]
        assert content["display_name"] in loaded
        assert parser["display_name"] in loaded
        assert not any(x.kind == "source" and x.display_name != content["display_name"] for x in _resources(bundle))


def test_unknown_bundle_is_rejected():
    with pytest.raises(SystemExit):
        upsert.main(["--bundle", "not_a_bundle", "--namespace", NS, "--compartment-id", COMPARTMENT])


def test_error_output_names_exception_class_only(monkeypatch, capsys):
    class Boom(Exception):
        pass

    def fail():
        raise Boom(f"tenancy {COMPARTMENT} namespace {NS}")

    monkeypatch.setattr(upsert, "build_client", fail)

    assert upsert.main(["--namespace", NS, "--compartment-id", COMPARTMENT]) == 1
    err = capsys.readouterr().err
    assert "Boom" in err and NS not in err and COMPARTMENT not in err


def test_resource_bodies_are_not_mutated_by_planning():
    resources = _resources()
    before = copy.deepcopy([x.body for x in resources])
    upsert.plan_content(_empty_state(resources), NS, COMPARTMENT, resources)
    assert [x.body for x in resources] == before
