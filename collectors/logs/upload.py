"""Shared catalog discovery and fail-closed OCI Log Analytics upload."""

from __future__ import annotations

import hashlib
import io
from typing import Any


class LoganUploader:
    def __init__(self, config: Any, logan: Any):
        self.config, self.logan = config, logan
        self._source_names: dict[str, str] = {}

    def source_name(self, source_name: str) -> str:
        """Resolve the catalog display label to the namespace's internal name."""
        if source_name in self._source_names:
            return self._source_names[source_name]
        group = self.logan.get_log_analytics_log_group(self.config.namespace, self.config.log_group_ocid).data
        compartment = getattr(group, "compartment_id", None)
        if not compartment:
            raise RuntimeError("log group compartment unavailable")
        matches, seen = [], set()
        page = None
        for _ in range(1000):
            response = self.logan.list_sources(self.config.namespace, compartment, **({"page": page} if page else {}))
            for source in response.data.items:
                if source.display_name == source_name:
                    matches.append(source)
            page = (response.headers or {}).get("opc-next-page")
            if not page:
                break
            if page in seen:
                raise RuntimeError("repeated source discovery page")
            seen.add(page)
        else:
            raise RuntimeError("source discovery exceeded page bound")
        if len(matches) != 1 or not matches[0].name:
            raise RuntimeError("catalog source missing or ambiguous")
        self._source_names[source_name] = matches[0].name
        return matches[0].name

    def upload(self, payload: bytes, identity: str, *, source_name: str, filename_prefix: str) -> None:
        digest = hashlib.sha256(identity.encode("utf-8") + b"\0" + payload).hexdigest()
        response = self.logan.upload_log_file(
            namespace_name=self.config.namespace, log_source_name=self.source_name(source_name),
            filename=f"{filename_prefix}-{digest}.ndjson", opc_meta_loggrpid=self.config.log_group_ocid,
            upload_log_file_body=io.BytesIO(payload), content_type="application/octet-stream",
            opc_retry_token=digest,
        )
        status = getattr(response, "status", None)
        if type(status) is not int or not 200 <= status < 300:
            raise RuntimeError("upload not accepted")

