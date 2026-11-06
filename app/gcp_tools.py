"""Live Google Cloud Compute Engine & Org Policy tools with Human-in-the-Loop safety gates."""

from __future__ import annotations

import os
import time
import uuid
from typing import Any, Dict, List, Optional

from google.api_core.exceptions import GoogleAPICallError, NotFound, PermissionDenied
from google.cloud import compute_v1
from google.cloud import orgpolicy_v2

from app.validator import (
    AcceleratorSpec,
    LocalSsdSpec,
    ReservationDesignInput,
    validate_and_generate_design,
)

DEFAULT_PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "merdogdu-sandbox-398713")
AUTHORIZED_RESERVATION_ADMIN = os.environ.get(
    "AUTHORIZED_RESERVATION_ADMIN", "admin@merdogdu.altostrat.com"
).strip().lower()

# In-memory store of staged mutation actions requiring explicit user approval
STAGED_ACTIONS: Dict[str, Dict[str, Any]] = {}


def normalize_user_email(raw_email: Optional[str]) -> str:
    """Normalizes an email string (stripping IAP prefixes like 'accounts.google.com:')."""
    if not raw_email:
        return ""
    cleaned = raw_email.strip().lower()
    if ":" in cleaned:
        cleaned = cleaned.split(":", 1)[-1].strip()
    return cleaned


def check_user_authorization(user_email: Optional[str]) -> Optional[Dict[str, Any]]:
    """Checks whether the caller is authorized to make or modify GCE reservations.

    Returns None if authorized (`admin@merdogdu.altostrat.com`), or an error dict if unauthorized.
    """
    caller = normalize_user_email(user_email)
    if caller == AUTHORIZED_RESERVATION_ADMIN:
        return None
    display_caller = caller or "unauthenticated/unknown user"
    return {
        "status": "forbidden",
        "authorized": False,
        "caller_email": display_caller,
        "authorized_admin": AUTHORIZED_RESERVATION_ADMIN,
        "error": (
            f"Access Denied: You ({display_caller}) have no rights to make or modify a reservation for this app. "
            f"Only {AUTHORIZED_RESERVATION_ADMIN} is authorized to provision, resize, or delete GCE reservations."
        ),
    }


def _format_reservation_proto(res: compute_v1.Reservation, zone_name: str, project_id: str) -> Dict[str, Any]:
    spec = res.specific_reservation
    props = spec.instance_properties if spec else None
    zone_short = zone_name.split("/")[-1] if "/" in zone_name else zone_name

    share_type = "LOCAL"
    shared_projects: List[str] = []
    if res.share_settings and res.share_settings.share_type:
        share_type = str(res.share_settings.share_type)
        if res.share_settings.project_map:
            for k, v in res.share_settings.project_map.items():
                shared_projects.append(v.project_id or k)

    accelerators = []
    if props and props.guest_accelerators:
        for acc in props.guest_accelerators:
            accelerators.append(
                {
                    "accelerator_type": acc.accelerator_type,
                    "accelerator_count": acc.accelerator_count,
                }
            )

    local_ssds = []
    if props and props.local_ssds:
        for ssd in props.local_ssds:
            local_ssds.append(
                {
                    "interface": ssd.interface,
                    "disk_size_gb": ssd.disk_size_gb,
                }
            )

    return {
        "id": str(res.id) if res.id else None,
        "name": res.name,
        "project": project_id,
        "zone": zone_short,
        "status": res.status,
        "creation_timestamp": res.creation_timestamp,
        "specific_reservation_required": bool(res.specific_reservation_required),
        "vm_count": int(spec.count) if spec and spec.count is not None else 0,
        "in_use_count": int(spec.in_use_count) if spec and spec.in_use_count is not None else 0,
        "assured_count": int(spec.assured_count) if spec and spec.assured_count is not None else 0,
        "machine_type": props.machine_type if props else None,
        "min_cpu_platform": props.min_cpu_platform if props else None,
        "accelerators": accelerators,
        "local_ssds": local_ssds,
        "share_type": share_type,
        "consumer_projects": shared_projects,
    }


