const bridge = window.AstrBotPluginPage;
const status = document.getElementById("status");
const controls = document.getElementById("controls");
const stage = document.getElementById("stage");
const presetSelect = document.getElementById("preset-select");
const presetName = document.getElementById("preset-name");
const previewImage = document.getElementById("preview-image");
let defaults;
let config;
let presets = [];
let fonts = [];
let currentPresetId = null;

const SECTIONS = [
  ["canvas", "画布", [["width", "宽度"], ["height", "高度"], ["margin", "边距"], ["background_color", "背景颜色", "color"]]],
  ["avatar", "头像", [["x", "X"], ["y", "Y"], ["width", "宽度"], ["height", "高度"]]],
  ["title", "头衔", [["x", "X"], ["y", "Y"], ["padding_x", "水平内边距"], ["padding_y", "垂直内边距"], ["font_size", "字号"], ["font", "字体", "font"], ["color", "文字颜色", "color"]]],
  ["nickname", "昵称", [["x", "X"], ["y", "Y"], ["font_size", "字号"], ["font", "字体", "font"], ["color", "文字颜色", "color"]]],
  ["bubble", "消息气泡", [["x", "X"], ["y", "Y"], ["padding", "内边距"], ["corner_radius", "圆角"], ["max_width", "最大宽度"], ["font_size", "字号"], ["font", "字体", "font"], ["background_color", "背景颜色", "color"], ["text_color", "文字颜色", "color"]]],
];

function clone(value) { return JSON.parse(JSON.stringify(value)); }
function setStatus(message, error = false) { status.textContent = message; status.classList.toggle("error", error); }

function fieldId(section, field) { return `field-${section}-${field}`; }

function createField(section, [field, label, type]) {
  const wrapper = document.createElement("label");
  if (type === "font" || type === "color") wrapper.className = "wide";
  wrapper.append(document.createTextNode(label));
  let input;
  if (type === "font") {
    input = document.createElement("select");
    input.append(new Option("当前角色默认字体", ""));
    fonts.forEach((font) => input.append(new Option(`${font.label} · ${font.id}`, font.id)));
  } else {
    input = document.createElement("input");
    input.type = type === "color" ? "text" : "number";
    if (type === "color") input.placeholder = "#RRGGBBAA";
  }
  input.id = fieldId(section, field);
  input.value = config[section][field];
  input.addEventListener("input", () => {
    config[section][field] = input.type === "number" ? Number(input.value) : input.value;
    updateStage();
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

function stageScale() { return stage.clientWidth / Math.max(1, config.canvas.width); }

function updateStage() {
  const scale = stageScale();
  stage.style.height = `${config.canvas.height * scale}px`;
  stage.style.background = config.canvas.background_color.slice(0, 7);
  const avatar = stage.querySelector('[data-section="avatar"]');
  Object.assign(avatar.style, { left: `${config.avatar.x * scale}px`, top: `${config.avatar.y * scale}px`, width: `${config.avatar.width * scale}px`, height: `${config.avatar.height * scale}px` });
  const title = stage.querySelector('[data-section="title"]');
  Object.assign(title.style, { left: `${config.title.x * scale}px`, top: `${config.title.y * scale}px`, height: `${Math.max(24, config.title.font_size + config.title.padding_y * 2) * scale}px`, fontSize: `${Math.max(9, config.title.font_size * scale)}px`, color: config.title.color.slice(0, 7) });
  const nickname = stage.querySelector('[data-section="nickname"]');
  Object.assign(nickname.style, { left: `${config.nickname.x * scale}px`, top: `${config.nickname.y * scale}px`, fontSize: `${Math.max(9, config.nickname.font_size * scale)}px`, color: config.nickname.color.slice(0, 7) });
  const bubble = stage.querySelector('[data-section="bubble"]');
  Object.assign(bubble.style, { left: `${config.bubble.x * scale}px`, top: `${config.bubble.y * scale}px`, width: `${Math.min(380, config.bubble.max_width) * scale}px`, borderRadius: `${config.bubble.corner_radius * scale}px`, padding: `${config.bubble.padding * scale}px`, fontSize: `${Math.max(9, config.bubble.font_size * scale)}px`, color: config.bubble.text_color.slice(0, 7), background: config.bubble.background_color.slice(0, 7) });
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
    const origin = { x: event.clientX, y: event.clientY, left: config[section].x, top: config[section].y };
    part.setPointerCapture(event.pointerId);
    part.classList.add("dragging");
    const move = (moveEvent) => {
      const scale = stageScale();
      config[section].x = Math.round(origin.left + (moveEvent.clientX - origin.x) / scale);
      config[section].y = Math.round(origin.top + (moveEvent.clientY - origin.y) / scale);
      syncPositionFields(section);
      updateStage();
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
  requestAnimationFrame(updateStage);
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
  try {
    const result = await bridge.apiPost("admin/layout/preview", {
      config,
      qq: document.getElementById("sample-qq").value,
      display_name: document.getElementById("sample-name").value,
      title: document.getElementById("sample-title").value,
      color: Number(document.getElementById("sample-color").value),
      text: document.getElementById("sample-text").value,
    });
    previewImage.src = result.image;
    previewImage.hidden = false;
    document.querySelector("#render-output span")?.remove();
    setStatus("真实预览已生成");
  } catch (error) { setStatus(error.message, true); }
});

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
