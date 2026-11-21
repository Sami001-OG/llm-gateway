#!/usr/bin/env python3
"""Entrypoint: python run.py --config config.yaml"""

from __future__ import annotations

import argparse
import logging
import os

import uvicorn

from gateway.app import create_app_from_file
from gateway.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Multi-account LLM API gateway (OpenAI + Anthropic)."
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("GATEWAY_CONFIG", "config.yaml"),
        help="Path to YAML config (default: config.yaml or $GATEWAY_CONFIG)",
    )
    parser.add_argument("--host", help="Override server host from config")
    parser.add_argument("--port", type=int, help="Override server port from config")
    parser.add_argument("--log-level", help="Override log level from config")
    args = parser.parse_args()

    cfg = load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, args.log_level or cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    )

    host = args.host or cfg.host
    port = args.port or cfg.port

    if not cfg.auth_tokens:
        logging.getLogger("gateway").warning(
            "No server.auth_tokens configured - gateway is OPEN to everyone. "
            "Set auth_tokens before exposing it publicly."
        )
    if not cfg.admin_tokens:
        logging.getLogger("gateway").warning(
            "No server.admin_tokens configured - admin endpoints are OPEN."
        )

    app = create_app_from_file(args.config)
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=True)


if __name__ == "__main__":
    main()
