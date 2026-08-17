const bridge = window.AstrBotPluginPage;
const list = document.getElementById("profile-list");
const empty = document.getElementById("empty");
const editor = document.getElementById("editor");
const notice = document.getElementById("notice");
const search = document.getElementById("search");
const count = document.getElementById("count");
const avatarPreview = document.getElementById("avatar-preview");
const avatarEmpty = document.getElementById("avatar-empty");
const avatarFile = document.getElementById("avatar-file");
const avatarUpload = document.getElementById("avatar-upload");
const avatarRemove = document.getElementById("avatar-remove");
const MAX_UPLOAD_BYTES = 8 * 1024 * 1024;
let profiles = [];
let avatarCustom = false;

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

function setAvatarPreview(dataUrl, custom) {
  avatarCustom = Boolean(custom);
  if (dataUrl) {
    avatarPreview.src = dataUrl;
    avatarPreview.hidden = false;
    avatarEmpty.hidden = true;
  } else {
    avatarPreview.removeAttribute("src");
    avatarPreview.hidden = true;
    avatarEmpty.hidden = false;
  }
  avatarRemove.hidden = !avatarCustom;
}

function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("读取文件失败"));
    reader.readAsDataURL(file);
  });
}

function validateImageFile(file) {
  if (!file.type.startsWith("image/")) throw new Error("请选择图片文件");
  if (file.size > MAX_UPLOAD_BYTES) throw new Error("图片不能超过 8 MB");
}

async function loadAvatar(qq) {
  if (!qq) { setAvatarPreview(null, false); return; }
  try {
    const result = await bridge.apiGet("admin/profiles/avatar", { qq });
    setAvatarPreview(result.avatar, result.custom);
  } catch (error) {
    setAvatarPreview(null, false);
  }
}

function actionButton(label, action, className = "") {
  const element = document.createElement("button");
  element.type = "button";
  element.textContent = label;
  element.className = className;
  element.addEventListener("click", action);
  return element;
}

function renderProfiles() {
  const keyword = search.value.trim().toLocaleLowerCase();
  const visible = profiles.filter((profile) =>
    [profile.qq, profile.name, profile.title]
      .join(" ")
      .toLocaleLowerCase()
      .includes(keyword),
  );
  list.replaceChildren();
  for (const profile of visible) {
    const row = document.createElement("tr");
    for (const value of [profile.qq, profile.name || "—", profile.title || "—"]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    const colorCell = document.createElement("td");
    const chip = document.createElement("span");
    chip.className = "db-color-chip";
    const dot = document.createElement("span");
    dot.className = "db-color-dot";
    dot.style.background = COLORS[profile.color]?.[1] || COLORS[1][1];
    chip.append(dot, document.createTextNode(COLORS[profile.color]?.[0] || "灰色"));
    colorCell.append(chip);
    row.append(colorCell);
    const actions = document.createElement("td");
    actions.className = "db-row-actions";
    actions.append(
      actionButton("编辑", () => openEditor(profile)),
      actionButton("删除", () => removeProfile(profile), "danger"),
    );
    row.append(actions);
    list.append(row);
  }
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
  loadAvatar(profile?.qq || "");
}

function closeEditor() {
  editor.hidden = true;
  editor.reset();
  setAvatarPreview(null, false);
}

async function loadProfiles() {
  setNotice("正在读取数据库…");
  try {
    const result = await bridge.apiGet("admin/profiles");
    profiles = result.profiles || [];
    renderProfiles();
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
    renderProfiles();
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
    renderProfiles();
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
search.addEventListener("input", renderProfiles);

avatarUpload.addEventListener("click", () => avatarFile.click());

avatarFile.addEventListener("change", async () => {
  const file = avatarFile.files[0];
  if (!file) return;
  const qq = document.getElementById("qq").value.trim();
  if (!qq) { setNotice("请先填写 QQ 号", true); return; }
  let dataUrl;
  try {
    validateImageFile(file);
    dataUrl = await readFileAsDataURL(file);
  } catch (error) {
    setNotice(error.message, true);
    avatarFile.value = "";
    return;
  }
  try {
    await bridge.apiPost("admin/profiles/avatar/upload", { qq, image: dataUrl });
    await loadAvatar(qq);
    setNotice("头像已上传");
  } catch (error) {
    setNotice(error.message, true);
  }
  avatarFile.value = "";
});

avatarRemove.addEventListener("click", async () => {
  const qq = document.getElementById("qq").value.trim();
  if (!qq || !avatarCustom) return;
  if (!window.confirm("确定移除自定义头像吗？移除后将恢复为自动获取的头像。")) return;
  try {
    await bridge.apiPost("admin/profiles/avatar/delete", { qq });
    await loadAvatar(qq);
    setNotice("已移除自定义头像");
  } catch (error) {
    setNotice(error.message, true);
  }
});

await bridge.ready();
await loadProfiles();