def list_gce_reservations(
    project_id: str = DEFAULT_PROJECT_ID,
    zone: Optional[str] = None,
) -> Dict[str, Any]:
    """Read-only tool: Lists GCE on-demand reservations in a project (for a specific zone or across all zones)."""
    project_id = project_id or DEFAULT_PROJECT_ID
    client = compute_v1.ReservationsClient()
    results: List[Dict[str, Any]] = []
    try:
        if zone:
            for res in client.list(project=project_id, zone=zone):
                results.append(_format_reservation_proto(res, zone, project_id))
        else:
            agg_request = compute_v1.AggregatedListReservationsRequest(project=project_id)
            for zone_scope, scoped_list in client.aggregated_list(request=agg_request):
                if scoped_list.reservations:
                    for res in scoped_list.reservations:
                        results.append(_format_reservation_proto(res, zone_scope, project_id))

        return {
            "status": "success",
            "project_id": project_id,
            "zone_filter": zone or "ALL_ZONES",
            "count": len(results),
            "reservations": results,
        }
    except GoogleAPICallError as exc:
        return {
            "status": "error",
            "project_id": project_id,
            "error": f"Failed to list reservations: {exc.message if hasattr(exc, 'message') else str(exc)}",
        }
    except Exception as exc:
        return {
            "status": "error",
            "project_id": project_id,
            "error": f"Unexpected error listing reservations: {str(exc)}",
        }


def describe_gce_reservation(
    reservation_name: str,
    zone: str,
    project_id: str = DEFAULT_PROJECT_ID,
) -> Dict[str, Any]:
    """Read-only tool: Describes a single GCE reservation in detail."""
    project_id = project_id or DEFAULT_PROJECT_ID
    client = compute_v1.ReservationsClient()
    try:
        res = client.get(project=project_id, zone=zone, reservation=reservation_name)
        return {
            "status": "success",
            "reservation": _format_reservation_proto(res, zone, project_id),
        }
    except NotFound:
        return {
            "status": "not_found",
            "project_id": project_id,
            "zone": zone,
            "reservation_name": reservation_name,
            "message": f"Reservation '{reservation_name}' does not exist in {project_id}/{zone}.",
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
        }


def check_region_quotas(
    region: str = "us-central1",
    project_id: str = DEFAULT_PROJECT_ID,
    metric_filter: Optional[str] = None,
) -> Dict[str, Any]:
    """Read-only tool: Checks GCE regional quotas (CPUs, GPUs, Local SSD, etc.) in the target project and region."""
    project_id = project_id or DEFAULT_PROJECT_ID
    # If user accidentally passed a zone (e.g. us-central1-a), strip the zone suffix
    parts = region.strip().split("-")
    if len(parts) >= 3 and len(parts[-1]) == 1 and parts[-1].isalpha():
        region = "-".join(parts[:-1])

    client = compute_v1.RegionsClient()
    try:
        reg_obj = client.get(project=project_id, region=region)
        key_metrics = {
            "CPUS",
            "E2_CPUS",
            "N2_CPUS",
            "N2D_CPUS",
            "C2_CPUS",
            "C2D_CPUS",
            "C3_CPUS",
            "C4_CPUS",
            "A2_CPUS",
            "A3_CPUS",
            "LOCAL_SSD_TOTAL_GB",
            "GPUS_ALL_REGIONS",
            "NVIDIA_T4_GPUS",
            "NVIDIA_L4_GPUS",
            "NVIDIA_A100_GPUS",
            "NVIDIA_A100_80GB_GPUS",
            "NVIDIA_H100_GPUS",
            "INSTANCES",
            "IN_USE_ADDRESSES",
        }

        quotas_out = []
        filter_upper = metric_filter.strip().upper() if metric_filter else None
        for q in reg_obj.quotas or []:
            metric_name = q.metric or ""
            include = False
            if filter_upper:
                if filter_upper in metric_name.upper():
                    include = True
            else:
                if metric_name in key_metrics or (q.usage and q.usage > 0):
                    include = True

            if include:
                limit_val = float(q.limit) if q.limit is not None else 0.0
                usage_val = float(q.usage) if q.usage is not None else 0.0
                quotas_out.append(
                    {
                        "metric": metric_name,
                        "usage": usage_val,
                        "limit": limit_val,
                        "available": max(0.0, limit_val - usage_val),
                    }
                )

        quotas_out.sort(key=lambda x: x["metric"])
        return {
            "status": "success",
            "project_id": project_id,
            "region": region,
            "region_status": reg_obj.status,
            "quotas": quotas_out,
        }
    except Exception as exc:
        return {
            "status": "error",
            "project_id": project_id,
            "region": region,
            "error": str(exc),
        }


