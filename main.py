"""Bulk-set Astro Deployment hibernation schedules from a YAML config.

Auth comes from the current `astro` CLI context (~/.astro/config.yaml): the
token and Organization ID of whatever org you last logged in / switched to.

Dry-run by default; pass --apply to write.

The Astro API has no schedule-only endpoint: hibernation schedules are part of
the full Update Deployment body. This script GETs each Deployment, rebuilds the
update body from its current config, swaps in the desired
`scalingSpec.hibernationSpec.schedules`, and POSTs it back. After applying, it
re-reads the Deployment and verifies nothing other than the schedules changed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import requests
import yaml

API_BASE = "https://api.astronomer.io/v1"
ASTRO_CONFIG = Path(os.path.expanduser("~/.astro/config.yaml"))
TIMEOUT = 30

# Top-level fields copied verbatim from GET into the update body (when present).
PASSTHROUGH_FIELDS = [
    "name",
    "workspaceId",
    "type",
    "executor",
    "isCicdEnforced",
    "isDagDeployEnabled",
    "isHighAvailability",
    "schedulerSize",
    "isDevelopmentMode",
    "contactEmails",
    "description",
    "defaultTaskPodCpu",
    "defaultTaskPodMemory",
    "resourceQuotaCpu",
    "resourceQuotaMemory",
]
WORKER_QUEUE_FIELDS = [
    "id",
    "name",
    "isDefault",
    "maxWorkerCount",
    "minWorkerCount",
    "workerConcurrency",
    "astroMachine",
    "podEphemeralStorage",
    "nodePoolId",
]
REMOTE_EXECUTION_FIELDS = ["enabled", "allowedIpAddressRanges", "taskLogBucket", "taskLogUrlPattern"]


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------------- auth


@dataclass
class AstroContext:
    token: str
    organization_id: str
    context_name: str


def load_astro_context() -> AstroContext:
    if not ASTRO_CONFIG.exists():
        raise ConfigError(f"{ASTRO_CONFIG} not found. Run `astro login` first.")
    cfg = yaml.safe_load(ASTRO_CONFIG.read_text()) or {}
    context_name = cfg.get("context")
    if not context_name:
        raise ConfigError("No current astro CLI context. Run `astro login` first.")
    ctx = (cfg.get("contexts") or {}).get(context_name.replace(".", "_"))
    if not ctx or not ctx.get("token"):
        raise ConfigError(f"astro CLI context '{context_name}' has no token. Run `astro login`.")
    if not ctx.get("organization"):
        raise ConfigError("astro CLI context has no organization. Run `astro organization switch`.")

    expires = ctx.get("expiresin")
    if expires:
        expires_at = expires if isinstance(expires, dt.datetime) else dt.datetime.fromisoformat(str(expires))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=dt.timezone.utc)
        if expires_at <= dt.datetime.now(dt.timezone.utc):
            raise ConfigError(
                f"astro CLI token expired at {expires_at.isoformat()}. "
                "Run `astro login` (or any `astro` command) to refresh it."
            )

    token = ctx["token"]
    if not token.lower().startswith("bearer "):
        token = f"Bearer {token}"
    return AstroContext(token=token, organization_id=ctx["organization"], context_name=context_name)


class AstroClient:
    def __init__(self, ctx: AstroContext):
        self.org_id = ctx.organization_id
        self.session = requests.Session()
        self.session.headers.update({"Authorization": ctx.token, "Content-Type": "application/json"})

    def _request(self, method: str, path: str, **kwargs) -> dict:
        resp = self.session.request(method, f"{API_BASE}{path}", timeout=TIMEOUT, **kwargs)
        if resp.status_code == 401:
            raise ConfigError("Astro API returned 401. Token is invalid or expired; run `astro login`.")
        if not resp.ok:
            try:
                body = resp.json()
                msg = body.get("message", resp.text)
                field_errors = body.get("fieldErrors")
                if field_errors:
                    msg += " " + "; ".join(f"{e.get('field')}: {e.get('message')}" for e in field_errors)
                msg += f" (requestId={body.get('requestId')})"
            except ValueError:
                msg = resp.text
            raise ApiError(resp.status_code, msg)
        return resp.json() if resp.content else {}

    def get_organization(self) -> dict:
        return self._request("GET", f"/organizations/{self.org_id}")

    def get_deployment(self, deployment_id: str) -> dict:
        return self._request("GET", f"/organizations/{self.org_id}/deployments/{deployment_id}")

    def update_deployment(self, deployment_id: str, body: dict) -> dict:
        return self._request("POST", f"/organizations/{self.org_id}/deployments/{deployment_id}", json=body)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


# --------------------------------------------------------------------------- config


@dataclass
class Plan:
    organization_id: str | None
    # deployment_id -> (group_name, schedules in API shape)
    targets: dict[str, tuple[str, list[dict]]] = field(default_factory=dict)


def _validate_cron(expr: str, where: str) -> str:
    if not isinstance(expr, str) or len(expr.split()) != 5:
        raise ConfigError(f"{where}: '{expr}' is not a 5-part cron expression")
    return expr.strip()


def load_plan(path: Path) -> Plan:
    raw = yaml.safe_load(path.read_text()) or {}
    groups = raw.get("schedule_groups")
    if not isinstance(groups, dict) or not groups:
        raise ConfigError("config must define a non-empty 'schedule_groups' mapping")

    plan = Plan(organization_id=raw.get("organization_id"))
    for group_name, group in groups.items():
        where = f"schedule_groups.{group_name}"
        schedules_raw = group.get("schedules")
        if schedules_raw is None or not isinstance(schedules_raw, list):
            raise ConfigError(f"{where}.schedules must be a list (use [] to remove all schedules)")
        schedules = []
        for i, s in enumerate(schedules_raw):
            swhere = f"{where}.schedules[{i}]"
            api_schedule = {
                "hibernateAtCron": _validate_cron(s.get("hibernate_at_cron"), f"{swhere}.hibernate_at_cron"),
                "wakeAtCron": _validate_cron(s.get("wake_at_cron"), f"{swhere}.wake_at_cron"),
                "isEnabled": bool(s.get("enabled", True)),
            }
            if s.get("description"):
                api_schedule["description"] = str(s["description"])
            schedules.append(api_schedule)

        deployments = group.get("deployments") or []
        if not deployments:
            raise ConfigError(f"{where}.deployments must list at least one deployment ID")
        for dep_id in deployments:
            dep_id = str(dep_id).strip()
            if dep_id in plan.targets:
                raise ConfigError(
                    f"deployment {dep_id} is listed in both '{plan.targets[dep_id][0]}' and '{group_name}'"
                )
            plan.targets[dep_id] = (group_name, schedules)
    return plan


# --------------------------------------------------------------------------- update body


def build_update_body(dep: dict, schedules: list[dict] | None = None) -> dict:
    """Rebuild a full Update Deployment body from a GET response.

    If `schedules` is None, the Deployment's current schedules are kept.
    """
    body = {k: dep[k] for k in PASSTHROUGH_FIELDS if dep.get(k) is not None}

    # Secret values are never returned by GET. Send secrets back with no value so the
    # API keeps the stored value (verified by the pilot test; see README notes).
    env_vars = []
    for ev in dep.get("environmentVariables") or []:
        item = {"key": ev["key"], "isSecret": bool(ev.get("isSecret"))}
        if not item["isSecret"]:
            item["value"] = ev.get("value", "")
        env_vars.append(item)
    body["environmentVariables"] = env_vars

    remote = dep.get("remoteExecution")
    if remote:
        body["remoteExecution"] = {k: remote[k] for k in REMOTE_EXECUTION_FIELDS if remote.get(k) is not None}

    remote_enabled = bool(remote and remote.get("enabled"))
    if dep.get("executor") == "CELERY" or (dep.get("executor") == "ASTRO" and not remote_enabled):
        body["workerQueues"] = [
            {k: q[k] for k in WORKER_QUEUE_FIELDS if q.get(k) is not None} for q in dep.get("workerQueues") or []
        ]

    if dep.get("effectiveWorkloadIdentity"):
        body["workloadIdentity"] = dep["effectiveWorkloadIdentity"]
    if dep.get("effectiveDRWorkloadIdentity"):
        body["drWorkloadIdentity"] = dep["effectiveDRWorkloadIdentity"]

    current_spec = ((dep.get("scalingSpec") or {}).get("hibernationSpec")) or {}
    hibernation_spec: dict = {
        "schedules": (current_spec.get("schedules") or []) if schedules is None else schedules
    }
    # Preserve any active manual hibernate/wake override.
    if current_spec.get("override"):
        override = {k: v for k, v in current_spec["override"].items() if k in ("isHibernating", "overrideUntil")}
        if override:
            hibernation_spec["override"] = override
    body["scalingSpec"] = {"hibernationSpec": hibernation_spec}
    return body


def normalize_schedules(schedules: list[dict] | None) -> list[tuple]:
    return sorted(
        (
            s.get("hibernateAtCron", "").strip(),
            s.get("wakeAtCron", "").strip(),
            bool(s.get("isEnabled")),
            s.get("description") or "",
        )
        for s in schedules or []
    )


def current_schedules(dep: dict) -> list[dict]:
    return (((dep.get("scalingSpec") or {}).get("hibernationSpec")) or {}).get("schedules") or []


def fmt_schedules(schedules: list[dict]) -> str:
    if not schedules:
        return "      (none)"
    lines = []
    for s in schedules:
        state = "on " if s.get("isEnabled") else "off"
        desc = f"  # {s['description']}" if s.get("description") else ""
        lines.append(f"      [{state}] hibernate '{s['hibernateAtCron']}'  wake '{s['wakeAtCron']}'{desc}")
    return "\n".join(lines)


def config_drift(before: dict, after: dict) -> list[str]:
    """Fields (other than hibernation schedules) whose value changed between two GETs."""
    b = build_update_body(before)
    a = build_update_body(after)
    for d in (a, b):
        d["scalingSpec"]["hibernationSpec"].pop("schedules", None)
    keys = sorted(set(a) | set(b))
    return [k for k in keys if json.dumps(a.get(k), sort_keys=True) != json.dumps(b.get(k), sort_keys=True)]


# --------------------------------------------------------------------------- main


@dataclass
class Result:
    deployment_id: str
    name: str
    status: str  # UPDATED / WOULD UPDATE / NO CHANGE / SKIPPED / FAILED
    detail: str = ""


def process(
    client: AstroClient, dep_id: str, group: str, desired: list[dict], apply: bool, include_secret: bool
) -> Result:
    try:
        dep = client.get_deployment(dep_id)
    except ApiError as e:
        return Result(dep_id, "?", "FAILED", str(e))

    name = dep.get("name", "?")
    print(f"\n- {name} ({dep_id})  [group: {group}]")

    if dep.get("type") == "HYBRID":
        return Result(dep_id, name, "SKIPPED", "Hybrid deployments are not supported by this script")
    if not dep.get("isDevelopmentMode"):
        return Result(dep_id, name, "SKIPPED", "not in Development Mode (hibernation unavailable)")
    if dep.get("isHighAvailability"):
        return Result(dep_id, name, "SKIPPED", "high availability is on (incompatible with hibernation)")

    secrets = [ev["key"] for ev in dep.get("environmentVariables") or [] if ev.get("isSecret")]
    if secrets and not include_secret:
        return Result(
            dep_id,
            name,
            "SKIPPED",
            f"has {len(secrets)} secret env var(s); re-run with --include-secret-deployments",
        )

    existing = current_schedules(dep)
    print("    current:")
    print(fmt_schedules(existing))
    print("    desired:")
    print(fmt_schedules(desired))

    if normalize_schedules(existing) == normalize_schedules(desired):
        return Result(dep_id, name, "NO CHANGE")

    if not apply:
        extra = f"; {len(secrets)} secret env var(s) will be sent back without values" if secrets else ""
        return Result(dep_id, name, "WOULD UPDATE", extra.lstrip("; "))

    try:
        client.update_deployment(dep_id, build_update_body(dep, desired))
        after = client.get_deployment(dep_id)
    except ApiError as e:
        return Result(dep_id, name, "FAILED", str(e))

    if normalize_schedules(current_schedules(after)) != normalize_schedules(desired):
        return Result(dep_id, name, "FAILED", "update accepted but schedules read back differ from desired")
    drift = config_drift(dep, after)
    if drift:
        return Result(dep_id, name, "FAILED", f"schedules set, but other config changed: {', '.join(drift)}")
    return Result(dep_id, name, "UPDATED")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", type=Path, help="YAML file with schedule_groups")
    parser.add_argument("--apply", action="store_true", help="Actually update deployments (default: dry run)")
    parser.add_argument(
        "--include-secret-deployments",
        action="store_true",
        help="Also update deployments that have secret env vars (sent back without values)",
    )
    args = parser.parse_args()

    try:
        plan = load_plan(args.config)
        ctx = load_astro_context()
        if plan.organization_id and plan.organization_id != ctx.organization_id:
            raise ConfigError(
                f"config organization_id {plan.organization_id} != astro CLI org {ctx.organization_id}. "
                f"Run `astro organization switch {plan.organization_id}`."
            )
        client = AstroClient(ctx)
        org = client.get_organization()
    except (ConfigError, ApiError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    mode = "APPLY" if args.apply else "DRY RUN (no changes; pass --apply to write)"
    print(f"Organization: {org.get('name')} ({ctx.organization_id})")
    print(f"Mode: {mode}")
    print(f"Targets: {len(plan.targets)} deployment(s)")

    results = []
    for dep_id, (group, desired) in plan.targets.items():
        try:
            r = process(client, dep_id, group, desired, args.apply, args.include_secret_deployments)
        except ConfigError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        print(f"    => {r.status}{': ' + r.detail if r.detail else ''}")
        results.append(r)

    print("\nSummary")
    w = max(len(r.name) for r in results)
    for r in results:
        print(f"  {r.status:<13} {r.name:<{w}}  {r.deployment_id}  {r.detail}")

    return 1 if any(r.status == "FAILED" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
