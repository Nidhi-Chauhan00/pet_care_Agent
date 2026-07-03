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

from app.config import config

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

# --- Mock Tools for Phase 2 ---
def check_vet_availability(date: str) -> str:
    """Checks veterinary clinic availability for a given date.
    
    Args:
        date: The date to check availability for (e.g. '2026-07-04').
    """
    return f"Available slots on {date}: 10:00 AM, 2:00 PM, 3:30 PM."

def get_vaccination_schedule(pet_type: str) -> str:
    """Retrieves standard vaccination schedule for a pet type.
    
    Args:
        pet_type: Type of pet, e.g. 'dog' or 'cat'.
    """
    if pet_type.lower() == "dog":
        return "Rabies (1yr/3yr), DHPP, Bordetella."
    return "Rabies, FVRCP."

def get_food_recommendations(breed: str, age_months: int) -> str:
    """Gets food recommendations based on pet breed and age in months.
    
    Args:
        breed: Breed of the pet (e.g. 'Golden Retriever').
        age_months: Age of the pet in months.
    """
    if age_months < 12:
        return f"Puppy/Kitten formula high in protein and DHA for {breed}."
    return f"Adult maintenance formula with balanced nutrients for {breed}."

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
    description="Handles scheduling vet appointments and immunization records.",
    model=config.model,
    instruction="You are a specialized Pet Vet Scheduler and Immunization Tracker agent. You help the user schedule vet appointments, retrieve immunization records, and answer vet-related queries. Call your tools to complete tasks.",
    tools=[mcp_vet_tools, check_vet_availability, get_vaccination_schedule],
    output_schema=SubAgentOutput,
)

diet_advisor_agent = LlmAgent(
    name="diet_advisor_agent",
    description="Provides pet diet and nutrition advice based on breed, age, and health conditions.",
    model=config.model,
    instruction="You are a specialized Pet Diet and Nutrition Advisor agent. You help the user design diet plans, recommend food options, and check food compatibility. Call your tools to complete tasks.",
    tools=[mcp_diet_tools, get_food_recommendations],
    output_schema=SubAgentOutput,
)

orchestrator_agent = LlmAgent(
    name="orchestrator_agent",
    description="The main orchestrator that delegates pet care queries to specialized agents.",
    model=config.model,
    instruction=(
        "You are the Pet Care Concierge Orchestrator. Your role is to understand the user's pet care request and coordinate the specialized agents to fulfill it. "
        "Use your tools to delegate to the Vet Scheduler (vet_scheduler_agent) or the Diet Advisor (diet_advisor_agent) as needed. "
        "If you are scheduling a vet appointment, make sure to set action='schedule_vet', requires_approval=True, and fill in appointment_details. "
        "For diet advice, set action='diet_advice'. "
        "Always return a response fitting the OrchestratorOutput schema."
    ),
    output_schema=OrchestratorOutput,
    tools=[
        AgentTool(vet_scheduler_agent),
        AgentTool(diet_advisor_agent),
    ],
)

# --- Workflow Node Functions ---
def security_checkpoint(ctx: Context, node_input: Any) -> Event:
    # node_input is the types.Content from START (no input_schema set)
    query = ""
    if isinstance(node_input, str):
        query = node_input
    elif hasattr(node_input, "parts") and node_input.parts:
        query = " ".join([p.text for p in node_input.parts if p.text])
    elif isinstance(node_input, dict):
        query = node_input.get("query", "")
        
    # 1. PII Scrubbing (Scrub phone numbers and email addresses)
    scrubbed_query = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '[EMAIL]', query)
    scrubbed_query = re.sub(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b', '[PHONE]', scrubbed_query)
    
    # 2. Prompt Injection Detection
    injection_patterns = ["ignore instructions", "ignore previous instructions", "override prompt", "system prompt"]
    is_injection = any(pattern in query.lower() for pattern in injection_patterns)
    
    # 3. Domain-Specific Safety Rule (Block queries about exotic illegal/dangerous pets)
    exotic_pets = ["tiger", "lion", "cobra", "cheetah", "panther"]
    is_exotic = any(pet in query.lower() for pet in exotic_pets)
    
    # 4. Structured Audit Log
    log_entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "original_query_len": len(query),
        "pii_detected": query != scrubbed_query,
        "injection_detected": is_injection,
        "exotic_pet_detected": is_exotic,
        "severity": "CRITICAL" if (is_injection or is_exotic) else ("WARNING" if query != scrubbed_query else "INFO")
    }
    print(f"AUDIT_LOG: {json.dumps(log_entry)}")
    
    # Routing
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
        
    # Wrap scrubbed query in types.Content for the downstream LLM agent
    scrubbed_content = types.Content(role='user', parts=[types.Part.from_text(text=scrubbed_query)])
    return Event(
        output=scrubbed_content,
        route="__DEFAULT__",
        state={"query": scrubbed_query}
    )

def handle_security_violation(ctx: Context, node_input: Any) -> Event:
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

# Use @node decorator to set rerun_on_resume=True and avoid validation error on resume
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

# --- Workflow Definition (No input_schema) ---
root_agent = Workflow(
    name="pet_care_workflow",
    description="Pet care concierge workflow directing queries, checking security, and scheduling appointments.",
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