def check_shared_reservation_org_policy(
    project_id: str = DEFAULT_PROJECT_ID,
) -> Dict[str, Any]:
    """Read-only tool: Inspects the effective Organization Policy `compute.sharedReservationsOwnerProjects` on the project."""
    project_id = project_id or DEFAULT_PROJECT_ID
    constraint_name = f"projects/{project_id}/policies/compute.sharedReservationsOwnerProjects"
    try:
        client = orgpolicy_v2.OrgPolicyClient()
        policy = client.get_effective_policy(name=constraint_name)
        rules_out = []
        allowed_values: List[str] = []
        allow_all = False
        deny_all = False

        if policy.spec and policy.spec.rules:
            for rule in policy.spec.rules:
                vals = list(rule.values.allowed_values) if rule.values else []
                denied = list(rule.values.denied_values) if rule.values else []
                allowed_values.extend(vals)
                if rule.allow_all:
                    allow_all = True
                if rule.deny_all:
                    deny_all = True
                rules_out.append(
                    {
                        "allow_all": bool(rule.allow_all),
                        "deny_all": bool(rule.deny_all),
                        "allowed_values": vals,
                        "denied_values": denied,
                    }
                )

        is_potentially_allowed = allow_all or any(
            project_id in v for v in allowed_values
        )
        return {
            "status": "success",
            "project_id": project_id,
            "constraint": "constraints/compute.sharedReservationsOwnerProjects",
            "effective_rules": rules_out,
            "allow_all": allow_all,
            "deny_all": deny_all,
            "project_explicitly_matched": is_potentially_allowed,
            "note": (
                "Owner projects for shared reservations must be allowlisted in "
                "`constraints/compute.sharedReservationsOwnerProjects` (by project ID or `projects/<PROJECT_NUMBER>`)."
            ),
        }
    except NotFound:
        return {
            "status": "not_configured",
            "project_id": project_id,
            "constraint": "constraints/compute.sharedReservationsOwnerProjects",
            "message": (
                "No explicit `compute.sharedReservationsOwnerProjects` policy found on this project/ancestry. "
                "By default in GCP Organizations, shared reservation creation is restricted unless the owner project is allowlisted."
            ),
        }
    except PermissionDenied as exc:
        return {
            "status": "permission_denied",
            "project_id": project_id,
            "constraint": "constraints/compute.sharedReservationsOwnerProjects",
            "message": (
                f"Caller lacks `orgpolicy.policy.get` permission on {project_id}: {str(exc)}. "
                "You can verify manually with: "
                f"`gcloud resource-manager org-policies describe compute.sharedReservationsOwnerProjects --project={project_id} --effective`"
            ),
        }
    except Exception as exc:
        return {
            "status": "error",
            "project_id": project_id,
            "constraint": "constraints/compute.sharedReservationsOwnerProjects",
            "error": str(exc),
        }


def design_and_validate_reservation(
    reservation_name: str,
    zone: str,
    vm_count: int,
    machine_type: str = "e2-standard-2",
    owner_project_id: str = DEFAULT_PROJECT_ID,
    specific_reservation_required: bool = False,
    share_type: str = "LOCAL",
    consumer_projects: Optional[List[str]] = None,
    shared_folders: Optional[List[str]] = None,
    min_cpu_platform: Optional[str] = None,
    accelerator_type: Optional[str] = None,
    accelerator_count: int = 0,
    local_ssd_count: int = 0,
    local_ssd_interface: str = "NVME",
    compact_placement_policy: Optional[str] = None,
    source_instance_template: Optional[str] = None,
    provisioning_model: str = "STANDARD",
    sole_tenant: bool = False,
) -> Dict[str, Any]:
    """Validates a GCE reservation configuration against all GCE architecture rules and generates Terraform HCL, gcloud CLI commands, and consumer VM targeting snippets."""
    accelerators = []
    if accelerator_type and accelerator_count > 0:
        accelerators.append(
            AcceleratorSpec(
                accelerator_type=accelerator_type,
                accelerator_count=accelerator_count,
            )
        )
    local_ssds = [
        LocalSsdSpec(interface=local_ssd_interface, disk_size_gb=375)
        for _ in range(max(0, local_ssd_count))
    ]

    design_input = ReservationDesignInput(
        reservation_name=reservation_name,
        owner_project_id=owner_project_id or DEFAULT_PROJECT_ID,
        zone=zone,
        vm_count=vm_count,
        machine_type=machine_type,
        source_instance_template=source_instance_template,
        min_cpu_platform=min_cpu_platform,
        accelerators=accelerators,
        local_ssds=local_ssds,
        specific_reservation_required=specific_reservation_required,
        share_type=share_type,
        consumer_projects=consumer_projects or [],
        shared_folders=shared_folders or [],
        compact_placement_policy=compact_placement_policy,
        provisioning_model=provisioning_model,
        sole_tenant=sole_tenant,
    )
    res = validate_and_generate_design(design_input)
    return res.model_dump()


