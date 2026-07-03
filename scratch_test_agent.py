import os
import re
import json
import datetime
from typing import Generator, Any
from pydantic import BaseModel, Field

from google.adk.workflow import Workflow, node, START
from google.adk.agents import LlmAgent
from google.adk.tools import AgentTool
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

from google.adk.tools.mcp_tool.mcp_toolset import McpToolset, StdioConnectionParams
from mcp import StdioServerParameters

# Mock config for test
class AgentConfig:
    model: str = "gemini-2.5-flash"
config = AgentConfig()

# --- Pydantic Schemas ---
class WorkflowOutput(BaseModel):
    response: str = Field(description="The final concierge response.")

class SubAgentOutput(BaseModel):
    response: str = Field(description="The response content or answer from the agent.")

    @classmethod
    def model_validate_json(cls, data: str | bytes, *, strict: bool | None = None, context: Any | None = None) -> "SubAgentOutput":
        try:
            return super().model_validate_json(data, strict=strict, context=context)
        except Exception:
            text = data.decode('utf-8') if isinstance(data, bytes) else data
            return cls(response=text)

class OrchestratorOutput(BaseModel):
    response: str = Field(description="The response content or delegation output.")
    action: str = Field(description="The action: 'schedule_vet', 'diet_advice', 'general', or 'error'.")
    pet_name: str = Field(default="", description="The name of the pet if mentioned.")
    requires_approval: bool = Field(default=False, description="True if a vet appointment is being scheduled.")
    appointment_details: str = Field(default="", description="Details of the appointment to schedule.")

    @classmethod
    def model_validate_json(cls, data: str | bytes, *, strict: bool | None = None, context: Any | None = None) -> "OrchestratorOutput":
        try:
            return super().model_validate_json(data, strict=strict, context=context)
        except Exception:
            text = data.decode('utf-8') if isinstance(data, bytes) else data
            return cls(response=text, action="general", pet_name="", requires_approval=False, appointment_details="")

# --- Mock Tools ---
def check_vet_availability(date: str) -> str:
    return "Slots available."

def get_vaccination_schedule(pet_type: str) -> str:
    return "Schedule."

def get_food_recommendations(breed: str, age_months: int) -> str:
    return "Recs."

# --- MCP Toolsets ---
mcp_vet_tools = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", "app.mcp_server"],
        )
    ),
    tool_filter=["search_vets", "log_pet_task"]
)

mcp_diet_tools = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", "app.mcp_server"],
        )
    ),
    tool_filter=["check_food_safety", "log_pet_task"]
)

# --- Specialized Agents ---
vet_scheduler_agent = LlmAgent(
    name="vet_scheduler_agent",
    description="Handles scheduling vet appointments.",
    model=config.model,
    tools=[mcp_vet_tools, check_vet_availability, get_vaccination_schedule],
    output_schema=SubAgentOutput,
)

diet_advisor_agent = LlmAgent(
    name="diet_advisor_agent",
    description="Diet advisor.",
    model=config.model,
    tools=[mcp_diet_tools, get_food_recommendations],
    output_schema=SubAgentOutput,
)

orchestrator_agent = LlmAgent(
    name="orchestrator_agent",
    description="Orchestrator.",
    model=config.model,
    output_schema=OrchestratorOutput,
    tools=[
        AgentTool(vet_scheduler_agent),
        AgentTool(diet_advisor_agent),
    ],
)

