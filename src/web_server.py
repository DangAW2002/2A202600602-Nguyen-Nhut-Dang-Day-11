import sys
import json
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from google.genai import types

# Add src to sys.path so we can import local modules
src_dir = Path(__file__).resolve().parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

# Import setup_api_key and run it immediately before importing other modules
from core.config import setup_api_key
setup_api_key()

# Now import the rest of the modules
from agents.agent import create_unsafe_agent, create_protected_agent
from guardrails.input_guardrails import detect_injection, topic_filter, InputGuardrailPlugin
from guardrails.output_guardrails import OutputGuardrailPlugin, _init_judge, content_filter, llm_safety_check
from guardrails.rate_limiter import RateLimitPlugin
from guardrails.audit_log import AuditLogPlugin
from guardrails.monitoring import SystemMonitor

# Global variables for agents and runners
unsafe_agent = None
unsafe_runner = None
protected_agent = None
protected_runner = None

# Initialize rate limit, audit log, and system monitor globally
rate_limit_plugin = RateLimitPlugin(max_requests=10, window_seconds=60)
audit_log_plugin = AuditLogPlugin()
system_monitor = SystemMonitor()

@asynccontextmanager
async def lifespan(app: FastAPI):
    global unsafe_agent, unsafe_runner, protected_agent, protected_runner
    print("Initializing agents and runners...")
    
    # Create Unprotected Agent
    unsafe_agent, unsafe_runner = create_unsafe_agent()
    
    # Create Protected Agent with guardrail plugins
    input_plugin = InputGuardrailPlugin()
    output_plugin = OutputGuardrailPlugin(use_llm_judge=True)
    _init_judge()
    
    # Register plugins to ADK pipeline
    protected_agent, protected_runner = create_protected_agent(
        plugins=[rate_limit_plugin, input_plugin, output_plugin, audit_log_plugin]
    )
    
    print("Web server startup complete.")
    yield
    print("Shutting down web server...")

app = FastAPI(
    title="VinBank Guardrails Comparison Dashboard",
    description="Lab 11: Prompt Injection & Guardrails Arena",
    version="1.0.0",
    lifespan=lifespan
)

# API Request Model
class ChatRequest(BaseModel):
    message: str
    comparison_mode: bool
    agent_type: str = "protected"  # "protected" or "unprotected"
    session_id: str | None = None

