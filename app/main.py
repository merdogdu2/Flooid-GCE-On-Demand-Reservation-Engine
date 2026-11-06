"""FastAPI application for the GCE On-Demand Reservation Architect Agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent import (
    AGENT_MD_PATH,
    MODEL_ID,
    SESSION_HISTORIES,
    load_system_instruction,
    run_agent_turn,
)
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
    list_gce_reservations,
    normalize_user_email,
)

app = FastAPI(
    title="GCE On-Demand Reservation Architect",
    description="AI Agent for designing, validating, and provisioning Google Cloud Compute Engine on-demand reservations.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="User prompt to the GCE Reservation Agent")
    session_id: str = Field(default="default", description="Conversation session ID")
    user_email: Optional[str] = Field(
        default=None,
        description="Caller email identity (only admin@merdogdu.altostrat.com is authorized to provision reservations)",
    )


class ActionExecuteRequest(BaseModel):
    user_email: Optional[str] = Field(
        default=None,
        description="Caller email identity",
    )


def resolve_caller_email(request: Request, body_email: Optional[str] = None) -> str:
    """Resolves caller email from Google Cloud IAP header, X-User-Email header, or request body."""
    iap_email = request.headers.get("X-Goog-Authenticated-User-Email")
    if iap_email:
        return normalize_user_email(iap_email)
    header_email = request.headers.get("X-User-Email")
    if header_email:
        return normalize_user_email(header_email)
    if body_email:
        return normalize_user_email(body_email)
    return ""


class DirectValidateRequest(BaseModel):
    reservation_name: str
    owner_project_id: str = DEFAULT_PROJECT_ID
    zone: str = "us-central1-a"
    vm_count: int = 1
    machine_type: str = "e2-standard-2"
    specific_reservation_required: bool = False
    share_type: str = "LOCAL"
    consumer_projects: List[str] = Field(default_factory=list)
    min_cpu_platform: Optional[str] = None
    accelerator_type: Optional[str] = None
    accelerator_count: int = 0
    local_ssd_count: int = 0
    local_ssd_interface: str = "NVME"
    compact_placement_policy: Optional[str] = None


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "agent": "gce-reservation-agent",
        "model": MODEL_ID,
        "default_project_id": DEFAULT_PROJECT_ID,
        "authorized_reservation_admin": AUTHORIZED_RESERVATION_ADMIN,
        "spec_file": AGENT_MD_PATH.name,
    }


@app.get("/api/spec")
async def get_agent_spec() -> Dict[str, Any]:
    return {
        "name": "gce-reservation-agent",
        "model": MODEL_ID,
        "default_project_id": DEFAULT_PROJECT_ID,
        "authorized_reservation_admin": AUTHORIZED_RESERVATION_ADMIN,
        "markdown": AGENT_MD_PATH.read_text(encoding="utf-8"),
        "system_instruction": load_system_instruction(caller_email=AUTHORIZED_RESERVATION_ADMIN),
    }


@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest, request: Request) -> Dict[str, Any]:
    try:
        caller_email = resolve_caller_email(request, req.user_email)
        return run_agent_turn(
            user_message=req.message,
            session_id=req.session_id,
            user_email=caller_email,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/session/{session_id}/reset")
async def reset_session(session_id: str) -> Dict[str, Any]:
    SESSION_HISTORIES.pop(session_id, None)
    return {"status": "reset", "session_id": session_id}


@app.post("/api/validate")
async def validate_endpoint(req: DirectValidateRequest) -> Dict[str, Any]:
    return design_and_validate_reservation(**req.model_dump())


@app.get("/api/reservations")
async def list_reservations_endpoint(
    project_id: str = DEFAULT_PROJECT_ID,
    zone: Optional[str] = None,
) -> Dict[str, Any]:
    return list_gce_reservations(project_id=project_id, zone=zone)


@app.get("/api/reservations/{zone}/{reservation_name}")
async def describe_reservation_endpoint(
    zone: str,
    reservation_name: str,
    project_id: str = DEFAULT_PROJECT_ID,
) -> Dict[str, Any]:
    return describe_gce_reservation(
        reservation_name=reservation_name,
        zone=zone,
        project_id=project_id,
    )


@app.get("/api/quotas")
async def quotas_endpoint(
    region: str = "us-central1",
    project_id: str = DEFAULT_PROJECT_ID,
    metric_filter: Optional[str] = None,
) -> Dict[str, Any]:
    return check_region_quotas(
        region=region,
        project_id=project_id,
        metric_filter=metric_filter,
    )


@app.get("/api/org-policy")
async def org_policy_endpoint(project_id: str = DEFAULT_PROJECT_ID) -> Dict[str, Any]:
    return check_shared_reservation_org_policy(project_id=project_id)


@app.get("/api/actions")
async def list_staged_actions() -> Dict[str, Any]:
    return {
        "actions": list(STAGED_ACTIONS.values()),
    }


@app.post("/api/actions/{action_id}/execute")
async def approve_and_execute_action(
    action_id: str,
    request: Request,
    req: Optional[ActionExecuteRequest] = None,
) -> Dict[str, Any]:
    caller_email = resolve_caller_email(request, req.user_email if req else None)
    auth_error = check_user_authorization(caller_email)
    if auth_error is not None:
        return JSONResponse(status_code=403, content=auth_error)

    if action_id not in STAGED_ACTIONS:
        raise HTTPException(status_code=404, detail=f"Action '{action_id}' not found.")
    result = execute_staged_action(action_id=action_id, user_email=caller_email)
    if result.get("status") == "forbidden":
        return JSONResponse(status_code=403, content=result)
    if result.get("status") == "error":
        return JSONResponse(status_code=400, content=result)
    return result


@app.post("/api/actions/{action_id}/reject")
async def reject_staged_action(action_id: str) -> Dict[str, Any]:
    ticket = STAGED_ACTIONS.get(action_id)
    if not ticket:
        raise HTTPException(status_code=404, detail=f"Action '{action_id}' not found.")
    ticket["status"] = "REJECTED"
    return {"status": "rejected", "ticket": ticket}
