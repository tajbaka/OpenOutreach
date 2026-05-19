"""Daemon startup environment-variable warnings."""
from __future__ import annotations

import logging
import os

from linkedin.env_spec import ENV_VARS

logger = logging.getLogger(__name__)


def check_env_vars() -> None:
    """Log a grouped summary of missing env vars without aborting startup."""
    missing_required = []
    missing_optional = []

    for var in ENV_VARS:
        if os.getenv(var.name, "").strip():
            continue
        if var.required:
            missing_required.append(var)
        elif var.default is None:
            missing_optional.append(var)

    if not missing_required and not missing_optional:
        logger.debug("env check: all required env vars present")
        return

    lines = ["env check: environment variable summary"]
    if missing_required:
        lines.append(f"  MISSING REQUIRED ({len(missing_required)}):")
        for var in missing_required:
            lines.append(f"    - {var.name} [{var.group}] - {var.description}")
    if missing_optional:
        lines.append(f"  missing optional integrations ({len(missing_optional)}):")
        for var in missing_optional:
            lines.append(f"    - {var.name} [{var.group}] - {var.description}")
    lines.append("  (declared in linkedin/env_spec.py)")
    summary = "\n".join(lines)

    if missing_required:
        logger.warning(summary)
    else:
        logger.info(summary)
