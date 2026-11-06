"""GCE On-Demand Reservation Architect Agent powered by Google GenAI SDK (`gemini-3.8-flash`)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from google import genai
from google.genai import types

from app.gcp_tools import (
    AUTHORIZED_RESERVATION_ADMIN,
    DEFAULT_PROJECT_ID,
    STAGED_ACTIONS,
    check_region_quotas,
    check_shared_reservation_org_policy,
    check_user_authorization,
    describe_gce_reservation,
    design_and_validate_reservation,
    execute_staged_action,
    list_authorized_projects,
    list_gce_reservations,
    normalize_user_email,
    stage_reservation_mutation,
)

MODEL_ID = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
AGENT_MD_PATH = Path(__file__).resolve().parent.parent / "gce-reservation-agent.md"


def load_system_instruction(
    caller_email: Optional[str] = None,
    active_project_id: Optional[str] = None,
) -> str:
    """Loads the system instruction directly from gce-reservation-agent.md."""
    raw = AGENT_MD_PATH.read_text(encoding="utf-8")
    # Strip YAML frontmatter if present
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            raw = parts[2].strip()

    normalized_caller = normalize_user_email(caller_email) or "unauthenticated/unknown"
    is_authorized = normalized_caller == AUTHORIZED_RESERVATION_ADMIN
    selected_project = (active_project_id or DEFAULT_PROJECT_ID).strip()

    runtime_context = f"""

# Deployed Runtime Context & Available Tools
* **Currently Selected GCP Project in UI**: `{selected_project}` (Default to `{selected_project}` as the `owner_project_id` unless the user specifies another project).
* **Current Authenticated Caller Email**: `{normalized_caller}` (Authorized to provision/modify reservations: `{is_authorized}`).
* **Strict RBAC Authorization Policy**:
  - ONLY `{AUTHORIZED_RESERVATION_ADMIN}` is allowed to create, stage, resize, or delete reservations for this application.
  - If the current caller (`{normalized_caller}`) is NOT `{AUTHORIZED_RESERVATION_ADMIN}` and asks to create, stage, resize, or delete a reservation, immediately reject the request with:
    `"Access Denied: You ({normalized_caller}) have no rights to make or modify a reservation for this app. Only {AUTHORIZED_RESERVATION_ADMIN} is authorized."`
* **Live GCP Read-Only Tools (Safe to call automatically without asking)**:
  - `list_authorized_projects`: List all active GCP projects that the account is authorized to access.
  - `list_gce_reservations`: List live GCE reservations in a project/zone.
  - `describe_gce_reservation`: Inspect a specific live GCE reservation.
  - `check_region_quotas`: Inspect live GCE regional quotas (CPUs, E2/N2/C3 CPUs, GPUs, Local SSDs) in the target region.
  - `check_shared_reservation_org_policy`: Inspect the effective `constraints/compute.sharedReservationsOwnerProjects` Org Policy on the owner project.
  - `design_and_validate_reservation`: Deterministically validate all 4 GCE reservation validation rules (`f1-micro`/`g1-small` restriction, `<= 1000` VM cap, shared reservation 1..100 consumer project limit, owner-not-in-consumer-list rule, compact placement policy constraints) and generate ready-to-use **Terraform (`google_compute_reservation`)** and **`gcloud compute reservations create`** blocks plus consumer VM targeting commands.
* **Mutation & Approval Tools (Strict Guardrail + RBAC Enforced)**:
  - `stage_reservation_mutation`: Stages a `CREATE`, `RESIZE`, or `DELETE` operation and creates an Approval Ticket (`action_id`). Only succeeds if the caller is `{AUTHORIZED_RESERVATION_ADMIN}`; otherwise fails with `Access Denied: You have no rights...`.
  - `execute_confirmed_mutation`: Executes a previously staged `action_id` ONLY AFTER `{AUTHORIZED_RESERVATION_ADMIN}` has explicitly confirmed in a follow-up message. NEVER call `execute_confirmed_mutation` in the same turn that you called `stage_reservation_mutation`.
