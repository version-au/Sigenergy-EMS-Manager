const ENTITY_FIELDS = [
  ["ems_mode_select", "EMS mode (select entity)"],
  ["ems_mode_enable_switch", "Remote EMS enable (switch, optional)"],
  ["charge_limit_number", "Charge power limit (number entity)"],
  ["discharge_limit_number", "Discharge power limit (number entity)"],
  ["soc_sensor", "Battery SoC (sensor entity)"],
  ["import_limit_number", "Grid import limit (number entity)"],
  ["export_limit_number", "Grid export limit (number entity)"],
];

let entityDatalistBuilt = false;

async function fetchJSON(url, options) {
  const resp = await fetch(url, options);
  if (!resp.ok) throw new Error(`${url} -> ${resp.status}`);
  return resp.json();
}

async function ensureEntityDatalist() {
  if (entityDatalistBuilt) return;
  entityDatalistBuilt = true;
  try {
    const matches = await fetchJSON("api/ha/entities?q=");
    const dl = document.createElement("datalist");
    dl.id = "entity-options";
    matches.forEach((m) => {
      const opt = document.createElement("option");
      opt.value = m.entity_id;
      dl.appendChild(opt);
    });
    document.body.appendChild(dl);
    document.querySelectorAll(".entity-grid input").forEach((inp) => {
      inp.setAttribute("list", "entity-options");
    });
  } catch (e) {
    console.warn("Could not load entity list for autocomplete", e);
  }
}

function renderEntityGrid(entities) {
  const grid = document.getElementById("entity-grid");
  grid.innerHTML = "";
  ENTITY_FIELDS.forEach(([key, label]) => {
    const wrapper = document.createElement("label");
    wrapper.textContent = label;
    const input = document.createElement("input");
    input.type = "text";
    input.dataset.key = key;
    input.value = entities[key] || "";
    input.placeholder = "domain.entity_id";
    wrapper.appendChild(input);
    grid.appendChild(wrapper);
  });
  ensureEntityDatalist();
}

function collectEntities() {
  const out = {};
  document.querySelectorAll("#entity-grid input").forEach((inp) => {
    out[inp.dataset.key] = inp.value.trim();
  });
  return out;
}

function renderWindow(win) {
  const tpl = document.getElementById("window-template");
  const node = tpl.content.firstElementChild.cloneNode(true);
  node.dataset.id = win.id;

  node.querySelector(".w-name").value = win.name || "";
  node.querySelector(".w-enabled").checked = win.enabled !== false;
  node.querySelector(".w-start").value = win.start || "";
  node.querySelector(".w-end").value = win.end || "";
  node.querySelector(".w-ems-mode").value = win.ems_mode || "";
  node.querySelector(".w-charge").value = win.charge_power_kw ?? "";
  node.querySelector(".w-discharge").value = win.discharge_power_kw ?? "";
  node.querySelector(".w-soc-stop").value = win.soc_stop_percent ?? "";
  node.querySelector(".w-import").value = win.import_limit_kw ?? "";
  node.querySelector(".w-export").value = win.export_limit_kw ?? "";
  node.querySelector(".w-notify-enabled").checked = !!win.notify_enabled;
  node.querySelector(".w-notify-service").value = win.notify_service || "";
  node.querySelector(".w-notify-title").value = win.notify_title || "";
  node.querySelector(".w-notify-message").value = win.notify_message || "";

  const days = win.days || ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  node.querySelectorAll("[data-days] input").forEach((cb) => {
    cb.checked = days.includes(cb.value);
  });

  node.querySelector(".remove-window").addEventListener("click", () => {
    node.remove();
  });

  node.querySelector(".move-up").addEventListener("click", () => {
    const prev = node.previousElementSibling;
    if (prev) node.parentElement.insertBefore(node, prev);
  });

  node.querySelector(".move-down").addEventListener("click", () => {
    const next = node.nextElementSibling;
    if (next) node.parentElement.insertBefore(next, node);
  });

  return node;
}

function renderWindows(windows) {
  const list = document.getElementById("windows-list");
  list.innerHTML = "";
  windows.forEach((w) => list.appendChild(renderWindow(w)));
}

function windowIdFromName(name, existingIds) {
  let base = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "window";
  let id = base;
  let n = 1;
  while (existingIds.has(id)) {
    id = `${base}-${n++}`;
  }
  existingIds.add(id);
  return id;
}

function collectWindows() {
  const existingIds = new Set();
  return [...document.querySelectorAll("#windows-list [data-window]")].map((node) => {
    const name = node.querySelector(".w-name").value.trim() || "Untitled";
    const id = node.dataset.id || windowIdFromName(name, existingIds);
    existingIds.add(id);

    const num = (sel) => {
      const v = node.querySelector(sel).value;
      return v === "" ? null : Number(v);
    };

    return {
      id,
      name,
      enabled: node.querySelector(".w-enabled").checked,
      days: [...node.querySelectorAll("[data-days] input:checked")].map((cb) => cb.value),
      start: node.querySelector(".w-start").value || "00:00",
      end: node.querySelector(".w-end").value || "00:00",
      ems_mode: node.querySelector(".w-ems-mode").value.trim() || null,
      charge_power_kw: num(".w-charge"),
      discharge_power_kw: num(".w-discharge"),
      soc_stop_percent: num(".w-soc-stop"),
      import_limit_kw: num(".w-import"),
      export_limit_kw: num(".w-export"),
      notify_enabled: node.querySelector(".w-notify-enabled").checked,
      notify_service: node.querySelector(".w-notify-service").value.trim() || null,
      notify_title: node.querySelector(".w-notify-title").value.trim() || null,
      notify_message: node.querySelector(".w-notify-message").value.trim() || null,
    };
  });
}

function flashSaved(id) {
  const el = document.getElementById(id);
  el.textContent = "Saved";
  setTimeout(() => (el.textContent = ""), 2000);
}

async function loadConfig() {
  const cfg = await fetchJSON("api/config");
  renderEntityGrid(cfg.entities);
  renderWindows(cfg.windows || []);
}

async function saveEntities() {
  await fetchJSON("api/entities", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(collectEntities()),
  });
  flashSaved("entities-save-msg");
}

async function saveWindows() {
  await fetchJSON("api/windows", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ windows: collectWindows() }),
  });
  flashSaved("windows-save-msg");
}

async function refreshStatus() {
  try {
    const status = await fetchJSON("api/status");
    const pill = document.getElementById("status-pill");
    const body = document.getElementById("status-body");
    if (status.last_run) {
      const names = status.active_window_names || [];
      pill.textContent = names.length ? names.join(", ") : "idle";
      pill.style.color = names.length ? "var(--good)" : "var(--muted)";
    } else {
      pill.textContent = "waiting for first run";
    }
    body.textContent = JSON.stringify(status, null, 2);
  } catch (e) {
    document.getElementById("status-pill").textContent = "unreachable";
  }
}

document.getElementById("save-entities").addEventListener("click", saveEntities);
document.getElementById("save-windows").addEventListener("click", saveWindows);
document.getElementById("add-window").addEventListener("click", () => {
  document.getElementById("windows-list").appendChild(
    renderWindow({ name: "", start: "00:00", end: "00:00", enabled: true })
  );
});

loadConfig();
refreshStatus();
setInterval(refreshStatus, 10000);
