const bridge = window.AstrBotPluginPage;
const status = document.getElementById("status");
const controls = document.getElementById("controls");
const stage = document.getElementById("stage");
const stageImage = document.getElementById("stage-image");
const presetSelect = document.getElementById("preset-select");
const presetName = document.getElementById("preset-name");
const previewImage = document.getElementById("preview-image");
let defaults;
let config;
let presets = [];
let fonts = [];
let currentPresetId = null;
let resolved = null;
let previewTimer = null;
let previewSequence = 0;

const SECTIONS = [
  ["canvas", "画布", [["auto_size", "自动尺寸（原样式）", "boolean"], ["width", "宽度"], ["height", "高度"], ["margin", "边距"], ["background_color", "背景颜色", "color"]]],
  ["avatar", "头像", [["x", "X"], ["y", "Y"], ["width", "宽度"], ["height", "高度"]]],
  ["title", "头衔", [["auto_position", "自动位置（原样式）", "boolean"], ["x", "X"], ["y", "Y"], ["padding_x", "水平内边距"], ["padding_y", "垂直内边距"], ["font_size", "字号"], ["font", "字体", "font"], ["color", "文字颜色", "color"]]],
  ["nickname", "昵称", [["auto_position", "自动位置（原样式）", "boolean"], ["x", "X"], ["y", "Y"], ["font_size", "字号"], ["font", "字体", "font"], ["color", "文字颜色", "color"]]],
  ["bubble", "消息气泡", [["x", "X"], ["y", "Y"], ["padding", "内边距"], ["corner_radius", "圆角"], ["max_width", "最大宽度"], ["font_size", "字号"], ["font", "字体", "font"], ["background_color", "背景颜色", "color"], ["text_color", "文字颜色", "color"]]],
];

function clone(value) { return JSON.parse(JSON.stringify(value)); }
function setStatus(message, error = false) { status.textContent = message; status.classList.toggle("error", error); }

function fieldId(section, field) { return `field-${section}-${field}`; }

function createField(section, [field, label, type]) {
  const wrapper = document.createElement("label");
  if (["font", "color", "boolean"].includes(type)) wrapper.className = "wide";
  wrapper.append(document.createTextNode(label));
  let input;
  if (type === "font") {
    input = document.createElement("select");
    input.append(new Option("当前角色默认字体", ""));
    fonts.forEach((font) => input.append(new Option(`${font.label} · ${font.id}`, font.id)));
  } else if (type === "boolean") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(config[section][field]);
  } else {
    input = document.createElement("input");
    input.type = type === "color" ? "text" : "number";
    if (type === "color") input.placeholder = "#RRGGBBAA";
  }
  input.id = fieldId(section, field);
  if (type !== "boolean") input.value = config[section][field];
  input.addEventListener("input", () => {
    if (section === "canvas" && ["width", "height"].includes(field)) freezeAutoMode("canvas");
    if (["title", "nickname"].includes(section) && ["x", "y"].includes(field)) freezeAutoMode(section);
    config[section][field] = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value;
    schedulePreview();
  });
  wrapper.append(input);
  return wrapper;
}

function renderControls() {
  controls.replaceChildren();
  SECTIONS.forEach(([section, title, fields]) => {
    const block = document.createElement("section");
    block.className = "control-section";
    const heading = document.createElement("h3");
    heading.textContent = title;
    const grid = document.createElement("div");
    grid.className = "control-grid";
    fields.forEach((field) => grid.append(createField(section, field)));
    block.append(heading, grid);
    controls.append(block);
  });
}

function setAutoMode(section, enabled) {
  const field = section === "canvas" ? "auto_size" : "auto_position";
  config[section][field] = enabled;
  const checkbox = document.getElementById(fieldId(section, field));
  if (checkbox) checkbox.checked = enabled;
}

