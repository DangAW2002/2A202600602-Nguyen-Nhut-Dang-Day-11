"""
Lab 11 — Rate Limiter Component
"""
import time
from collections import defaultdict, deque
from google.adk.plugins import base_plugin
from google.genai import types

class RateLimitPlugin(base_plugin.BasePlugin):
    """
    Sliding window rate limiter plugin for ADK.
    Limits user requests in a sliding time window.
    """

    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        super().__init__(name="rate_limiter")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.user_windows = defaultdict(deque)

    async def before_agent_callback(self, *, agent, callback_context) -> types.Content | None:
        """
        Intercepts user message and blocks if rate limit is exceeded.
        """
        user_id = getattr(callback_context, "user_id", "student") or "student"
        now = time.time()
        window = self.user_windows[user_id]

        # Clean up expired timestamps from the front of the deque
        while window and window[0] <= now - self.window_seconds:
            window.popleft()

        # Check if rate limit has been exceeded
        if len(window) >= self.max_requests:
            wait_time = max(1, int(window[0] + self.window_seconds - now))
            block_msg = f"Blocked: Rate limit exceeded. Please wait {wait_time} seconds before retrying."

            # Log directly to audit log
            from datetime import datetime
            session_id = "default"
            if callback_context and callback_context.session:
                session_id = callback_context.session.id or "default"

            # Extract query text safely
            query_text = "Unknown"
            if callback_context and callback_context.user_content:
                parts = getattr(callback_context.user_content, "parts", None)
                if parts:
                    query_text = "".join([part.text for part in parts if hasattr(part, "text") and part.text])

            from guardrails.audit_log import AuditLogPlugin
            AuditLogPlugin().write_log({
                "timestamp": datetime.now().isoformat(),
                "session_id": session_id,
                "input": query_text,
                "output": block_msg,
                "blocked": True,
                "blocker_layer": "Rate Limiter",
                "latency_ms": 0
            })

            return types.Content(
                role="model",
                parts=[types.Part.from_text(text=block_msg)]
            )

        # Record this request's timestamp
        window.append(now)
        return None

    def get_remaining_requests(self, user_id: str = "student") -> int:
        """
        Get the number of remaining allowed requests in the current window.
        """
        now = time.time()
        window = self.user_windows[user_id]
        while window and window[0] <= now - self.window_seconds:
            window.popleft()
        return max(0, self.max_requests - len(window))

    def reset(self, user_id: str = None):
        """
        Reset the rate limit window for the user, or all users if user_id is None.
        """
        if user_id is None:
            self.user_windows.clear()
        elif user_id in self.user_windows:
            self.user_windows[user_id].clear()