"""
    return raw + runtime_context


def get_genai_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if api_key:
        return genai.Client(api_key=api_key)
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", DEFAULT_PROJECT_ID)
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    return genai.Client(vertexai=True, project=project, location=location)


# Session conversation store: session_id -> list of types.Content
SESSION_HISTORIES: Dict[str, List[types.Content]] = {}


TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="list_authorized_projects",
        description="Read-only: Lists all active GCP projects that the account is authorized to access.",
        parameters_json_schema={
            "type": "object",
            "properties": {},
        },
    ),
    types.FunctionDeclaration(
        name="list_gce_reservations",
        description="Read-only: Lists live Google Cloud Compute Engine (GCE) reservations in the specified project and optional zone.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": f"GCP project ID (defaults to '{DEFAULT_PROJECT_ID}').",
                },
                "zone": {
                    "type": "string",
                    "description": "Optional GCE zone (e.g. 'us-central1-a'). Omit to list across all zones.",
                },
            },
        },
    ),
    types.FunctionDeclaration(
        name="describe_gce_reservation",
        description="Read-only: Describes a specific GCE reservation in a project and zone.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "reservation_name": {
                    "type": "string",
                    "description": "Name of the GCE reservation.",
                },
                "zone": {
                    "type": "string",
                    "description": "GCE zone where the reservation resides (e.g. 'us-central1-a').",
                },
                "project_id": {
                    "type": "string",
                    "description": f"GCP project ID (defaults to '{DEFAULT_PROJECT_ID}').",
                },
            },
            "required": ["reservation_name", "zone"],
        },
    ),
    types.FunctionDeclaration(
        name="check_region_quotas",
        description="Read-only: Inspects live GCE regional quotas (CPUs, E2_CPUS, N2_CPUS, GPUs, LOCAL_SSD_TOTAL_GB, etc.) for a GCP project and region.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "description": "GCP region name, e.g. 'us-central1' or 'europe-west1'.",
                },
                "project_id": {
                    "type": "string",
                    "description": f"GCP project ID (defaults to '{DEFAULT_PROJECT_ID}').",
                },
                "metric_filter": {
                    "type": "string",
                    "description": "Optional substring filter for quota metric name (e.g. 'CPU', 'GPU', 'N2').",
                },
            },
            "required": ["region"],
        },
    ),
    types.FunctionDeclaration(
        name="check_shared_reservation_org_policy",
        description="Read-only: Checks the effective `compute.sharedReservationsOwnerProjects` Organization Policy on the target owner project.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": f"GCP owner project ID to inspect (defaults to '{DEFAULT_PROJECT_ID}').",
                },
            },
        },
    ),
    types.FunctionDeclaration(
        name="design_and_validate_reservation",
        description=(
            "Validates a GCE on-demand reservation design against all GCE architecture rules "
            "(disallowed f1-micro/g1-small, max 1000 VMs, shared reservation 1..100 consumer projects, "
            "owner project excluded from consumer map, compact placement policy constraints) and "
            "generates Terraform (`google_compute_reservation`) HCL, `gcloud compute reservations create` CLI command, "
            "and consumer VM targeting snippets."
        ),
        parameters_json_schema={
            "type": "object",
            "properties": {
                "reservation_name": {
                    "type": "string",
                    "description": "RFC1035 reservation name, e.g. 'prod-e2-res-01'.",
                },
                "owner_project_id": {
                    "type": "string",
                    "description": f"Owner GCP project ID (defaults to '{DEFAULT_PROJECT_ID}').",
                },
                "zone": {
                    "type": "string",
                    "description": "Target GCE zone, e.g. 'us-central1-a'.",
                },
                "vm_count": {
                    "type": "integer",
                    "description": "Number of VMs to reserve (1 to 1000).",
                },
                "machine_type": {
                    "type": "string",
                    "description": "GCE machine type, e.g. 'e2-standard-4', 'n2-standard-8', 'e2-micro'.",
                },
                "specific_reservation_required": {
                    "type": "boolean",
                    "description": "True if VMs must explicitly target this reservation (--require-specific-reservation); False for automatic consumption.",
                },
                "share_type": {
                    "type": "string",
                    "enum": ["LOCAL", "SPECIFIC_PROJECTS", "DIRECT_PROJECTS_UNDER_SPECIFIC_FOLDERS"],
                    "description": "LOCAL for single-project, SPECIFIC_PROJECTS for shared across 1-100 consumer projects.",
                },
                "consumer_projects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Consumer GCP project IDs (1 to 100) if share_type is SPECIFIC_PROJECTS. Do NOT include owner_project_id.",
                },
                "shared_folders": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Folder IDs if share_type is DIRECT_PROJECTS_UNDER_SPECIFIC_FOLDERS.",
                },
                "min_cpu_platform": {
                    "type": "string",
                    "description": "Optional minimum CPU platform, e.g. 'Intel Ice Lake'.",
                },
                "accelerator_type": {
                    "type": "string",
                    "description": "Optional GPU accelerator type, e.g. 'nvidia-l4' or 'nvidia-tesla-t4'.",
                },
                "accelerator_count": {
                    "type": "integer",
                    "description": "Number of GPUs per VM (0 if none).",
                },
                "local_ssd_count": {
                    "type": "integer",
                    "description": "Number of 375GB local SSD partitions per VM (0 if none).",
                },
                "local_ssd_interface": {
                    "type": "string",
                    "enum": ["NVME", "SCSI"],
                    "description": "Local SSD interface (NVME or SCSI).",
                },
                "compact_placement_policy": {
                    "type": "string",
                    "description": "Optional compact placement policy name.",
                },
                "source_instance_template": {
                    "type": "string",
                    "description": "Optional instance template URI instead of explicit machine_type.",
                },
                "provisioning_model": {
                    "type": "string",
                    "enum": ["STANDARD", "SPOT", "PREEMPTIBLE", "FLEX_START"],
                    "description": "Provisioning model (STANDARD is required for on-demand reservations).",
                },
                "sole_tenant": {
                    "type": "boolean",
                    "description": "True if user asked to reserve sole-tenant nodes.",
                },
            },
            "required": ["reservation_name", "zone", "vm_count"],
        },
    ),
    types.FunctionDeclaration(
        name="stage_reservation_mutation",
        description=(
            "Stages a GCE reservation mutation (CREATE, RESIZE, or DELETE) for explicit user approval. "
            "Does NOT execute on GCP immediately; creates an approval ticket with the exact gcloud command, "
            "Terraform configuration, and billing impact so the user can review and confirm."
        ),
        parameters_json_schema={
            "type": "object",
            "properties": {
                "action_type": {
                    "type": "string",
                    "enum": ["CREATE", "RESIZE", "DELETE"],
                    "description": "Type of mutation to stage.",
                },
                "reservation_name": {
                    "type": "string",
                    "description": "Name of the GCE reservation.",
                },
                "zone": {
                    "type": "string",
                    "description": "GCE zone (e.g. 'us-central1-a').",
                },
                "owner_project_id": {
                    "type": "string",
                    "description": f"Owner project ID (defaults to '{DEFAULT_PROJECT_ID}').",
                },
                "vm_count": {
                    "type": "integer",
                    "description": "Target VM count for CREATE or RESIZE (1 to 1000).",
                },
                "machine_type": {
                    "type": "string",
                    "description": "GCE machine type for CREATE (e.g. 'e2-standard-2').",
                },
                "specific_reservation_required": {
                    "type": "boolean",
                    "description": "True for specific reservation; False for automatic use.",
                },
                "share_type": {
                    "type": "string",
                    "enum": ["LOCAL", "SPECIFIC_PROJECTS"],
                    "description": "LOCAL or SPECIFIC_PROJECTS.",
                },
                "consumer_projects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Consumer project IDs when share_type is SPECIFIC_PROJECTS.",
                },
                "min_cpu_platform": {
                    "type": "string",
                    "description": "Optional minimum CPU platform.",
                },
                "accelerator_type": {
                    "type": "string",
                    "description": "Optional GPU accelerator type.",
                },
                "accelerator_count": {
                    "type": "integer",
                    "description": "Number of GPUs per VM.",
                },
                "local_ssd_count": {
                    "type": "integer",
                    "description": "Number of 375GB local SSDs per VM.",
                },
                "local_ssd_interface": {
                    "type": "string",
                    "enum": ["NVME", "SCSI"],
                },
            },
            "required": ["action_type", "reservation_name", "zone"],
        },
    ),
    types.FunctionDeclaration(
        name="execute_confirmed_mutation",
        description=(
            "Executes a previously staged reservation mutation (`action_id`) on Google Cloud Compute Engine "
            "ONLY AFTER the user has explicitly confirmed/approved the staged proposal in a subsequent message."
        ),
        parameters_json_schema={
            "type": "object",
            "properties": {
                "action_id": {
                    "type": "string",
                    "description": "The staged action_id (e.g. 'act-1a2b3c4d') that the user approved.",
                },
            },
            "required": ["action_id"],
        },
    ),
]


def run_agent_turn(
    user_message: str,
    session_id: str = "default",
    user_email: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Runs a full agent turn (including multi-step tool calls) and returns the response and telemetry."""
    client = get_genai_client()
    normalized_caller = normalize_user_email(user_email)
    selected_project = (project_id or DEFAULT_PROJECT_ID).strip()
    system_instruction = load_system_instruction(
        caller_email=normalized_caller,
        active_project_id=selected_project,
    )

    history = SESSION_HISTORIES.setdefault(session_id, [])
    history.append(
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(
                    text=(
                        f"[Caller Identity: {normalized_caller or 'unauthenticated'} | "
                        f"Selected Project: {selected_project}]\n{user_message}"
                    )
                )
            ],
        )
    )

    tool_obj = types.Tool(function_declarations=TOOL_DECLARATIONS)
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=[tool_obj],
        temperature=0.2,
    )

    tool_trace: List[Dict[str, Any]] = []
    staged_in_this_turn: set[str] = set()
    latest_design: Optional[Dict[str, Any]] = None
    staged_tickets: List[Dict[str, Any]] = []

    max_iterations = 6
    for _ in range(max_iterations):
        response = client.models.generate_content(
            model=MODEL_ID,
            contents=history,
            config=config,
        )

        candidate = response.candidates[0] if response.candidates else None
        if not candidate or not candidate.content:
            break

        history.append(candidate.content)

        function_calls = response.function_calls
        if not function_calls:
            break

        tool_response_parts: List[types.Part] = []
        for fc in function_calls:
            fn_name = fc.name
            fn_args = dict(fc.args) if fc.args else {}

            result_payload = _dispatch_tool(
                fn_name=fn_name,
                fn_args=fn_args,
                staged_in_this_turn=staged_in_this_turn,
                user_email=normalized_caller,
            )

            if fn_name == "design_and_validate_reservation":
                latest_design = result_payload
            elif fn_name == "stage_reservation_mutation":
                if result_payload.get("status") == "staged_for_approval":
                    ticket = result_payload.get("ticket", {})
                    if ticket.get("action_id"):
                        staged_in_this_turn.add(ticket["action_id"])
                        staged_tickets.append(ticket)

            tool_trace.append(
                {
                    "tool": fn_name,
                    "arguments": fn_args,
                    "result": result_payload,
                }
            )

            tool_response_parts.append(
                types.Part.from_function_response(
                    name=fn_name,
                    response={"result": result_payload},
                )
            )

        history.append(types.Content(role="tool", parts=tool_response_parts))

    reply_text = response.text if response and response.text else ""
    pending_actions = [
        t for t in STAGED_ACTIONS.values() if t.get("status") == "PENDING_APPROVAL"
    ]

    return {
        "session_id": session_id,
        "model": MODEL_ID,
        "caller_email": normalized_caller or "unauthenticated",
        "authorized": check_user_authorization(normalized_caller) is None,
        "authorized_admin": AUTHORIZED_RESERVATION_ADMIN,
        "reply": reply_text,
        "tool_trace": tool_trace,
        "latest_design": latest_design,
        "staged_tickets": staged_tickets,
        "pending_actions": pending_actions,
    }


