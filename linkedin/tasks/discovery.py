"""Daemon task entrypoint for standalone profile discovery."""

from linkedin.discovery.collector import handle_discovery

__all__ = ["handle_discovery"]
