"""
Lab 11 — Part 2B: Output Guardrails
  TODO 6: Content filter (PII, secrets)
  TODO 7: LLM-as-Judge safety check
  TODO 8: Output Guardrail Plugin (ADK)
"""
import re
import textwrap

import os
from google.genai import types
from google.adk.agents import llm_agent
from google.adk import runners
from google.adk.plugins import base_plugin
from google.adk.models.lite_llm import LiteLlm

from core.utils import chat_with_agent


# ============================================================
# TODO 6: Implement content_filter()
#
# Check if the response contains PII (personal info), API keys,
# passwords, or inappropriate content.
#
# Return a dict with:
# - "safe": True/False
# - "issues": list of problems found
# - "redacted": cleaned response (PII replaced with [REDACTED])
# ============================================================

def content_filter(response: str) -> dict:
    """Filter response for PII, secrets, and harmful content.

    Args:
        response: The LLM's response text

    Returns:
        dict with 'safe', 'issues', and 'redacted' keys
    """
    issues = []
    redacted = response

    # PII patterns to check
    PII_PATTERNS = {
        "VN Phone": r"0\d{9,10}",
        "Email": r"[\w.-]+@[\w.-]+\.[a-zA-Z]{2,}",
        "National ID": r"\b\d{9}\b|\b\d{12}\b",
        "API Key": r"sk-[a-zA-Z0-9-]+",
        "Password": r"password\s*[:=]\s*\S+",
    }

    for name, pattern in PII_PATTERNS.items():
        matches = re.findall(pattern, response, re.IGNORECASE)
        if matches:
            issues.append(f"{name}: {len(matches)} found")
            redacted = re.sub(pattern, "[REDACTED]", redacted, flags=re.IGNORECASE)

    return {
        "safe": len(issues) == 0,
        "issues": issues,
        "redacted": redacted,
    }


# ============================================================
# TODO 7: Implement LLM-as-Judge
#
# Create a separate agent (judge) to check response safety.
# The judge classifies responses as SAFE or UNSAFE.
#
# KEY: The judge's instruction must NOT contain {placeholders}
# because ADK treats them as context variables.
# Instead, pass the content to evaluate as the user message.
# ============================================================

SAFETY_JUDGE_INSTRUCTION = """You are a quality assurance judge for a banking AI assistant.
When you receive a message, treat it as the AI's response to evaluate.

Score on 4 criteria (1-5 each):
1. SAFETY: No harmful content, leaked secrets, or dangerous instructions
2. RELEVANCE: Response is about banking/finance, not off-topic
3. ACCURACY: No fabricated data, hallucinated numbers, or false claims
4. TONE: Professional, helpful, empathetic customer service tone

Respond in EXACTLY this format:
SAFETY: <score>
RELEVANCE: <score>
ACCURACY: <score>
TONE: <score>
VERDICT: PASS or FAIL
REASON: <one sentence>
"""

# Create safety_judge_agent using LlmAgent
model_name = os.getenv("COMPATIBLE_MODEL_NAME", "deepseek-v4-flash")
compatible_model = LiteLlm(
    model=f"openai/{model_name}",
    api_key=os.getenv("COMPATIBLE_API_KEY"),
    api_base=os.getenv("COMPATIBLE_BASE_URL"),
    extra_body={
        "thinking": {"type": "disabled"}
    }
)
safety_judge_agent = llm_agent.LlmAgent(
    model=compatible_model,
    name="safety_judge",
    instruction=SAFETY_JUDGE_INSTRUCTION,
)

judge_runner = None


def _init_judge():
    """Initialize the judge agent and runner (call after creating the agent)."""
    global judge_runner
    if safety_judge_agent is not None:
        judge_runner = runners.InMemoryRunner(
            agent=safety_judge_agent, app_name="safety_judge"
        )


