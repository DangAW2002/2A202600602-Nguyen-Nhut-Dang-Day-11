"""
Lab 11 — Monitoring & Alerts Component
"""
from collections import deque

class SystemMonitor:
    """
    Monitors request safety statistics and fires alerts if failure rate thresholds are exceeded.
    """

    def __init__(self, window_size: int = 10, alert_threshold: float = 0.3):
        self.window_size = window_size
        self.alert_threshold = alert_threshold
        self.recent_statuses = deque(maxlen=window_size)
        self.rate_limit_hits = 0
        self.input_blocks = 0
        self.output_blocks = 0
        self.total_requests = 0

    def record_request(self, blocked: bool, reason: str = None):
        """
        Record a request outcome.
        blocked: True if blocked by any layer, False if allowed.
        reason: 'rate_limiter', 'input_guardrail', 'output_guardrail', 'judge', or None.
        """
        self.total_requests += 1
        self.recent_statuses.append(1 if blocked else 0)

        if blocked:
            if reason == "rate_limiter":
                self.rate_limit_hits += 1
            elif reason and "input" in reason:
                self.input_blocks += 1
            elif reason and ("output" in reason or "judge" in reason):
                self.output_blocks += 1

    def get_failure_rate(self) -> float:
        """Calculate the failure (block) rate in the active window."""
        if not self.recent_statuses:
            return 0.0
        return sum(self.recent_statuses) / len(self.recent_statuses)

    def should_alert(self) -> bool:
        """Trigger an alert if failure rate meets or exceeds the threshold."""
        # Check alert only when we have at least 3 requests in history to prevent noise
        if len(self.recent_statuses) < 3:
            return False
        return self.get_failure_rate() >= self.alert_threshold

    def get_metrics(self) -> dict:
        """Retrieve current monitoring metrics."""
        failure_rate = self.get_failure_rate()
        alert = self.should_alert()
        return {
            "total_requests": self.total_requests,
            "rate_limit_hits": self.rate_limit_hits,
            "input_blocks": self.input_blocks,
            "output_blocks": self.output_blocks,
            "failure_rate_percent": int(failure_rate * 100),
            "should_alert": alert,
            "alert_message": "⚠️ SYSTEM ALERT: High block rate detected! Potential adversarial attack underway." if alert else ""
        }
