"""
Lab 11 — Audit Logging Component
"""
import os
import json
import time
from datetime import datetime
from google.adk.plugins import base_plugin
from google.genai import types

class AuditLogPlugin(base_plugin.BasePlugin):
    """
    Plugin that records inputs, outputs, blocker layers, and latencies.
    Logs are exported to a local JSON file.
    """

    def __init__(self, log_filepath: str = None):
        super().__init__(name="audit_log")
        if log_filepath is None:
            # Set default path under .local directory
            curr_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.dirname(os.path.dirname(curr_dir))
            self.log_filepath = os.path.join(project_root, ".local", "security_audit.json")
        else:
            self.log_filepath = log_filepath

        # Ensure directory exists
        os.makedirs(os.path.dirname(self.log_filepath), exist_ok=True)
        self.request_times = {}

    def _extract_text(self, content) -> str:
        """Helper to extract raw text from ADK Content or string."""
        if isinstance(content, str):
            return content
        text = ""
        if hasattr(content, "parts") and content.parts:
            for part in content.parts:
                if hasattr(part, "text") and part.text:
                    text += part.text
        return text

    async def before_agent_callback(self, *, agent, callback_context) -> types.Content | None:
        """
        Record start time and query text at the beginning of a user request.
        """
        session_id = "default"
        if callback_context and callback_context.session:
            session_id = callback_context.session.id or "default"

        user_message = callback_context.user_content
        self.request_times[session_id] = {
            "start_time": time.time(),
            "input_text": self._extract_text(user_message)
        }
        return None

    async def after_model_callback(self, *, callback_context, llm_response):
        """
        Record the response, calculate latency, check safety status, and write to log.
        """
        session_id = "default"
        if callback_context and callback_context.session:
            session_id = callback_context.session.id or "default"
        now = time.time()

        start_info = self.request_times.pop(session_id, None)
        if start_info:
            latency = int((now - start_info["start_time"]) * 1000)
            input_text = start_info["input_text"]
        else:
            latency = 0
            input_text = "Unknown"

        response_content = llm_response.content if hasattr(llm_response, "content") else llm_response
        response_text = self._extract_text(response_content)

        # Classify blocker layer and status
        blocked = False
        blocker_layer = "None"

        if response_text.startswith("Blocked:"):
            blocked = True
            if "Rate limit" in response_text:
                blocker_layer = "Rate Limiter"
            elif "Prompt injection" in response_text:
                blocker_layer = "Input Guardrail - Injection"
            elif "off-topic" in response_text or "inappropriate" in response_text:
                blocker_layer = "Input Guardrail - Topic"
            elif "safety classifier" in response_text or "safety" in response_text:
                blocker_layer = "LLM Judge"
            else:
                blocker_layer = "Guardrails"
        elif "[REDACTED]" in response_text:
            blocker_layer = "Output Guardrail - PII Redacted"

        entry = {
            "timestamp": datetime.now().isoformat(),
            "session_id": session_id,
            "input": input_text,
            "output": response_text,
            "blocked": blocked,
            "blocker_layer": blocker_layer,
            "latency_ms": latency
        }

        self.write_log(entry)
        return llm_response

    def write_log(self, entry: dict):
        """Write a log entry to the JSON lines file."""
        try:
            with open(self.log_filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"Error writing to audit log: {e}")

    def read_all_logs(self) -> list[dict]:
        """Read all logged entries from the audit file."""
        if not os.path.exists(self.log_filepath):
            return []
        logs = []
        try:
            with open(self.log_filepath, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        logs.append(json.loads(line))
        except Exception as e:
            print(f"Error reading audit log: {e}")
        return logs

    def clear_logs(self):
        """Clear the current audit logs."""
        if os.path.exists(self.log_filepath):
            try:
                os.remove(self.log_filepath)
            except Exception:
                pass