def _dispatch_tool(
    fn_name: str,
    fn_args: Dict[str, Any],
    staged_in_this_turn: set[str],
    user_email: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        if fn_name == "list_authorized_projects":
            return list_authorized_projects()
        if fn_name == "list_gce_reservations":
            return list_gce_reservations(**fn_args)
        if fn_name == "describe_gce_reservation":
            return describe_gce_reservation(**fn_args)
        if fn_name == "check_region_quotas":
            return check_region_quotas(**fn_args)
        if fn_name == "check_shared_reservation_org_policy":
            return check_shared_reservation_org_policy(**fn_args)
        if fn_name == "design_and_validate_reservation":
            return design_and_validate_reservation(**fn_args)
        if fn_name == "stage_reservation_mutation":
            fn_args["user_email"] = user_email
            return stage_reservation_mutation(**fn_args)
        if fn_name == "execute_confirmed_mutation":
            auth_err = check_user_authorization(user_email)
            if auth_err is not None:
                return auth_err
            action_id = fn_args.get("action_id", "")
            if action_id in staged_in_this_turn:
                return {
                    "status": "blocked_by_safety_guardrail",
                    "error": (
                        f"Guardrail Enforced: Action '{action_id}' was staged in this same turn. "
                        "You MUST present the exact gcloud command, Terraform configuration, and billing impact "
                        "to the user first and wait for their explicit confirmation in the next turn (or via the Approve button)."
                    ),
                }
            return execute_staged_action(action_id=action_id, user_email=user_email)
        return {"status": "error", "error": f"Unknown tool '{fn_name}'"}
    except Exception as exc:
        return {"status": "error", "error": f"Tool execution error in {fn_name}: {str(exc)}"}
