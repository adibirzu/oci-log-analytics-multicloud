"""Load and validate mcctl.yaml (placeholders only; ids resolved from env)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import yaml

CLOUDS = ("aws", "azure", "gcp")
SIGNALS = ("logs", "metrics", "apm")
DEFAULT_CONFIG = "mcctl.yaml"
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
REPO_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class OciTarget:
    profile: str
    tenancy_name: str
    compartment_name: str
    # profile -> the only compartment mutations are allowed in (empty = no guard)
    protected_profiles: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CloudConfig:
    cloud: str
    account: str
    region: str
    signals: tuple[str, ...]
    var_file: str | None
    oci: OciTarget
    stop_command: tuple[str, ...] | None = None

    def module_dir(self, signal: str, root: Path = REPO_ROOT) -> Path:
        return root / "modules" / signal / self.cloud / "terraform"


@dataclass(frozen=True)
class Config:
    clouds: Mapping[str, CloudConfig]

    def cloud(self, name: str) -> CloudConfig:
        if name not in CLOUDS:
            raise ConfigError(f"unknown cloud {name!r}; expected one of {', '.join(CLOUDS)}")
        if name not in self.clouds:
            raise ConfigError(f"cloud {name!r} is not configured")
        return self.clouds[name]


def resolve_env(value: str, env: Mapping[str, str]) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if not env.get(name):
            raise ConfigError(f"environment variable {name} is not set")
        return env[name]

    return _VAR.sub(sub, value)


def _req(d: Mapping, key: str, where: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v:
        raise ConfigError(f"{where}.{key} is required")
    return v


def _protected_profiles(raw) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(isinstance(k, str) and isinstance(v, str) and k and v
                                           for k, v in raw.items()):
        raise ConfigError("'protected_profiles' must map OCI profile name -> allowed compartment name")
    return dict(raw)


def parse_config(raw: Mapping, env: Mapping[str, str]) -> Config:
    clouds_raw = (raw or {}).get("clouds")
    if not isinstance(clouds_raw, dict) or not clouds_raw:
        raise ConfigError("'clouds' mapping is required")
    protected = _protected_profiles((raw or {}).get("protected_profiles"))
    out: dict[str, CloudConfig] = {}
    for name, c in clouds_raw.items():
        if name not in CLOUDS:
            raise ConfigError(f"unknown cloud {name!r}")
        where = f"clouds.{name}"
        signals = tuple(c.get("signals") or ())
        bad = [s for s in signals if s not in SIGNALS]
        if bad or not signals:
            raise ConfigError(f"{where}.signals must be a non-empty subset of {SIGNALS}; got {list(signals)}")
        oci = c.get("oci") or {}
        stop = (c.get("collector") or {}).get("stop_command")
        if stop is not None and (not isinstance(stop, list) or not stop or not all(isinstance(x, str) for x in stop)):
            raise ConfigError(f"{where}.collector.stop_command must be a non-empty list of strings")
        out[name] = CloudConfig(
            cloud=name,
            account=resolve_env(_req(c, "account", where), env),
            region=_req(c, "region", where),
            signals=signals,
            var_file=c.get("var_file"),
            stop_command=tuple(stop) if stop else None,
            oci=OciTarget(
                profile=_req(oci, "profile", f"{where}.oci"),
                tenancy_name=_req(oci, "tenancy_name", f"{where}.oci"),
                compartment_name=_req(oci, "compartment_name", f"{where}.oci"),
                protected_profiles=protected,
            ),
        )
    return Config(clouds=out)


def load_config(path: str | os.PathLike | None = None, env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    p = Path(path or env.get("MCCTL_CONFIG") or DEFAULT_CONFIG)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    return parse_config(yaml.safe_load(p.read_text()), env)