function freezeAutoMode(section) {
  const field = section === "canvas" ? "auto_size" : "auto_position";
  if (!config[section][field] || !resolved?.[section]) return;
  const fields = section === "canvas" ? ["width", "height"] : ["x", "y"];
  for (const name of fields) {
    config[section][name] = resolved[section][name];
    const input = document.getElementById(fieldId(section, name));
    if (input) input.value = resolved[section][name];
  }
  setAutoMode(section, false);
}

function stageScale() { return stage.clientWidth / Math.max(1, resolved?.canvas?.width || config.canvas.width); }

function updateStage() {
  if (!resolved) return;
  const scale = stageScale();
  for (const section of ["avatar", "title", "nickname", "bubble"]) {
    const part = stage.querySelector(`[data-section="${section}"]`);
    const box = resolved[section];
    Object.assign(part.style, {
      left: `${box.x * scale}px`,
      top: `${box.y * scale}px`,
      width: `${Math.max(18, (box.width || 28) * scale)}px`,
      height: `${Math.max(18, (box.height || 28) * scale)}px`,
    });
  }
}

function previewPayload() {
  return {
    config,
    qq: document.getElementById("sample-qq").value,
    display_name: document.getElementById("sample-name").value,
    title: document.getElementById("sample-title").value,
    color: Number(document.getElementById("sample-color").value),
    text: document.getElementById("sample-text").value,
  };
}

function syncResolvedFields() {
  const pairs = config.canvas.auto_size ? [["canvas", "width"], ["canvas", "height"]] : [];
  for (const section of ["title", "nickname"]) {
    if (config[section].auto_position) pairs.push([section, "x"], [section, "y"]);
  }
  for (const [section, field] of pairs) {
    const input = document.getElementById(fieldId(section, field));
    if (input && resolved?.[section]?.[field] !== undefined) input.value = resolved[section][field];
  }
}

async function requestPreview(showResult = false, sequence = ++previewSequence) {
  try {
    const result = await bridge.apiPost("admin/layout/preview", previewPayload());
    if (sequence !== previewSequence) return;
    resolved = result.resolved;
    stageImage.onload = updateStage;
    stageImage.src = result.image;
    syncResolvedFields();
    updateStage();
    if (showResult) {
      previewImage.src = result.image;
      previewImage.hidden = false;
      document.querySelector("#render-output span")?.remove();
      setStatus("真实预览已生成");
    }
  } catch (error) { setStatus(error.message, true); }
}

function schedulePreview(delay = 180) {
  clearTimeout(previewTimer);
  const sequence = ++previewSequence;
  previewTimer = setTimeout(() => requestPreview(false, sequence), delay);
}

function syncPositionFields(section) {
  for (const axis of ["x", "y"]) {
    const input = document.getElementById(fieldId(section, axis));
    if (input) input.value = config[section][axis];
  }
}

stage.querySelectorAll(".part").forEach((part) => {
  part.addEventListener("pointerdown", (event) => {
    const section = part.dataset.section;
    const origin = { x: event.clientX, y: event.clientY, left: resolved?.[section]?.x ?? config[section].x, top: resolved?.[section]?.y ?? config[section].y };
    if (["title", "nickname"].includes(section)) freezeAutoMode(section);
    part.setPointerCapture(event.pointerId);
    part.classList.add("dragging");
    const move = (moveEvent) => {
      const scale = stageScale();
      config[section].x = Math.round(origin.left + (moveEvent.clientX - origin.x) / scale);
      config[section].y = Math.round(origin.top + (moveEvent.clientY - origin.y) / scale);
      if (resolved?.[section]) Object.assign(resolved[section], { x: config[section].x, y: config[section].y });
      syncPositionFields(section);
      updateStage();
      schedulePreview();
    };
    const up = () => {
      part.classList.remove("dragging");
      part.removeEventListener("pointermove", move);
      part.removeEventListener("pointerup", up);
      part.removeEventListener("pointercancel", up);
    };
    part.addEventListener("pointermove", move);
    part.addEventListener("pointerup", up);
    part.addEventListener("pointercancel", up);
  });
});

