"""Run with python -m collectors.logs; all deployment settings are external."""

import argparse
import logging
import os
import time

from .config import CollectorConfig
from .runtime import build_collector, suppress_sdk_payload_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="poll once and exit nonzero if retry is required")
    parser.add_argument("--source", choices=["aws", "azure", "gcp"], default="aws", help="source adapter (default: aws)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    suppress_sdk_payload_logging()
    from collectors.common.federation import federation_environment
    try:
        with federation_environment(args.source, os.environ) as env:
            return run_source(args, env, parser)
    except Exception:
        logging.error("ingest_configuration_or_identity_unavailable")
        return 2


def run_source(args, env, parser) -> int:
    if args.source == "azure":
        if args.once:
            parser.error("--once is supported by the AWS and GCP queue adapters only")
        from .sources.azure_eventhub import AzureCollectorConfig, build_azure_runtime, receive_azure
        try:
            adapter, consumer, credential = build_azure_runtime(AzureCollectorConfig.from_env(env), env=env)
            try:
                return receive_azure(adapter, consumer)
            finally:
                credential.close()
        except Exception:
            logging.error("ingest_configuration_or_identity_unavailable")
            return 2
    if args.source == "gcp":
        from .sources.gcp_pubsub import GCPCollectorConfig, build_gcp_runtime
        try:
            collector = build_gcp_runtime(GCPCollectorConfig.from_env(env), env=env)
        except Exception:
            logging.error("ingest_configuration_or_identity_unavailable")
            return 2
        try:
            return poll_collector(collector, once=args.once)
        finally:
            try:
                collector.subscriber.close()
            finally:
                collector.publisher.stop()
    try:
        collector = build_collector(CollectorConfig.from_env(env))
    except Exception:
        logging.error("ingest_configuration_or_identity_unavailable")
        return 2
    return poll_collector(collector, once=args.once)


def _log_summary(counts) -> None:
    """One safe line per poll: integer counters only, by outcome and reason."""
    fields = " ".join(f"{key}={int(value)}" for key, value in sorted(counts.items()) if isinstance(value, int))
    logging.info("ingest_poll_summary %s", fields)


def poll_collector(collector, *, once: bool) -> int:
    while True:
        try:
            counts = collector.poll_once()
            _log_summary(counts)
            if once:
                return 1 if counts.get("retry") else 0
        except KeyboardInterrupt:
            return 0
        except Exception:
            logging.warning("ingest_poll_failed")
            if once:
                return 1
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
