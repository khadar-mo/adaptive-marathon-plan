/* Exact decimal inputs stay as strings between the form and SQLite. */
const form = document.querySelector("#template-editor");
const initial = JSON.parse(document.querySelector("#template-values").textContent);
const list = document.querySelector("#ordered-blocks");
const saved = new WeakMap();
let requestId = crypto.randomUUID();

function control(root, name) {
  return root.querySelector('[name="' + name + '"]');
}
function assign(root, name, v) {
  const el = control(root, name);
  el.value = v ?? "";
  saved.set(el, {display: el.value, value: v});
}
function value(root, name) {
  const el = control(root, name);
  const original = saved.get(el);
  return original && original.display === el.value ? original.value : el.value;
}
function optional(root, name) {
  return value(root, name) || null;
}
function intValue(root, name) {
  const v = control(root, name).value;
  return v === "" ? null : Number(v);
}
function refresh() {
  const blocks = [...list.children];
  blocks.forEach((b, index) => {
    b.querySelector("legend").textContent = "Block " + (index + 1);
    b.querySelector(".up").disabled = index === 0;
    b.querySelector(".down").disabled = index === blocks.length - 1;
    b.querySelector(".remove").disabled = blocks.length === 1;
  });
}
const paceKeys = ["lower", "upper", "unit", "method", "source_ref", "calibration", "evidence_known_at", "uncertainty", "status"];
const zoneKeys = ["method", "band", "calibration"];
function evidence(b, name, keys, original) {
  const enabled = control(b, "use_" + name);
  enabled.checked = !!original;
  for (const key of keys) {
    const fallback = {unit: "s/km", status: "UNVERIFIED"}[key] ?? "";
    assign(b, name + "_" + key, original?.[key] ?? fallback);
  }
  function availability() {
    const section = b.querySelector("." + name + "-evidence");
    section.hidden = !enabled.checked;
    section.querySelectorAll("input, select, textarea").forEach(el => { el.disabled = !enabled.checked; });
  }
  availability();
  enabled.addEventListener("change", availability);
}
function addBlock(original = {}) {
  const b = document.querySelector("#block-editor").content.firstElementChild.cloneNode(true);
  b.dataset.id = original.block_id || crypto.randomUUID();
  for (const [name, fallback] of Object.entries({
    kind: "EASY_VOLUME", distance_m: "", min_m: "", max_m: "",
    intensity: "", intensity_method: "", rest_after_s: "0"
  })) assign(b, name, original[name] ?? fallback);
  control(b, "protected").checked = !!original.protected;
  control(b, "allow_split").checked = !!original.allow_split;
  evidence(b, "pace", paceKeys, original.pace);
  evidence(b, "zone", zoneKeys, original.zone);
  b.querySelector(".up").addEventListener("click", () => { b.previousElementSibling.before(b); changed(); refresh(); control(b, "kind").focus(); });
  b.querySelector(".down").addEventListener("click", () => { b.nextElementSibling.after(b); changed(); refresh(); control(b, "kind").focus(); });
  b.querySelector(".remove").addEventListener("click", () => { b.remove(); changed(); refresh(); document.querySelector("#add-block").focus(); });
  list.append(b);
  refresh();
  return b;
}
function readEvidence(block, name, keys) {
  if (!control(block, "use_" + name).checked) return null;
  return Object.fromEntries(keys.map(k => [k, k === "calibration" ? optional(block, name + "_" + k) : value(block, name + "_" + k)]));
}
function readBlock(b) {
  return {
    block_id: b.dataset.id, kind: value(b, "kind"), distance_m: intValue(b, "distance_m"),
    min_m: intValue(b, "min_m"), max_m: intValue(b, "max_m"), intensity: value(b, "intensity"),
    intensity_method: value(b, "intensity_method"), protected: control(b, "protected").checked,
    allow_split: control(b, "allow_split").checked, rest_after_s: value(b, "rest_after_s") || "0",
    pace: readEvidence(b, "pace", paceKeys), zone: readEvidence(b, "zone", zoneKeys)
  };
}
function changed() { requestId = crypto.randomUUID(); }
assign(form, "name", initial.name || "");
assign(form, "primary", initial.primary || "EASY_AEROBIC");
assign(form, "role", initial.role || "EASY");
assign(form, "tags", JSON.stringify(initial.tags || []));
(initial.blocks || [{}]).forEach(addBlock);
document.querySelector("#add-block").addEventListener("click", () => { changed(); control(addBlock(), "kind").focus(); });
form.addEventListener("input", changed);
form.addEventListener("change", changed);
form.addEventListener("submit", async event => {
  event.preventDefault();
  const status = form.querySelector('[role="status"]');
  const button = form.querySelector('[type="submit"]');
  button.disabled = true;
  try {
    const doc = {
      request_id: requestId, expected_revision: initial.version_id || null,
      actor: control(form, "actor").value, reason: control(form, "reason").value,
      name: value(form, "name"), primary: value(form, "primary"), role: value(form, "role"),
      tags: JSON.parse(value(form, "tags")), blocks: [...list.children].map(readBlock)
    };
    const resp = await fetch(form.action, {method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(doc)});
    const result = await resp.json();
    if (!resp.ok) throw new Error(result.detail || "Could not save this template.");
    location.assign(result.html_url);
  } catch (err) { status.textContent = err.message; button.disabled = false; }
});
