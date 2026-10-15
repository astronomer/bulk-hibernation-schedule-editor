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

## Other Ways to Manage Hibernation

This script manages **schedules** in bulk. For one-off changes, or to temporarily ignore a schedule, use the Astro UI or the manual overrides below.

### Change a Schedule in the Astro UI

1. In the Astro UI, click **Deployments**, then select the Deployment.
2. Click **Edit** at the top of the Deployment page.
3. In the **Advanced** section, find **Wake Schedules**. Add, edit, or remove schedules using the calendar-based interface or cron expressions. Switch between them with **Switch to cron scheduling** / **Switch to simple scheduling**.
   - To keep a Deployment hibernated indefinitely, choose the **Never Wake** schedule type. In cron mode, this is `@always` as the hibernate expression and `@never` as the wake expression. Only one **Never Wake** schedule can be enabled at a time.
4. Click **Update Deployment**.

Cron schedules are always in UTC, in the UI as well as in this script.

> **Note:** This script **replaces** a Deployment's schedules. If a Deployment is listed in your config, the next `--apply` run overwrites any schedule changes made in the UI. Update the YAML config instead, or remove the Deployment from it.

See [Create a wake schedule](https://www.astronomer.io/docs/astro/deployment-resources#create-a-wake-schedule).

### Manually Override the Schedule

An **override** hibernates or wakes a Deployment regardless of its schedules, either until a set time or until you remove it. Overrides don't change the schedules themselves. When an override ends or is removed, the Deployment goes back to following its schedules.

Setting or removing an override requires the `deployment.hibernation.update` permission. Editing schedules requires `deployment.update`.

#### Astro UI

To hibernate now:

1. Click **Deployments**, then select the Deployment.
2. Open the **More actions** menu (⋯) and select **Hibernate Deployment**.
3. Configure the hibernation period, then click **Confirm**.

To wake up, or to return to the normal schedule:

1. Open the Deployment's **More actions** menu (⋯) and select **Wake Up Deployment**.
2. Choose one of the following:
   - **Wake until further notice**: wake now and ignore schedules until you remove the override.
   - **Wake until set time and date**: wake now and go back to the schedule at the time you set.
   - **Remove override and return to normal schedule**: cancel any override.
3. Click **Confirm**.

#### Astro CLI

```bash
# Hibernate: indefinitely, for a duration, or until a UTC time
astro deployment hibernate <deployment-id>
astro deployment hibernate <deployment-id> --for 2h30m
astro deployment hibernate <deployment-id> --until 2026-10-20T13:00:00Z

# Wake up: same options
astro deployment wake-up <deployment-id>
astro deployment wake-up <deployment-id> --for 4h
astro deployment wake-up <deployment-id> --until 2026-10-15T23:00:00Z

# Remove any override and return to the schedule
astro deployment wake-up <deployment-id> --remove-override
```

Add `--force` to skip the confirmation prompt. See [`astro deployment hibernate`](https://www.astronomer.io/docs/cli/v1.46/astro-deployment-hibernate) and [`astro deployment wake-up`](https://www.astronomer.io/docs/cli/v1.46/astro-deployment-wake-up).

#### Astro API

Unlike schedules, overrides have their own endpoint, so you don't have to send the full Deployment config.

```bash
# Hibernate until a UTC time ("isHibernating": false wakes it instead).
# Omit overrideUntil to keep the override until it's removed.
curl -X POST "https://api.astronomer.io/v1/organizations/$ORG_ID/deployments/$DEPLOYMENT_ID/hibernation-override" \
  -H "Authorization: Bearer $ASTRO_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"isHibernating": true, "overrideUntil": "2026-10-20T13:00:00Z"}'

# Remove the override and return to the schedule
curl -X DELETE "https://api.astronomer.io/v1/organizations/$ORG_ID/deployments/$DEPLOYMENT_ID/hibernation-override" \
  -H "Authorization: Bearer $ASTRO_API_TOKEN"
```

See [Configure a hibernation override](https://www.astronomer.io/docs/astro/api/v-1/deployment/configure-a-hibernation-override-for-a-deployment) and [Delete a hibernation override](https://www.astronomer.io/docs/astro/api/v-1/deployment/delete-a-hibernation-override-for-a-deployment).

#### How Overrides Interact With This Script

- The script never sets or removes overrides.
- When the script updates a Deployment's schedules, any active override is kept.

This means a Deployment can stay hibernated or awake after a bulk update until its override ends or you remove it.

## Recommendations

- Always do a dry run first and read the Organization line at the top of the output.
- Try `--apply` on one or two Deployments before a large batch.
- Keep the config in version control so schedule changes are reviewed.

## Limitations

- **Tested setup:** the script was tested on a Dedicated cluster Deployment (AWS, Astro executor). Standard, Celery, Kubernetes executor, and Remote Execution Deployments haven't been tested. The post-update verification will flag unexpected changes, but test on one Deployment first.
- **Hybrid:** Hybrid Deployments aren't supported.
- **Never Wake schedules:** the UI's **Never Wake** schedule type (`@always` / `@never`) can't be set from the config, because the script only accepts 5-part cron expressions. For indefinite hibernation, use a manual override instead.
- **Targeting:** Deployments are targeted by explicit ID only; there's no workspace-wide or name-pattern selection.
- **Concurrent edits:** Deployments are updated one at a time. If someone edits a Deployment in the UI between the script's read and write, the script's write overwrites that edit.