def stage_reservation_mutation(
    action_type: str,
    reservation_name: str,
    zone: str,
    owner_project_id: str = DEFAULT_PROJECT_ID,
    vm_count: int = 1,
    machine_type: str = "e2-standard-2",
    specific_reservation_required: bool = False,
    share_type: str = "LOCAL",
    consumer_projects: Optional[List[str]] = None,
    min_cpu_platform: Optional[str] = None,
    accelerator_type: Optional[str] = None,
    accelerator_count: int = 0,
    local_ssd_count: int = 0,
    local_ssd_interface: str = "NVME",
    user_email: Optional[str] = None,
) -> Dict[str, Any]:
    """Stages a GCE reservation mutation (CREATE, RESIZE, or DELETE) for explicit user approval.

    NEVER executes the mutation immediately. Creates a pending approval ticket containing
    the exact gcloud command, Terraform HCL, validation status, and billing impact.
    Enforces RBAC: only `admin@merdogdu.altostrat.com` is permitted to stage or execute reservations.
    """
    auth_error = check_user_authorization(user_email)
    if auth_error is not None:
        return auth_error

    action_upper = action_type.strip().upper()
    owner_project_id = owner_project_id or DEFAULT_PROJECT_ID

    if action_upper not in {"CREATE", "RESIZE", "DELETE"}:
        return {
            "status": "error",
            "error": f"Invalid action_type '{action_type}'. Must be CREATE, RESIZE, or DELETE.",
        }

    if action_upper == "CREATE":
        design = design_and_validate_reservation(
            reservation_name=reservation_name,
            zone=zone,
            vm_count=vm_count,
            machine_type=machine_type,
            owner_project_id=owner_project_id,
            specific_reservation_required=specific_reservation_required,
            share_type=share_type,
            consumer_projects=consumer_projects,
            min_cpu_platform=min_cpu_platform,
            accelerator_type=accelerator_type,
            accelerator_count=accelerator_count,
            local_ssd_count=local_ssd_count,
            local_ssd_interface=local_ssd_interface,
        )
        if not design["valid"]:
            return {
                "status": "validation_failed",
                "errors": design["errors"],
                "warnings": design["warnings"],
                "message": "Cannot stage reservation CREATE because validation rules failed.",
            }
        gcloud_cmd = design["gcloud_command"]
        terraform_hcl = design["terraform_hcl"]
        billing_impact = design["billing_impact_summary"]
    elif action_upper == "RESIZE":
        if vm_count < 1 or vm_count > 1000:
            return {
                "status": "validation_failed",
                "errors": [f"vm_count ({vm_count}) must be between 1 and 1,000."],
            }
        gcloud_cmd = (
            f"gcloud compute reservations update {reservation_name} \\\n"
            f"  --project={owner_project_id} \\\n"
            f"  --zone={zone} \\\n"
            f"  --vm-count={vm_count}"
        )
        terraform_hcl = f"# Update `count = {vm_count}` inside specific_reservation block of google_compute_reservation.{reservation_name}"
        billing_impact = (
            f"IMMEDIATE BILLING IMPACT: Resizing reservation '{reservation_name}' in '{owner_project_id}' ({zone}) "
            f"to {vm_count} VM(s) immediately adjusts billed capacity to {vm_count} instances."
        )
    else:  # DELETE
        gcloud_cmd = (
            f"gcloud compute reservations delete {reservation_name} \\\n"
            f"  --project={owner_project_id} \\\n"
            f"  --zone={zone} \\\n"
            f"  --quiet"
        )
        terraform_hcl = f"# Run: terraform destroy -target=google_compute_reservation.{reservation_name}"
        billing_impact = (
            f"CAPACITY & BILLING IMPACT: Deleting reservation '{reservation_name}' in '{owner_project_id}' ({zone}) "
            "releases the reserved capacity and stops on-demand reservation billing (unless locked by a CUD commitment)."
        )

    action_id = f"act-{uuid.uuid4().hex[:8]}"
    ticket = {
        "action_id": action_id,
        "action_type": action_upper,
        "status": "PENDING_APPROVAL",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "reservation_name": reservation_name,
        "owner_project_id": owner_project_id,
        "zone": zone,
        "vm_count": vm_count,
        "machine_type": machine_type,
        "specific_reservation_required": specific_reservation_required,
        "share_type": share_type,
        "consumer_projects": consumer_projects or [],
        "min_cpu_platform": min_cpu_platform,
        "accelerator_type": accelerator_type,
        "accelerator_count": accelerator_count,
        "local_ssd_count": local_ssd_count,
        "local_ssd_interface": local_ssd_interface,
        "gcloud_command": gcloud_cmd,
        "terraform_hcl": terraform_hcl,
        "billing_impact": billing_impact,
        "requested_by": normalize_user_email(user_email),
    }
    STAGED_ACTIONS[action_id] = ticket
    return {
        "status": "staged_for_approval",
        "message": (
            f"Action '{action_id}' ({action_upper} reservation '{reservation_name}') has been staged by authorized admin "
            f"'{normalize_user_email(user_email)}'. Present the gcloud command, Terraform configuration, and billing impact "
            "to the user and ask for explicit approval."
        ),
        "ticket": ticket,
    }


