"""Load the bundled source catalog module from catalog_data/ (stdlib only, SDKs stay optional)."""

from __future__ import annotations

import importlib.util
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any


@lru_cache(maxsize=1)
def _catalog_module() -> ModuleType:
    path = Path(__file__).resolve().parent / "catalog_data" / "logan_source_catalog.py"
    spec = importlib.util.spec_from_file_location("_mc_logs_logan_catalog", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("canonical source catalog module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_catalog(path: str | Path | None = None) -> Any:
    return _catalog_module().load_catalog(path)
