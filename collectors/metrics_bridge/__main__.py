"""Run the metrics bridge: `python -m collectors.metrics_bridge` (config from env)."""
from __future__ import annotations

import logging
import sys

from .server import BridgeConfig, ConfigError, build_server


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = BridgeConfig.from_env()
    except ConfigError as exc:
        print(f"metrics-bridge: {exc}", file=sys.stderr)
        return 2
    server = build_server(config)
    logging.getLogger(__name__).info(
        "listening on %s:%d (auth=%s)", config.listen_host, config.listen_port, config.auth_mode)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
