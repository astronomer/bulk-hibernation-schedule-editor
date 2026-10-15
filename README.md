# Bulk Hibernation Schedule Editor

A Python script that sets [hibernation schedules](https://www.astronomer.io/docs/astro/deployment-resources#hibernate-a-development-deployment) on many Astro Deployments at once, driven by a YAML config file and the [Astro API](https://www.astronomer.io/docs/astro/api/v-1/deployment/update-a-deployment).

It is **dry-run by default**: it shows what would change for each Deployment and only writes when you pass `--apply`.

## Prerequisites

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- The [Astro CLI](https://www.astronomer.io/docs/astro/cli/overview), logged in to the Organization that owns the Deployments
- Permission to update the target Deployments. Creating or editing hibernation schedules requires `deployment.update` (included in Workspace Operator, Workspace Owner, Organization Owner, and Deployment Admin).
- Target Deployments must have **Development Mode** turned on and **High Availability** turned off. Hibernation isn't available otherwise.

## Setup

```bash
uv sync
```

## Authentication

The script reuses your Astro CLI session; there is no separate API token to manage. It reads the token and Organization ID of the current context from `~/.astro/config.yaml`.

```bash
astro login
astro organization switch <organization-id>   # if you belong to more than one org
```

CLI tokens are short-lived. If the token has expired, the script exits and tells you to run `astro login`. The script never refreshes the token itself.

The script always prints the Organization name and ID it's acting on before it does anything. Check it.

## Configuration

Copy the example and edit it:

```bash
cp hibernation_schedules.example.yaml hibernation_schedules.yaml
```

```yaml
# Optional guard: abort if the astro CLI is pointed at a different organization.
organization_id: <your-organization-id>

schedule_groups:
  business_hours_central:
    schedules:
      - description: nights
        hibernate_at_cron: "0 23 * * 1-5"
        wake_at_cron: "0 13 * * 1-5"
        enabled: true
    deployments:
      - <deployment-id>
      - <deployment-id>
```

| Field | Required | Notes |
|---|---|---|
| `organization_id` | No | If set, the run aborts when the CLI's current org is different. Recommended. Remove the line if you don't want the guard. |
| `schedule_groups` | Yes | Named groups. Each group's schedules apply to every Deployment listed under it. |
| `schedules` | Yes | List of schedules. Use `schedules: []` to **remove** all schedules from the listed Deployments. |
| `hibernate_at_cron` / `wake_at_cron` | Yes | 5-part cron expressions, evaluated in **UTC**. |
| `enabled` | No | Defaults to `true`. |
| `description` | No | Shown in the Astro UI. |
| `deployments` | Yes | Deployment IDs. A Deployment can appear in only one group. |

**Timezones:** crons are UTC, so local-time schedules shift by an hour across daylight saving changes. For example, 6 PM Central is `0 23` UTC during CDT and `0 0` UTC during CST.

## Usage

```bash
# Dry run: show current vs. desired schedules for each Deployment
uv run main.py hibernation_schedules.yaml

# Apply
uv run main.py hibernation_schedules.yaml --apply

# Also update Deployments that have secret environment variables (see below)
uv run main.py hibernation_schedules.yaml --apply --include-secret-deployments
```

Each Deployment ends in one of these states, followed by a summary table:

| Status | Meaning |
|---|---|
| `WOULD UPDATE` | Dry run: schedules differ and would be replaced. |
| `UPDATED` | Schedules were replaced and verified. |
| `NO CHANGE` | Schedules already match the config; nothing sent. |
| `SKIPPED` | Deployment isn't eligible (reason shown). |
| `FAILED` | API error, or verification after update failed (reason shown). |

The run continues past failures. Exit codes: `0` success, `1` at least one Deployment failed, `2` config or auth error (nothing was processed).

## How It Works

The Astro API has no endpoint that only changes the hibernation schedule. Schedules live in `scalingSpec.hibernationSpec.schedules` on the [Update Deployment](https://www.astronomer.io/docs/astro/api/v-1/deployment/update-a-deployment) endpoint, which replaces the **entire** Deployment configuration. So for each Deployment, the script:

1. **Reads** the Deployment with `GET /organizations/{org}/deployments/{id}`.
2. **Checks eligibility** and skips the Deployment if any of these apply:
   - Development Mode is off.
   - High Availability is on.
   - It's a Hybrid Deployment (not supported by this script).
   - It has secret env vars and `--include-secret-deployments` wasn't passed.
3. **Compares** current and desired schedules. If they match, nothing is sent.
4. **Rebuilds the full update body** from the current config: name, executor, scheduler size, worker queues, env vars, workload identity, remote execution settings, and so on. Only the schedules are swapped in. Any active manual hibernate or wake override is preserved. Existing schedules are **replaced**, not merged.
5. **Updates** the Deployment with `POST /organizations/{org}/deployments/{id}`.
6. **Verifies** the update by reading the Deployment again and confirming that:
   - the schedules match the config, and
   - no other configuration changed.

   If anything else changed, the Deployment is reported as `FAILED`, with the names of the fields that changed.

### Secret Environment Variables

The API never returns the values of secret env vars, so the script sends them back with no value. The API keeps the stored value in that case, but this isn't documented. Because of that, Deployments with secrets are skipped unless you pass `--include-secret-deployments`.

This behavior was verified on 2026-10-15 against a test Deployment: a secret env var was read correctly by a DAG task both before and after the script updated the Deployment. If you want extra assurance, repeat that test on a disposable Deployment before bulk runs.

## Recommendations

- Always do a dry run first and read the Organization line at the top of the output.
- Try `--apply` on one or two Deployments before a large batch.
- Keep the config in version control so schedule changes are reviewed.

## Limitations

- **Tested setup:** the script was tested on a Dedicated cluster Deployment (AWS, Astro executor). Standard, Celery, Kubernetes executor, and Remote Execution Deployments haven't been tested. The post-update verification will flag unexpected changes, but test on one Deployment first.
- **Hybrid:** Hybrid Deployments aren't supported.
- **Targeting:** Deployments are targeted by explicit ID only; there's no workspace-wide or name-pattern selection.
- **Concurrent edits:** Deployments are updated one at a time. If someone edits a Deployment in the UI between the script's read and write, the script's write overwrites that edit.