function renderPresetOptions() {
  presetSelect.replaceChildren(new Option("未保存的新预设", ""));
  presets.forEach((preset) => presetSelect.append(new Option(`${preset.is_active ? "● " : ""}${preset.name}`, preset.id)));
  presetSelect.value = currentPresetId ?? "";
}

function loadPreset(preset) {
  currentPresetId = preset?.id ?? null;
  presetName.value = preset?.name ?? "";
  config = clone(preset?.config ?? defaults);
  renderPresetOptions();
  renderControls();
  resolved = null;
  schedulePreview(0);
}

async function refreshPresets(selectedId = currentPresetId) {
  const result = await bridge.apiGet("admin/layout/presets");
  presets = result.presets || [];
  const selected = presets.find((preset) => preset.id === selectedId);
  if (selected) loadPreset(selected); else renderPresetOptions();
}

presetSelect.addEventListener("change", () => {
  const selected = presets.find((preset) => preset.id === Number(presetSelect.value));
  loadPreset(selected || null);
});

document.getElementById("new-preset").addEventListener("click", () => loadPreset(null));
document.getElementById("save-preset").addEventListener("click", async () => {
  try {
    const result = await bridge.apiPost("admin/layout/presets/save", { id: currentPresetId, name: presetName.value, config });
    await refreshPresets(result.preset.id);
    setStatus("预设已保存到数据库");
  } catch (error) { setStatus(error.message, true); }
});

document.getElementById("activate-preset").addEventListener("click", async () => {
  if (!currentPresetId) return setStatus("请先保存预设", true);
  try { await bridge.apiPost("admin/layout/presets/activate", { id: currentPresetId }); await refreshPresets(currentPresetId); setStatus("已设为实际气泡生成布局"); } catch (error) { setStatus(error.message, true); }
});
document.getElementById("deactivate-preset").addEventListener("click", async () => {
  try { await bridge.apiPost("admin/layout/presets/activate", { id: null }); await refreshPresets(currentPresetId); setStatus("已恢复插件默认布局"); } catch (error) { setStatus(error.message, true); }
});
document.getElementById("reset-preset").addEventListener("click", async () => {
  try {
    if (currentPresetId) { const result = await bridge.apiPost("admin/layout/presets/reset", { id: currentPresetId }); await refreshPresets(result.preset.id); }
    else loadPreset(null);
    setStatus("布局参数已一键重置");
  } catch (error) { setStatus(error.message, true); }
});
document.getElementById("delete-preset").addEventListener("click", async () => {
  if (!currentPresetId || !window.confirm("确定删除这个预设吗？")) return;
  try { await bridge.apiPost("admin/layout/presets/delete", { id: currentPresetId }); currentPresetId = null; await refreshPresets(); loadPreset(null); setStatus("预设已删除"); } catch (error) { setStatus(error.message, true); }
});

document.getElementById("generate-preview").addEventListener("click", async () => {
  setStatus("正在调用真实气泡生成接口…");
  await requestPreview(true);
});

for (const id of ["sample-qq", "sample-name", "sample-title", "sample-color", "sample-text"]) {
  document.getElementById(id).addEventListener("input", () => schedulePreview());
}

window.addEventListener("resize", updateStage);
await bridge.ready();
try {
  const [defaultResult, fontResult, presetResult] = await Promise.all([
    bridge.apiGet("admin/layout/defaults"),
    bridge.apiGet("admin/layout/fonts"),
    bridge.apiGet("admin/layout/presets"),
  ]);
  defaults = defaultResult.layout;
  fonts = fontResult.fonts || [];
  presets = presetResult.presets || [];
  const active = presets.find((preset) => preset.is_active);
  loadPreset(active || null);
  setStatus(active ? `当前使用：${active.name}` : "当前使用插件默认布局");
} catch (error) { setStatus(error.message, true); }
