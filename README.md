# GCE On-Demand Reservation Architect (`gce-reservation-agent`)

An AI Architect agent powered by **Gemini 3.8 Flash** (`google-genai` SDK on Vertex AI) for designing, validating, inspecting, and provisioning **Google Cloud Compute Engine (GCE)** on-demand capacity reservations (both `LOCAL` single-project and `SPECIFIC_PROJECTS` shared reservations across up to 100 consumer projects).

Built from [`gce-reservation-agent.md`](./gce-reservation-agent.md) and deployed to **Cloud Run** in project `merdogdu-sandbox-398713`.

---

## Key Capabilities & Enforced Guardrails

### 1. Deterministic Validation Rules (`app/validator.py`)
1. **Machine Type & Provisioning Model Restrictions**
   - Rejects legacy N1 fractional shared-core types (`f1-micro`, `g1-small`) disallowed by the GCE control plane (`"This machine type is disallowed for reservations."`).
   - Recommends supported alternatives (`e2-micro`, `e2-small`, `e2-medium`, `n1-standard-1`, `e2-standard-2`).
   - Rejects Spot/Preemptible VMs, Flex-start VMs, and sole-tenant nodes for on-demand VM reservations.
2. **Capacity & Immutability Limits**
   - Enforces `1 <= vm_count <= 1,000` (`specificReservation.count <= 1000`).
   - Documents immutable fields (`name`, `zone`, `machine_type`, `min_cpu_platform`, `accelerators`, `local_ssds`).
3. **Shared Reservation Rules (Multi-Project)**
   - Enforces **1 to 100 consumer projects** (`--share-setting=projects` / `SPECIFIC_PROJECTS`).
   - Automatically prevents listing `owner_project_id` inside `--share-with` / `share_settings.project_map` (which causes API rejection).
   - Recommends the **Dedicated Owner Project** pattern, checks the `constraints/compute.sharedReservationsOwnerProjects` Org Policy, warns on Cloud Billing account alignment for CUDs, and advises sharding or folder-level sharing (`DIRECT_PROJECTS_UNDER_SPECIFIC_FOLDERS`, up to 128 folders) when scaling beyond 100 consumer projects.
4. **Compact Placement Policy Constraints**
   - Rejects compact placement policies on shared reservations (must be `LOCAL`) and enforces `--require-specific-reservation` (`specific_reservation_required = true`).

### 2. Live GCP Inspection, RBAC & Two-Stage Mutation Safety Gate (`app/gcp_tools.py`)
- **Strict RBAC Authorization Policy**:
  - Only `admin@merdogdu.altostrat.com` (`AUTHORIZED_RESERVATION_ADMIN`) is permitted to stage or execute GCE reservation mutations (`CREATE`, `RESIZE`, `DELETE`).
  - Any other caller identity is immediately rejected at both the agent/tool layer and the REST endpoint (`HTTP 403 Forbidden`) with:
    `Access Denied: You (<caller_email>) have no rights to make or modify a reservation for this app. Only admin@merdogdu.altostrat.com is authorized to provision, resize, or delete GCE reservations.`
- **Read-Only Live Tools (Run Automatically)**:
  - `list_gce_reservations`: Queries live GCE reservations across a zone or all zones (`aggregated_list`).
  - `describe_gce_reservation`: Inspects a specific reservation's capacity, `in_use_count`, and share settings.
  - `check_region_quotas`: Inspects live GCE regional quotas (`CPUS`, `E2_CPUS`, `N2_CPUS`, `GPUs`, `LOCAL_SSD_TOTAL_GB`).
  - `check_shared_reservation_org_policy`: Queries the effective `compute.sharedReservationsOwnerProjects` Organization Policy on the owner project.
- **Explicit Approval Gate for Mutations**:
  - `stage_reservation_mutation` creates a pending approval ticket (`act-...`) with the exact `gcloud` command, `google_compute_reservation` Terraform resource block, and billing impact statement.
  - Mutations (`CREATE`, `RESIZE`, `DELETE`) are **never** executed without explicit user confirmation (either via the **Approve & Execute on GCP** button in the Web UI or in a subsequent confirmation turn).

---

## Project Structure

- [`gce-reservation-agent.md`](./gce-reservation-agent.md) — Agent specification, guardrails, validation rules, and workflow loaded directly as the system instruction.
- [`app/validator.py`](./app/validator.py) — Deterministic GCE reservation validator and Terraform/`gcloud`/consumer-affinity code generator.
- [`app/gcp_tools.py`](./app/gcp_tools.py) — Live Google Cloud Compute Engine (`google-cloud-compute`) and Org Policy (`google-cloud-org-policy`) tools + approval ticket queue.
- [`app/agent.py`](./app/agent.py) — Multi-turn Gemini (`gemini-3.8-flash`) tool-calling agent orchestrator with same-turn mutation guardrail enforcement.
- [`app/main.py`](./app/main.py) — FastAPI REST backend & static asset server.
- [`app/static/index.html`](./app/static/index.html) — Interactive Architect Console Web UI (Chat, Approval Gate cards, Terraform/`gcloud` Workbench, and Live GCP Telemetry).
- [`Dockerfile`](./Dockerfile) — Container definition for Cloud Run.

---

## Deploying to Cloud Run (`merdogdu-sandbox-398713`)

```bash
gcloud run deploy gce-reservation-agent \
  --source=. \
  --project=merdogdu-sandbox-398713 \
  --region=us-central1 \
  --service-account=gce-reservation-agent-sa@merdogdu-sandbox-398713.iam.gserviceaccount.com \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=merdogdu-sandbox-398713,GOOGLE_CLOUD_LOCATION=global,GEMINI_MODEL=gemini-3.8-flash" \
  --allow-unauthenticated
```