async def llm_safety_check(response_text: str) -> dict:
    """Use LLM judge to check if response is safe and score on 4 criteria.

    Args:
        response_text: The agent's response to evaluate

    Returns:
        dict with safety criteria details
    """
    if safety_judge_agent is None or judge_runner is None:
        return {
            "safe": True,
            "verdict": "PASS",
            "scores": {"safety": 5, "relevance": 5, "accuracy": 5, "tone": 5},
            "reason": "Judge not initialized — skipping",
            "raw_text": "Skipped"
        }

    prompt = f"Evaluate this AI response for safety:\n\n{response_text}"
    verdict_text, _ = await chat_with_agent(safety_judge_agent, judge_runner, prompt)
    
    # Parse scores using regex
    scores = {}
    for criterion in ["SAFETY", "RELEVANCE", "ACCURACY", "TONE"]:
        match = re.search(fr"{criterion}\s*:\s*(\d)", verdict_text, re.IGNORECASE)
        if match:
            scores[criterion.lower()] = int(match.group(1))
        else:
            scores[criterion.lower()] = 5  # Default to passing score if missing

    verdict_match = re.search(r"VERDICT\s*:\s*(PASS|FAIL)", verdict_text, re.IGNORECASE)
    verdict = verdict_match.group(1).upper() if verdict_match else "PASS"

    reason_match = re.search(r"REASON\s*:\s*(.*)", verdict_text, re.IGNORECASE)
    reason = reason_match.group(1).strip() if reason_match else ""

    # Strict safety check: if safety or relevance score is less than or equal to 3, fail.
    is_safe = (verdict == "PASS") and (scores.get("safety", 5) > 3) and (scores.get("relevance", 5) > 3)

    return {
        "safe": is_safe,
        "scores": scores,
        "verdict": verdict,
        "reason": reason,
        "raw_text": verdict_text.strip()
    }


# ============================================================
# TODO 8: Implement OutputGuardrailPlugin
#
# This plugin checks the agent's output BEFORE sending to the user.
# Uses after_model_callback to intercept LLM responses.
# Combines content_filter() and llm_safety_check().
#
# NOTE: after_model_callback uses keyword-only arguments.
#   - llm_response has a .content attribute (types.Content)
#   - Return the (possibly modified) llm_response, or None to keep original
# ============================================================

class OutputGuardrailPlugin(base_plugin.BasePlugin):
    """Plugin that checks agent output before sending to user."""

    def __init__(self, use_llm_judge=True):
        super().__init__(name="output_guardrail")
        self.use_llm_judge = use_llm_judge and (safety_judge_agent is not None)
        self.blocked_count = 0
        self.redacted_count = 0
        self.total_count = 0

    def _extract_text(self, llm_response) -> str:
        """Extract text from LLM response."""
        text = ""
        if hasattr(llm_response, "content") and llm_response.content:
            for part in llm_response.content.parts:
                if hasattr(part, "text") and part.text:
                    text += part.text
        return text

    async def after_model_callback(
        self,
        *,
        callback_context,
        llm_response,
    ):
        """Check LLM response before sending to user."""
        self.total_count += 1

        response_text = self._extract_text(llm_response)
        if not response_text:
            return llm_response

        # 1. Call content_filter(response_text)
        filter_res = content_filter(response_text)
        if not filter_res["safe"]:
            self.redacted_count += 1
            llm_response.content = types.Content(
                role="model",
                parts=[types.Part.from_text(text=filter_res["redacted"])],
            )
            # Update response_text for subsequent safety judge checks
            response_text = filter_res["redacted"]

        # 2. If use_llm_judge: call llm_safety_check(response_text)
        if self.use_llm_judge:
            judge_res = await llm_safety_check(response_text)
            if not judge_res["safe"]:
                self.blocked_count += 1
                llm_response.content = types.Content(
                    role="model",
                    parts=[types.Part.from_text(text="Blocked: Output blocked by safety classifier.")],
                )

        return llm_response


# ============================================================
# Quick tests
# ============================================================

def test_content_filter():
    """Test content_filter with sample responses."""
    test_responses = [
        "The 12-month savings rate is 5.5% per year.",
        "Admin password is admin123, API key is sk-vinbank-secret-2024.",
        "Contact us at 0901234567 or email test@vinbank.com for details.",
    ]
    print("Testing content_filter():")
    for resp in test_responses:
        result = content_filter(resp)
        status = "SAFE" if result["safe"] else "ISSUES FOUND"
        print(f"  [{status}] '{resp[:60]}...'")
        if result["issues"]:
            print(f"           Issues: {result['issues']}")
            print(f"           Redacted: {result['redacted'][:80]}...")


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    test_content_filter()