@app.post("/api/chat")
async def chat_endpoint(request: ChatRequest):
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    
    # 1. Check Rate Limiter before anything else
    user_id = "student"
    remaining_requests = rate_limit_plugin.get_remaining_requests(user_id)
    
    if remaining_requests <= 0:
        # Record block in monitor
        system_monitor.record_request(blocked=True, reason="rate_limiter")
        
        # Calculate remaining time
        import time
        from datetime import datetime
        wait_time = 60
        window = rate_limit_plugin.user_windows[user_id]
        if window:
            wait_time = max(1, int(window[0] + 60 - time.time()))
            
        block_msg = f"Blocked: Rate limit exceeded. Please wait {wait_time} seconds before retrying."
        
        # Record direct audit log
        audit_log_plugin.write_log({
            "timestamp": datetime.now().isoformat(),
            "session_id": request.session_id or "default",
            "input": request.message,
            "output": block_msg,
            "blocked": True,
            "blocker_layer": "Rate Limiter",
            "latency_ms": 0
        })
        
        async def block_generator():
            yield json.dumps({
                "type": "rate_limit_blocked",
                "message": block_msg,
                "remaining_requests": 0,
                "metrics": system_monitor.get_metrics()
            }) + "\n"
        return StreamingResponse(block_generator(), media_type="text/event-stream")
    
    async def event_generator():
        # Record request timestamp in rate limiter window since it's allowed
        import time
        rate_limit_plugin.user_windows[user_id].append(time.time())
        
        # 2. Run Input Guardrails check on the input message directly
        input_injection = detect_injection(request.message)
        input_off_topic = topic_filter(request.message)
        
        # Yield input guardrails report immediately
        yield json.dumps({
            "type": "input_guardrails",
            "input_injection": input_injection,
            "input_off_topic": input_off_topic
        }) + "\n"
        
        # 3. Run the streams concurrently
        queue = asyncio.Queue()
        active_tasks = 0
        
        async def produce_stream(runner, agent, name):
            try:
                user_id_val = "student"
                app_name = runner.app_name
                session = None
                if request.session_id is not None:
                    try:
                        session = await runner.session_service.get_session(
                            app_name=app_name, user_id=user_id_val, session_id=request.session_id
                        )
                    except Exception:
                        pass
                if session is None:
                    session = await runner.session_service.create_session(
                        app_name=app_name, user_id=user_id_val
                    )
                
                content = types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=request.message)],
                )
                
                async for event in runner.run_async(
                    user_id=user_id_val, session_id=session.id, new_message=content
                ):
                    if hasattr(event, "content") and event.content and event.content.parts:
                        for part in event.content.parts:
                            if hasattr(part, "text") and part.text:
                                await queue.put((name, part.text))
            except Exception as e:
                await queue.put((name, f"Error: {e}"))
            finally:
                await queue.put((name, None))
                
        if request.comparison_mode:
            asyncio.create_task(produce_stream(unsafe_runner, unsafe_agent, "unprotected"))
            asyncio.create_task(produce_stream(protected_runner, protected_agent, "protected"))
            active_tasks = 2
        else:
            if request.agent_type == "unprotected":
                asyncio.create_task(produce_stream(unsafe_runner, unsafe_agent, "unprotected"))
                active_tasks = 1
            else:
                asyncio.create_task(produce_stream(protected_runner, protected_agent, "protected"))
                active_tasks = 1
                
        unprotected_accumulated = ""
        protected_accumulated = ""
        
        finished_tasks = 0
        while finished_tasks < active_tasks:
            name, chunk = await queue.get()
            if chunk is None:
                finished_tasks += 1
            else:
                if name == "unprotected":
                    unprotected_accumulated += chunk
                else:
                    protected_accumulated += chunk
                
                yield json.dumps({
                    "type": "content",
                    "agent": name,
                    "text": chunk
                }) + "\n"
                
        # 4. Output guardrails report at the end
        target_for_analysis = unprotected_accumulated if request.comparison_mode or request.agent_type == "unprotected" else protected_accumulated
        
        output_pii_redacted = False
        output_pii_issues = []
        safety_judge_verdict = "Skipped"
        safety_judge_safe = True
        judge_scores = {"safety": 5, "relevance": 5, "accuracy": 5, "tone": 5}
        judge_reason = ""
        
        if target_for_analysis:
            # Check PII
            pii_res = content_filter(target_for_analysis)
            output_pii_redacted = not pii_res["safe"]
            output_pii_issues = pii_res["issues"]
            
            # Check Judge
            judge_res = await llm_safety_check(target_for_analysis)
            safety_judge_verdict = judge_res["verdict"]
            safety_judge_safe = judge_res["safe"]
            judge_scores = judge_res["scores"]
            judge_reason = judge_res["reason"]
            
        # 5. Record request metrics in system monitor
        blocked = (input_injection or input_off_topic or not safety_judge_safe)
        reason = None
        if blocked:
            if input_injection:
                reason = "input_injection"
            elif input_off_topic:
                reason = "input_off_topic"
            elif not safety_judge_safe:
                reason = "judge_fail"
        system_monitor.record_request(blocked=blocked, reason=reason)
            
        yield json.dumps({
            "type": "output_guardrails",
            "output_pii_redacted": output_pii_redacted,
            "output_pii_issues": output_pii_issues,
            "safety_judge_verdict": safety_judge_verdict,
            "safety_judge_safe": safety_judge_safe,
            "judge_scores": judge_scores,
            "judge_reason": judge_reason,
            "remaining_requests": rate_limit_plugin.get_remaining_requests(user_id),
            "metrics": system_monitor.get_metrics()
        }) + "\n"
        
    return StreamingResponse(event_generator(), media_type="text/event-stream")

# Mount the static files directory
static_path = src_dir / "static"
static_path.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=str(static_path), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web_server:app", host="127.0.0.1", port=8080, reload=True)