def execute_staged_action(
    action_id: str,
    user_email: Optional[str] = None,
) -> Dict[str, Any]:
    """Executes a previously staged GCE reservation action via the Google Cloud Compute Engine API."""
    auth_error = check_user_authorization(user_email)
    if auth_error is not None:
        return auth_error

    ticket = STAGED_ACTIONS.get(action_id)
    if not ticket:
        return {
            "status": "error",
            "error": f"Staged action '{action_id}' not found.",
        }

    if ticket["status"] == "EXECUTED":
        return {
            "status": "already_executed",
            "ticket": ticket,
        }

    client = compute_v1.ReservationsClient()
    project_id = ticket["owner_project_id"]
    zone = ticket["zone"]
    res_name = ticket["reservation_name"]
    action_type = ticket["action_type"]

    try:
        if action_type == "CREATE":
            props = compute_v1.AllocationSpecificSKUAllocationReservedInstanceProperties(
                machine_type=ticket["machine_type"],
            )
            if ticket.get("min_cpu_platform"):
                props.min_cpu_platform = ticket["min_cpu_platform"]

            if ticket.get("accelerator_type") and ticket.get("accelerator_count", 0) > 0:
                props.guest_accelerators = [
                    compute_v1.AcceleratorConfig(
                        accelerator_type=ticket["accelerator_type"],
                        accelerator_count=int(ticket["accelerator_count"]),
                    )
                ]

            if ticket.get("local_ssd_count", 0) > 0:
                props.local_ssds = [
                    compute_v1.AllocationSpecificSKUAllocationAllocatedInstancePropertiesReservedDisk(
                        interface=ticket.get("local_ssd_interface", "NVME"),
                        disk_size_gb=375,
                    )
                    for _ in range(int(ticket["local_ssd_count"]))
                ]

            spec_res = compute_v1.AllocationSpecificSKUReservation(
                count=int(ticket["vm_count"]),
                instance_properties=props,
            )

            res_resource = compute_v1.Reservation(
                name=res_name,
                zone=zone,
                specific_reservation_required=bool(ticket["specific_reservation_required"]),
                specific_reservation=spec_res,
            )

            share_type = ticket.get("share_type", "LOCAL").upper()
            consumers = [
                p for p in (ticket.get("consumer_projects") or []) if p and p != project_id
            ]
            if share_type in {"SPECIFIC_PROJECTS", "SHARED", "PROJECTS"} and consumers:
                proj_map = {
                    p: compute_v1.ShareSettingsProjectConfig(project_id=p) for p in consumers
                }
                res_resource.share_settings = compute_v1.ShareSettings(
                    share_type="SPECIFIC_PROJECTS",
                    project_map=proj_map,
                )

            operation = client.insert(
                project=project_id,
                zone=zone,
                reservation_resource=res_resource,
            )
            operation.result(timeout=120)

        elif action_type == "RESIZE":
            resize_req = compute_v1.ReservationsResizeRequest(
                specific_sku_count=int(ticket["vm_count"])
            )
            operation = client.resize(
                project=project_id,
                zone=zone,
                reservation=res_name,
                reservations_resize_request_resource=resize_req,
            )
            operation.result(timeout=120)

        elif action_type == "DELETE":
            operation = client.delete(
                project=project_id,
                zone=zone,
                reservation=res_name,
            )
            operation.result(timeout=120)

        ticket["status"] = "EXECUTED"
        ticket["executed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Fetch updated state if CREATE or RESIZE
        latest_details = None
        if action_type in {"CREATE", "RESIZE"}:
            desc = describe_gce_reservation(res_name, zone, project_id)
            if desc.get("status") == "success":
                latest_details = desc.get("reservation")

        return {
            "status": "success",
            "action_id": action_id,
            "action_type": action_type,
            "message": f"Successfully executed {action_type} for reservation '{res_name}' in {project_id}/{zone}.",
            "ticket": ticket,
            "reservation": latest_details,
        }
    except Exception as exc:
        ticket["status"] = "FAILED"
        ticket["error"] = str(exc)
        return {
            "status": "error",
            "action_id": action_id,
            "action_type": action_type,
            "error": str(exc),
            "ticket": ticket,
        }
