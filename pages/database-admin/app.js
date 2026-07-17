const bridge = window.AstrBotPluginPage;
const list = document.getElementById("profile-list");
const empty = document.getElementById("empty");
const editor = document.getElementById("editor");
const notice = document.getElementById("notice");
const search = document.getElementById("search");
const count = document.getElementById("count");
let profiles = [];

const COLORS = {
  1: ["灰色", "#B5B6B5"],
  2: ["紫色", "#D69AFF"],
  3: ["黄色", "#FFC629"],
  4: ["绿色", "#52D7C5"],
};

function setNotice(message, isError = false) {
  notice.textContent = message;
  notice.classList.toggle("error", isError);
}

function button(label, action, className = "") {
  const element = document.createElement("button");
  element.type = "button";
  element.textContent = label;
  element.className = className;
  element.addEventListener("click", action);
  return element;
}

function render() {
  const keyword = search.value.trim().toLocaleLowerCase();
  const visible = profiles.filter((profile) =>
    [profile.qq, profile.name, profile.title]
      .join(" ")
      .toLocaleLowerCase()
      .includes(keyword),
  );
  list.replaceChildren();
  visible.forEach((profile) => {
    const row = document.createElement("tr");
    [profile.qq, profile.name || "—", profile.title || "—"].forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    });
    const colorCell = document.createElement("td");
    const chip = document.createElement("span");
    chip.className = "color-chip";
    const dot = document.createElement("span");
    dot.className = "color-dot";
    dot.style.background = COLORS[profile.color]?.[1] || COLORS[1][1];
    chip.append(dot, document.createTextNode(COLORS[profile.color]?.[0] || "灰色"));
    colorCell.append(chip);
    row.append(colorCell);
    const actions = document.createElement("td");
    actions.className = "row-actions";
    actions.append(
      button("编辑", () => openEditor(profile)),
      button("删除", () => removeProfile(profile), "danger"),
    );
    row.append(actions);
    list.append(row);
  });
  empty.hidden = visible.length > 0;
  count.textContent = `${visible.length} / ${profiles.length} 条`;
}

function openEditor(profile = null) {
  editor.hidden = false;
  document.getElementById("editor-title").textContent = profile ? "编辑用户" : "新增用户";
  document.getElementById("qq").value = profile?.qq || "";
  document.getElementById("qq").readOnly = Boolean(profile);
  document.getElementById("name").value = profile?.stored_name || "";
  document.getElementById("title").value = profile?.title || "";
  document.getElementById("color").value = String(profile?.color || 1);
  document.getElementById("qq").focus();
}

function closeEditor() {
  editor.hidden = true;
  editor.reset();
}

async function loadProfiles() {
  setNotice("正在读取数据库…");
  try {
    const result = await bridge.apiGet("admin/profiles");
    profiles = result.profiles || [];
    render();
    setNotice("数据库已同步");
  } catch (error) {
    setNotice(error.message, true);
  }
}

async function removeProfile(profile) {
  if (!window.confirm(`确定删除 QQ ${profile.qq} 的设置吗？`)) return;
  try {
    await bridge.apiPost("admin/profiles/delete", { qq: profile.qq });
    profiles = profiles.filter((item) => item.qq !== profile.qq);
    render();
    setNotice(`已删除 ${profile.qq}`);
  } catch (error) {
    setNotice(error.message, true);
  }
}

editor.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {
    qq: document.getElementById("qq").value.trim(),
    name: document.getElementById("name").value.trim(),
    title: document.getElementById("title").value.trim(),
    color: Number(document.getElementById("color").value),
  };
  try {
    const result = await bridge.apiPost("admin/profiles/save", payload);
    const index = profiles.findIndex((item) => item.qq === result.profile.qq);
    if (index >= 0) profiles[index] = result.profile;
    else profiles.push(result.profile);
    profiles.sort((left, right) => left.qq.localeCompare(right.qq));
    render();
    closeEditor();
    setNotice(`已保存 ${result.profile.qq}`);
  } catch (error) {
    setNotice(error.message, true);
  }
});

document.getElementById("new-profile").addEventListener("click", () => openEditor());
document.getElementById("refresh").addEventListener("click", loadProfiles);
document.getElementById("close-editor").addEventListener("click", closeEditor);
document.getElementById("cancel").addEventListener("click", closeEditor);
search.addEventListener("input", render);

await bridge.ready();
await loadProfiles();
