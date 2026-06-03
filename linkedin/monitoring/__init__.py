"""Node monitoring — peer liveness + in-daemon degraded-state detection.

See `linkedin/models.py:DaemonHeartbeat` and the conf.py "Node monitoring"
section. Wired into the daemon by `run_daemon()`:

  - `NodeMonitor` thread — heartbeat + peer scan (catches "daemon dead").
  - `NodeMonitor` activity scan — fresh heartbeat but stale/no outbound
    ActionLog progress for expected senders.
  - `TaskFailureTracker` — task-loop degraded check for repeated handler
    failures.

All alerts route to the ops Slack channel via `notify_degraded`.
"""
from linkedin.monitoring.degraded import TaskFailureTracker
from linkedin.monitoring.node_monitor import NodeMonitor, clear_heartbeat

__all__ = [
    "NodeMonitor",
    "clear_heartbeat",
    "TaskFailureTracker",
]
