"""Node monitoring — peer liveness + in-daemon degraded-state detection.

See `linkedin/models.py:DaemonHeartbeat` and the conf.py "Node monitoring"
section. Wired into the daemon by `run_daemon()`:

  - `NodeMonitor` thread — heartbeat + peer scan (catches "daemon dead").
  - `TaskFailureTracker` + `check_listener_heartbeat` — degraded checks
    run in the task loop (catches "daemon alive but not working").

All alerts route to the ops Slack channel via `notify_degraded`.
"""
from linkedin.monitoring.degraded import (
    TaskFailureTracker,
    check_listener_heartbeat,
)
from linkedin.monitoring.node_monitor import NodeMonitor, clear_heartbeat

__all__ = [
    "NodeMonitor",
    "clear_heartbeat",
    "TaskFailureTracker",
    "check_listener_heartbeat",
]
