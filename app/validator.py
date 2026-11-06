"""Deterministic validation and Terraform/gcloud generator for GCE On-Demand Reservations.

Implements all rules from gce-reservation-agent.md:
1. Machine Type Restrictions (f1-micro, g1-small disallowed; Spot/Flex-start/sole-tenant warnings)
2. Capacity & Immutability Limits (max 1,000 VMs per reservation; immutable fields)
3. Shared Reservation Rules (1..100 consumer projects, owner not in consumer list, org policy, billing, >100 sharding/folder sharing)
4. Compact Placement Policy Constraints (LOCAL only, requires specific_reservation_required=True)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


DISALLOWED_MACHINE_TYPES = {"f1-micro", "g1-small"}
RECOMMENDED_ALTERNATIVES = [
    "e2-micro",
    "e2-small",
    "e2-medium",
    "n1-standard-1",
    "e2-standard-2",
]
MAX_VMS_PER_RESERVATION = 1000
MAX_CONSUMER_PROJECTS = 100
MAX_SHARED_FOLDERS = 128


class AcceleratorSpec(BaseModel):
    accelerator_type: str = Field(..., description="e.g. nvidia-tesla-t4, nvidia-l4, nvidia-h100-80gb")
    accelerator_count: int = Field(..., ge=1, description="Number of GPUs per VM")


class LocalSsdSpec(BaseModel):
    interface: str = Field(default="NVME", description="NVME or SCSI")
    disk_size_gb: int = Field(default=375, description="Each local SSD partition is 375 GB")


class ReservationDesignInput(BaseModel):
    reservation_name: str = Field(..., description="RFC1035 compliant GCE reservation name")
    owner_project_id: str = Field(..., description="GCP Project ID that owns the reservation")
    zone: str = Field(..., description="GCE zone, e.g. us-central1-a")
    vm_count: int = Field(..., description="Number of VMs to reserve (1 to 1000)")
    machine_type: Optional[str] = Field(
        default=None, description="GCE machine type, e.g. e2-standard-4, n2-standard-8"
    )
    source_instance_template: Optional[str] = Field(
        default=None,
        description="Optional selfLink or path to an instance template instead of explicit machine_type",
    )
    min_cpu_platform: Optional[str] = Field(
        default=None, description="Optional minimum CPU platform, e.g. 'Intel Ice Lake'"
    )
    accelerators: List[AcceleratorSpec] = Field(default_factory=list)
    local_ssds: List[LocalSsdSpec] = Field(default_factory=list)
    specific_reservation_required: bool = Field(
        default=False,
        description="True = Specific reservation (--require-specific-reservation); False = Use automatically",
    )
    share_type: str = Field(
        default="LOCAL",
        description="LOCAL, SPECIFIC_PROJECTS, or DIRECT_PROJECTS_UNDER_SPECIFIC_FOLDERS",
    )
    consumer_projects: List[str] = Field(
        default_factory=list,
        description="List of consumer GCP project IDs when share_type is SPECIFIC_PROJECTS",
    )
    shared_folders: List[str] = Field(
        default_factory=list,
        description="List of folder IDs when share_type is DIRECT_PROJECTS_UNDER_SPECIFIC_FOLDERS",
    )
    compact_placement_policy: Optional[str] = Field(
        default=None,
        description="Optional compact placement policy name or URI",
    )
    provisioning_model: str = Field(
        default="STANDARD",
        description="VM provisioning model (STANDARD is required; SPOT/PREEMPTIBLE/FLEX_START disallowed)",
    )
    sole_tenant: bool = Field(
        default=False,
        description="Whether the user asked to reserve sole-tenant nodes",
    )


class ReservationDesignResult(BaseModel):
    valid: bool
    errors: List[str]
    warnings: List[str]
    best_practices: List[str]
    billing_impact_summary: str
    terraform_hcl: str
    gcloud_command: str
    consumer_targeting_gcloud: str
    consumer_targeting_terraform: str
    normalized_spec: Dict[str, Any]


def validate_and_generate_design(spec: ReservationDesignInput) -> ReservationDesignResult:
    errors: List[str] = []
    warnings: List[str] = []
    best_practices: List[str] = []

    name = spec.reservation_name.strip()
    if not re.match(r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$", name):
        errors.append(
            f"Invalid reservation_name '{name}': must be RFC1035 compliant (1-63 lowercase letters, digits, or hyphens, starting with a letter)."
        )

    # 1. Machine Type & Provisioning Model Restrictions
    if not spec.machine_type and not spec.source_instance_template:
        errors.append("Either `machine_type` or `source_instance_template` must be provided.")

    if spec.machine_type:
        mt = spec.machine_type.strip().lower()
        if mt in DISALLOWED_MACHINE_TYPES:
            errors.append(
                f"Machine type '{mt}' is disallowed by the GCE control plane (\"This machine type is disallowed for reservations.\"). "
                f"Legacy N1 fractional shared-core machine types (f1-micro, g1-small) cannot be reserved. "
                f"Allowed alternatives: {', '.join(RECOMMENDED_ALTERNATIVES)} (E2 shared-core types ARE supported for reservations)."
            )

    if spec.provisioning_model.upper() in {"SPOT", "PREEMPTIBLE", "FLEX_START", "FLEX-START"}:
        errors.append(
            f"Provisioning model '{spec.provisioning_model}' cannot consume on-demand reservations. "
            "Spot/preemptible VMs and Flex-start VMs are not eligible for GCE on-demand capacity reservations."
        )

    if spec.sole_tenant:
        errors.append(
            "Sole-tenant nodes cannot consume standard on-demand VM reservations. Use sole-tenant node groups/templates instead."
        )

    # 2. Capacity & Immutability Limits
    if spec.vm_count < 1:
        errors.append(f"Invalid vm_count ({spec.vm_count}): must be at least 1.")
    elif spec.vm_count > MAX_VMS_PER_RESERVATION:
        errors.append(
            f"vm_count ({spec.vm_count}) exceeds the maximum limit of {MAX_VMS_PER_RESERVATION} VMs per reservation "
            "(`specificReservation.count <= 1000`). Split your capacity across multiple reservations."
        )

    best_practices.append(
        "Immutability Notice: `name`, `zone`, `machine_type`, `min_cpu_platform`, `accelerators`, and `local_ssds` "
        "cannot be modified after creation. Only `vm_count` (resize) and `share-with` consumer projects can be updated later."
    )

    # 3. Shared Reservation Rules
    share_type = spec.share_type.strip().upper()
    if share_type in {"SHARED", "PROJECTS", "SPECIFIC_PROJECTS"}:
        share_type = "SPECIFIC_PROJECTS"
    elif share_type in {"FOLDERS", "DIRECT_PROJECTS_UNDER_SPECIFIC_FOLDERS"}:
        share_type = "DIRECT_PROJECTS_UNDER_SPECIFIC_FOLDERS"
    else:
        share_type = "LOCAL"

    cleaned_consumers = [p.strip() for p in spec.consumer_projects if p.strip()]

    if share_type == "SPECIFIC_PROJECTS":
        if not cleaned_consumers:
            errors.append(
                "Shared reservation (`SPECIFIC_PROJECTS`) requires between 1 and 100 consumer project IDs in `consumer_projects`."
            )
        if spec.owner_project_id in cleaned_consumers:
            errors.append(
                f"Owner project '{spec.owner_project_id}' must NOT be listed in `consumer_projects` (`--share-with` / `share_settings.project_map`). "
                "The owner project is included automatically, and the GCE API rejects requests that list it."
            )
        if len(cleaned_consumers) > MAX_CONSUMER_PROJECTS:
            errors.append(
                f"Shared reservation has {len(cleaned_consumers)} consumer projects, which exceeds the 100 consumer project limit "
                "(`--share-setting=projects` supports 1 to 100 consumer projects in the same GCP Organization). "
                "To scale beyond 100 projects: (1) shard into multiple shared reservations (up to 100 shared reservations per unique VM shape per org), "
                "or (2) use beta folder-level sharing (`DIRECT_PROJECTS_UNDER_SPECIFIC_FOLDERS`, up to 128 folders, requires `--require-specific-reservation`)."
            )

        best_practices.append(
            f"Dedicated Owner Project Best Practice: Create shared reservations in a dedicated owner project ('{spec.owner_project_id}') "
            "that does not run VMs itself. The owner project consumes quota for the full reservation at creation time, plus additional quota if it also runs VMs; "
            "consumer projects only consume quota when they create VMs."
        )
        best_practices.append(
            f"Organization Policy Requirement: Ensure owner project '{spec.owner_project_id}' is allowlisted in the "
            "`constraints/compute.sharedReservationsOwnerProjects` Organization Policy constraint."
        )
        warnings.append(
            "Billing Alignment Warning: Consumer projects should share the same Cloud Billing account as the owner project "
            f"('{spec.owner_project_id}'), especially when applying resource-based Committed Use Discounts (CUDs)."
        )

    elif share_type == "DIRECT_PROJECTS_UNDER_SPECIFIC_FOLDERS":
        if not spec.shared_folders:
            errors.append(
                "Folder-shared reservation (`DIRECT_PROJECTS_UNDER_SPECIFIC_FOLDERS`) requires 1 to 128 folder IDs in `shared_folders`."
            )
        elif len(spec.shared_folders) > MAX_SHARED_FOLDERS:
            errors.append(
                f"Folder-shared reservation has {len(spec.shared_folders)} folders, exceeding the maximum of {MAX_SHARED_FOLDERS} folders."
            )
        if not spec.specific_reservation_required:
            errors.append(
                "Folder-level sharing (`DIRECT_PROJECTS_UNDER_SPECIFIC_FOLDERS`) requires `specific_reservation_required = True` (`--require-specific-reservation`)."
            )

    # 4. Compact Placement Policy Constraints
    if spec.compact_placement_policy:
        if share_type != "LOCAL":
            errors.append(
                "Compact placement policies cannot be used with shared reservations (must be `LOCAL` single-project)."
            )
        if not spec.specific_reservation_required:
            errors.append(
                "Compact placement policies require `specific_reservation_required = True` (`--require-specific-reservation`)."
            )

    # Generate Terraform HCL & gcloud CLI
    terraform_hcl = _build_terraform_hcl(spec, share_type, cleaned_consumers)
    gcloud_cmd = _build_gcloud_command(spec, share_type, cleaned_consumers)
    targeting_gcloud, targeting_tf = _build_consumer_targeting(spec, share_type, cleaned_consumers)

    shape_desc = spec.machine_type or f"template:{spec.source_instance_template}"
    billing_impact_summary = (
        f"IMMEDIATE BILLING IMPACT: Provisioning reservation '{name}' in '{spec.owner_project_id}' ({spec.zone}) "
        f"immediately bills for {spec.vm_count}x {shape_desc} VM(s) at standard GCE on-demand rates (covered by applicable CUDs) "
        "whether or not VMs are running, until the reservation is deleted."
    )

    return ReservationDesignResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        best_practices=best_practices,
        billing_impact_summary=billing_impact_summary,
        terraform_hcl=terraform_hcl,
        gcloud_command=gcloud_cmd,
        consumer_targeting_gcloud=targeting_gcloud,
        consumer_targeting_terraform=targeting_tf,
        normalized_spec={
            "reservation_name": name,
            "owner_project_id": spec.owner_project_id,
            "zone": spec.zone,
            "region": "-".join(spec.zone.split("-")[:-1]) if "-" in spec.zone else spec.zone,
            "vm_count": spec.vm_count,
            "machine_type": spec.machine_type,
            "source_instance_template": spec.source_instance_template,
            "min_cpu_platform": spec.min_cpu_platform,
            "specific_reservation_required": spec.specific_reservation_required,
            "share_type": share_type,
            "consumer_projects": [p for p in cleaned_consumers if p != spec.owner_project_id],
            "shared_folders": spec.shared_folders,
            "compact_placement_policy": spec.compact_placement_policy,
        },
    )


def _build_terraform_hcl(
    spec: ReservationDesignInput, share_type: str, consumers: List[str]
) -> str:
    safe_tf_name = re.sub(r"[^a-zA-Z0-9_]", "_", spec.reservation_name)
    lines = [
        f'resource "google_compute_reservation" "{safe_tf_name}" {{',
        f'  name                          = "{spec.reservation_name}"',
        f'  project                       = "{spec.owner_project_id}"',
        f'  zone                          = "{spec.zone}"',
        f'  specific_reservation_required = {str(spec.specific_reservation_required).lower()}',
        "",
        "  specific_reservation {",
        f"    count = {spec.vm_count}",
    ]

    if spec.source_instance_template:
        lines.append(f'    source_instance_template = "{spec.source_instance_template}"')
    else:
        lines.append("    instance_properties {")
        lines.append(f'      machine_type     = "{spec.machine_type or "e2-standard-2"}"')
        if spec.min_cpu_platform:
            lines.append(f'      min_cpu_platform = "{spec.min_cpu_platform}"')
        for acc in spec.accelerators:
            lines.append("      guest_accelerators {")
            lines.append(f'        accelerator_type  = "{acc.accelerator_type}"')
            lines.append(f"        accelerator_count = {acc.accelerator_count}")
            lines.append("      }")
        for ssd in spec.local_ssds:
            lines.append("      local_ssds {")
            lines.append(f'        interface    = "{ssd.interface}"')
            lines.append(f"        disk_size_gb = {ssd.disk_size_gb}")
            lines.append("      }")
        lines.append("    }")

    lines.append("  }")

    if share_type == "SPECIFIC_PROJECTS":
        valid_consumers = [p for p in consumers if p != spec.owner_project_id]
        lines.append("")
        lines.append("  share_settings {")
        lines.append('    share_type = "SPECIFIC_PROJECTS"')
        for proj in valid_consumers:
            lines.append("    project_map {")
            lines.append(f'      id         = "{proj}"')
            lines.append(f'      project_id = "{proj}"')
            lines.append("    }")
        lines.append("  }")

    if spec.compact_placement_policy:
        lines.append("")
        lines.append("  resource_policies = {")
        lines.append(f'     policy = "{spec.compact_placement_policy}"')
        lines.append("  }")

    lines.append("}")
    return "\n".join(lines)


def _build_gcloud_command(
    spec: ReservationDesignInput, share_type: str, consumers: List[str]
) -> str:
    parts = [
        f"gcloud compute reservations create {spec.reservation_name}",
        f"  --project={spec.owner_project_id}",
        f"  --zone={spec.zone}",
        f"  --vm-count={spec.vm_count}",
    ]

    if spec.source_instance_template:
        parts.append(f"  --source-instance-template={spec.source_instance_template}")
    else:
        parts.append(f"  --machine-type={spec.machine_type or 'e2-standard-2'}")
        if spec.min_cpu_platform:
            parts.append(f'  --min-cpu-platform="{spec.min_cpu_platform}"')
        for acc in spec.accelerators:
            parts.append(
                f"  --accelerator=count={acc.accelerator_count},type={acc.accelerator_type}"
            )
        for ssd in spec.local_ssds:
            parts.append(f"  --local-ssd=interface={ssd.interface},size={ssd.disk_size_gb}")

    if spec.specific_reservation_required:
        parts.append("  --require-specific-reservation")

    if share_type == "SPECIFIC_PROJECTS":
        valid_consumers = [p for p in consumers if p != spec.owner_project_id]
        parts.append("  --share-setting=projects")
        if valid_consumers:
            parts.append(f"  --share-with={','.join(valid_consumers)}")
    elif share_type == "DIRECT_PROJECTS_UNDER_SPECIFIC_FOLDERS":
        parts.append("  --share-setting=folders")
        if spec.shared_folders:
            parts.append(f"  --share-with={','.join(spec.shared_folders)}")

    if spec.compact_placement_policy:
        parts.append(f"  --resource-policies=policy={spec.compact_placement_policy}")

    return " \\\n".join(parts)


def _build_consumer_targeting(
    spec: ReservationDesignInput, share_type: str, consumers: List[str]
) -> tuple[str, str]:
    target_project = (
        consumers[0]
        if (share_type == "SPECIFIC_PROJECTS" and consumers)
        else spec.owner_project_id
    )
    mt = spec.machine_type or "e2-standard-2"

    if spec.specific_reservation_required:
        if share_type != "LOCAL":
            res_Target = f"projects/{spec.owner_project_id}/reservations/{spec.reservation_name}"
        else:
            res_Target = spec.reservation_name

        gcloud_example = (
            f"# Consumer VM targeting specific reservation:\n"
            f"gcloud compute instances create sample-consumer-vm \\\n"
            f"  --project={target_project} \\\n"
            f"  --zone={spec.zone} \\\n"
            f"  --machine-type={mt} \\\n"
            f"  --reservation-affinity=specific \\\n"
            f"  --reservation={res_Target}"
        )
        tf_example = (
            f'# Terraform google_compute_instance reservation_affinity block:\n'
            f'reservation_affinity {{\n'
            f'  type = "SPECIFIC_RESERVATION"\n'
            f'  specific_reservation {{\n'
            f'    key    = "compute.googleapis.com/reservation-name"\n'
            f'    values = ["{res_Target}"]\n'
            f'  }}\n'
            f'}}'
        )
    else:
        gcloud_example = (
            f"# Automatic consumption (matching VMs in {target_project} / {spec.zone} consume automatically):\n"
            f"gcloud compute instances create sample-consumer-vm \\\n"
            f"  --project={target_project} \\\n"
            f"  --zone={spec.zone} \\\n"
            f"  --machine-type={mt} \\\n"
            f"  --reservation-affinity=any"
        )
        tf_example = (
            f'# Terraform google_compute_instance reservation_affinity block (default):\n'
            f'reservation_affinity {{\n'
            f'  type = "ANY_RESERVATION"\n'
            f'}}'
        )
    return gcloud_example, tf_example
