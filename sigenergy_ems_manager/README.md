# Sigenergy EMS Manager (Home Assistant Add-on)

Schedule-driven control for a Sigenergy battery/inverter system via the
**Sigenergy-Local-Modbus** HACS integration. Runs as a Docker add-on inside
Home Assistant OS, shows up in the sidebar (via Ingress), and continuously
enforces your configured EMS mode, discharge power, and grid import/export
limits during set time windows — re-applying them if anything else changes
those values while a window is active.

## What it does

- Lets you define one or more **schedule windows** (days + start/end time),
  each specifying: EMS work mode, charge power, discharge power, an SoC
  cutoff that forces discharge to 0 once the battery drops to it, grid
  import/export limits, and an optional notification.
- Polls Home Assistant every `poll_interval_seconds` (default 20s). For any
  active window, it compares the live entity states to the desired values
  and re-applies anything that's drifted — whether from a manual change,
  another automation, or the inverter resetting on its own.
- Outside all windows, it does nothing — your existing manual control (or
  the inverter's own defaults) is left alone.
- Config (entity mapping + schedules) is stored in the add-on's persistent
  `/data` volume, editable any time from the sidebar UI, and survives
  add-on updates.

## Installing

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**,
   and add the URL of the GitHub repo you push this folder structure to
   (the repo root needs `repository.json` and the `sigenergy_ems_manager/`
   folder as siblings — that's already how this zip is laid out). If
   you're running it as a local add-on instead, drop the
   `sigenergy_ems_manager/` folder straight into `/addons/local/`.
2. Refresh the store, find **Sigenergy EMS Manager**, install it.
3. Start it, then open it from the sidebar (look for the battery icon).
4. On first load, fill in the **Entities** section with your actual
   Sigenergy Local Modbus entity IDs — the fields autocomplete against your
   live Home Assistant entities as you type.
5. Add your schedule windows (e.g. your free-power period, your high-PV
   discharge-rate period) and save.

No Home Assistant automations are required for this logic once it's set up
— you can disable/remove the ones you were using for this specific purpose.

## Entities you'll need to map

| Field | What it is |
|---|---|
| EMS mode (select) | The remote EMS work-mode select entity (e.g. Standby / Max self-consumption / Command charging / Command discharging) |
| Remote EMS enable (switch) | Optional — some setups gate remote control behind a switch entity |
| Charge power limit (number) | Sets charge power when in a charging EMS mode |
| Discharge power limit (number) | Sets discharge power when in a discharging EMS mode |
| Battery SoC (sensor) | Used for the discharge cutoff |
| Grid import limit (number) | If your inverter/firmware exposes this via Modbus |
| Grid export limit (number) | If your inverter/firmware exposes this via Modbus |

If your integration doesn't expose import/export limit registers locally,
leave those fields blank — the rest of the app still works.

## Units

Power fields in the UI (charge/discharge power, import/export limits) are
entered in **kW**. By default these are converted to Watts when written to
your Sigenergy number entities, since that's the typical native unit for
the Local Modbus integration's power-limit entities. If your entities are
already natively in kW, set the add-on option `power_entities_unit` to
`kW` (Settings → Add-ons → Sigenergy EMS Manager → Configuration) so no
conversion happens.

## Reordering schedules

Use the ▲/▼ buttons on each schedule card to move it up or down, then hit
**Save schedules**. Order matters when two schedules overlap in time:
whichever one is *lower* in the list wins on any field they both set (EMS
mode, charge/discharge power, limits). Non-overlapping schedules are
unaffected by order.

## Per-schedule notifications

Each schedule has an optional **Notify when this schedule becomes active**
toggle. When enabled, the add-on calls a Home Assistant service the moment
that schedule transitions from inactive to active (not repeatedly while it
stays active, and not again until it goes inactive and re-activates).

- **Notify service** — any HA service in `domain.service` form, e.g.
  `persistent_notification.create` (shows up in HA's own notifications, no
  extra setup) or `notify.mobile_app_<yourphone>` (push notification via
  the HA mobile app).
- **Title** — optional, passed through as the service's `title` field.
- **Message** — optional; defaults to "`<schedule name>` schedule is now
  active" if left blank.

Notification attempts (success or failure) show up in the Status panel
alongside the other actions for that poll.

## Build system note

The Dockerfile uses an explicit `FROM python:3.12-alpine` rather than the
old `ARG BUILD_FROM` / `build.yaml` pattern. Supervisor 2026.04.0 dropped
that build system in favour of plain Docker BuildKit, and a `build.yaml`
in the repo is simply ignored now — the Dockerfile is the single source of
truth for the base image.

## If the sidebar UI doesn't show the latest features after an update

The add-on now sends `Cache-Control: no-cache, no-store, must-revalidate`
on every UI asset (index.html/app.js/style.css) specifically because
browsers were holding onto old copies of these across updates, inside the
ingress iframe, even after the add-on itself had rebuilt to a new version.
If you're still seeing old behaviour after updating: hard-refresh
(Ctrl+Shift+R / Cmd+Shift+R), or check in a private/incognito window to
rule out cache entirely.

## Troubleshooting: values not being applied

If the UI loads and saves fine but scheduled values never actually land on
your Sigenergy entities, check the add-on log for:

```
ERROR sigen_ems.ha_client: SUPERVISOR_TOKEN is empty - calls to Home Assistant will fail.
```

This means the add-on couldn't authenticate to the Home Assistant Core API.
Make sure `homeassistant_api: true` is present in `config.yaml` (it is by
default in this repo). This add-on intentionally builds on a plain
`python:3.12-alpine` base rather than an s6-overlay-based add-on base
image — s6's "legacy service" wrapping was found to not reliably pass the
container's environment (including `SUPERVISOR_TOKEN`) through to a plain
Dockerfile `CMD`, which silently broke every write.

## Notes / current limitations (v0.3)

- Windows are matched by local wall-clock time; if the free-power period or
  PV-rate period shifts daily (e.g. published by your retailer), you'll
  need to update the schedule manually for now — a future version could
  pull those from an `input_datetime` helper or a retailer API instead.
- Overlapping windows: the lower window in the list wins on any field it
  sets. Each schedule's notification is independent — both would fire if
  both toggle on and both become active at once.
- Notification "already fired" state is tracked in memory, not persisted —
  a restart mid-window could cause one re-notification if the window is
  still active when the add-on comes back up.
- No historical logging yet beyond the last poll's status, shown in the
  Status panel.

## Development

The add-on is a small FastAPI app (`app/main.py`) with a background
scheduler (`app/scheduler.py`) and a plain HTML/JS frontend served over
Ingress (`app/static/`). To iterate locally outside HA, you'd need to stub
out `HAClient` since it depends on the `SUPERVISOR_TOKEN`/Supervisor proxy
that's only present inside a running add-on container.
