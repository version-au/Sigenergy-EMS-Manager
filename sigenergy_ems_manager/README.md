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
| Home consumption power (sensor, optional) | Used by the discharge ramp to net off house load - see below |

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

## EMS mode

The EMS mode field is a dropdown matching the exact options exposed by the
Sigenergy Local Modbus select entity: PCS Remote Control, Standby, Maximum
Self Consumption, Command Charging (Grid First), Command Charging (PV
First), Command Discharging (PV First), Command Discharging (ESS First),
and V2G. Leave it on "-- don't manage --" for a schedule that shouldn't
touch the EMS mode at all.

## Smooth discharge/charge ramping

Setting a flat discharge power for a whole window and only stopping once
SoC drops to your floor tends to export hard right up until it hits a
cliff. Ramping paces it instead: every poll, the add-on recalculates how
much power is needed to land exactly on your SoC target right as the
schedule ends, and lowers the limit smoothly as that happens — rather than
exporting/importing flat-out and then abruptly stopping.

- **Discharge ramp** — only shown once a schedule's EMS mode is set to
  either "Command Discharging" option. Enable "Ramp export limit" and it
  uses that schedule's existing "Stop discharge at SoC (%)" as the target.
  Each poll it computes a target total discharge rate as `(current SoC −
  target SoC) ÷ 100 × battery capacity ÷ hours remaining in the window`,
  capped at that schedule's configured discharge power - then keeps the
  result within your **Min export limit** / **Max export limit** range
  (also only shown for discharge modes). The discharge power setting
  itself is never touched by this, only the export limit.
- **Charge ramp** — only shown once a schedule's EMS mode is set to
  either "Command Charging" option. Enable "Ramp import limit" and set a
  **Charge target SoC (%)** (also only shown for charge modes) — the
  ceiling to charge up to. Same math in reverse, adjusting only the
  **import limit** and capped at that schedule's configured charge power.
- Both require **Battery capacity (kWh)** to be set in the new **System**
  card, since that's what converts a SoC percentage into an energy amount.
  Without it, ramping is silently skipped and the schedule just uses its
  flat Max export limit / import limit as configured, unchanged.
- **Consumption-aware export ramp** — if you map a **Home consumption
  power** sensor, the discharge ramp becomes smarter about what it
  actually asks the grid for. The battery's total output covers your
  house load first, and only the surplus is exported — so the ramp
  computes a target *total* discharge rate from the SoC/time math (capped
  at your discharge power ceiling), subtracts current consumption from
  that, and only then clamps the result to your Min/Max export limit
  range. A final safety check re-applies the discharge power ceiling
  after that range — so even if your Min export limit is set high,
  (export limit + consumption) can never be pushed past what your
  discharge power setting allows. Leave the sensor unmapped and
  consumption simply isn't subtracted (export limit = target, still kept
  within your Min/Max range).
- If SoC is already past the target when a ramp-enabled window becomes
  active, the relevant **limit** (export or import) is held at 0 rather
  than ramping "backwards" — the configured discharge/charge power is
  never touched by this, on the same schedule or any other. This also
  supersedes the older plain "Stop discharge at SoC (%)" cutoff, which
  used to zero the discharge power directly: with discharge ramp enabled,
  only the export limit is zeroed at cutoff instead.

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

## Notes / current limitations (v0.5)

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

## Fixed in v0.5.1: window id could be saved as the literal text "undefined"

A brand-new schedule (added via "+ Add schedule") had no id yet, and the
frontend accidentally stringified that missing value instead of leaving it
unset, so it got saved as the literal text `"undefined"`. This is now
fixed at the source, and `storage.py` also auto-repairs any window still
carrying that bad id (regenerating a proper one from its name) the next
time the config loads - no manual fix needed if you hit this on an
earlier version.

## Changed in v0.6.0: Export limit is now a Min/Max range

The old single **Export limit (kW)** field is gone, replaced by **Min
export limit (kW)** and **Max export limit (kW)** - both only shown once
a discharge EMS mode is selected. Existing schedules are migrated
automatically: any old `export_limit_kw` value carries over as the new
Max export limit the next time the config loads, so nothing is lost.

- Without ramping enabled, the Max export limit is just applied flat, the
  same as the old single field used to be.
- With ramping enabled, the computed export value is kept within that
  Min/Max range - see "Smooth discharge/charge ramping" above for the
  full formula and how it interacts with consumption and your discharge
  power ceiling.
- **Charge target SoC (%)** is now also only shown once a charge EMS mode
  is selected, instead of always being visible.

## Development

The add-on is a small FastAPI app (`app/main.py`) with a background
scheduler (`app/scheduler.py`) and a plain HTML/JS frontend served over
Ingress (`app/static/`). To iterate locally outside HA, you'd need to stub
out `HAClient` since it depends on the `SUPERVISOR_TOKEN`/Supervisor proxy
that's only present inside a running add-on container.