# --- Workflow Node Functions ---
def security_checkpoint(ctx: Context, node_input: Any) -> Event:
    query = ""
    if isinstance(node_input, str):
        query = node_input
    elif hasattr(node_input, "parts") and node_input.parts:
        query = " ".join([p.text for p in node_input.parts if p.text])
    elif isinstance(node_input, dict):
        query = node_input.get("query", "")
        
    scrubbed_query = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '[EMAIL]', query)
    scrubbed_query = re.sub(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b', '[PHONE]', scrubbed_query)
    
    # 2. Prompt Injection Detection
    injection_patterns = ["ignore instructions", "ignore previous instructions", "override prompt", "system prompt"]
    is_injection = any(pattern in query.lower() for pattern in injection_patterns)
    
    # 3. Domain-Specific Safety Rule
    exotic_pets = ["tiger", "lion", "cobra", "cheetah", "panther"]
    is_exotic = any(pet in query.lower() for pet in exotic_pets)
    
    # 4. Audit Log
    log_entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "original_query_len": len(query),
        "pii_detected": query != scrubbed_query,
        "injection_detected": is_injection,
        "exotic_pet_detected": is_exotic,
        "severity": "CRITICAL" if (is_injection or is_exotic) else ("WARNING" if query != scrubbed_query else "INFO")
    }
    print(f"AUDIT_LOG: {json.dumps(log_entry)}")
    
    if is_injection:
        return Event(
            output={"reason": "Prompt injection attempt detected."},
            route="security_event",
            state={"query": scrubbed_query}
        )
    if is_exotic:
        return Event(
            output={"reason": "Concierge services are only available for domestic pets (dogs, cats, etc.)."},
            route="security_event",
            state={"query": scrubbed_query}
        )
        
    scrubbed_content = types.Content(role='user', parts=[types.Part.from_text(text=scrubbed_query)])
    return Event(
        output=scrubbed_content,
        route="__DEFAULT__",
        state={"query": scrubbed_query}
    )

def handle_security_violation(ctx: Context, node_input: Any) -> Event:
    # safe default for dict-like access
    reason = "Security validation failed."
    if isinstance(node_input, dict):
        reason = node_input.get("reason", reason)
    return Event(
        output={"response": f"⚠️ Security Checkpoint Triggered: {reason}"},
        state={"security_failed": True}
    )

def route_orchestrator(ctx: Context, node_input: Any) -> Event:
    action = "general"
    pet_name = ""
    requires_approval = False
    appointment_details = ""
    response_text = ""
    
    if isinstance(node_input, dict):
        action = node_input.get("action", action)
        pet_name = node_input.get("pet_name", pet_name)
        requires_approval = node_input.get("requires_approval", requires_approval)
        appointment_details = node_input.get("appointment_details", appointment_details)
        response_text = node_input.get("response", response_text)
    
    state_delta = {
        "action": action,
        "pet_name": pet_name,
        "appointment_details": appointment_details,
        "orchestrator_response": response_text
    }
    
    if requires_approval:
        return Event(output=node_input, route="needs_approval", state=state_delta)
    return Event(output=node_input, route="auto_approve", state=state_delta)

# Use @node decorator to set rerun_on_resume=True
@node(rerun_on_resume=True)
async def human_approval_node(ctx: Context, node_input: Any) -> Generator[Any, None, None]:
    if not ctx.resume_inputs:
        pet_name = ctx.state.get("pet_name") or "your pet"
        details = ctx.state.get("appointment_details") or "vet appointment"
        msg = f"Do you approve scheduling this vet appointment for {pet_name}: '{details}'? (yes/no)"
        yield RequestInput(interrupt_id="vet_approval", message=msg)
        return
        
    approval = ctx.resume_inputs.get("vet_approval", "").strip().lower()
    if approval in ["yes", "y", "approve", "confirm"]:
        msg = f"✅ Vet appointment successfully scheduled: {ctx.state.get('appointment_details')}"
        yield Event(output={"response": msg}, state={"approval_status": "approved"})
    else:
        msg = "❌ Vet appointment scheduling was cancelled by the user."
        yield Event(output={"response": msg}, state={"approval_status": "denied"})

def final_output_node(ctx: Context, node_input: Any) -> Generator[Any, None, None]:
    response_text = ""
    if isinstance(node_input, dict):
        response_text = node_input.get("response") or node_input.get("response_text")
    if not response_text:
        response_text = ctx.state.get("orchestrator_response") or ""
        
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=response_text)]))
    yield Event(output=WorkflowOutput(response=response_text))

# --- Workflow Definition ---
root_agent = Workflow(
    name="pet_care_workflow",
    description="Pet care concierge workflow.",
    output_schema=WorkflowOutput,
    edges=[
        (START, security_checkpoint),
        (security_checkpoint, {
            "__DEFAULT__": orchestrator_agent,
            "security_event": handle_security_violation
        }),
        (orchestrator_agent, route_orchestrator),
        (route_orchestrator, {
            "needs_approval": human_approval_node,
            "auto_approve": final_output_node
        }),
        (human_approval_node, final_output_node),
        (handle_security_violation, final_output_node),
    ]
)

app = App(
    root_agent=root_agent,
    name="app",
)

print("SUCCESS: Rerun_on_resume node decorated and validated successfully!")
