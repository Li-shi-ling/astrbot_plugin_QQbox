const navigation = [...document.querySelectorAll("[data-view]")];
const panels = [...document.querySelectorAll("[data-view-panel]")];

function selectView(name, updateHash = true) {
  const selected = panels.some((panel) => panel.dataset.viewPanel === name) ? name : "bubble";
  for (const panel of panels) panel.hidden = panel.dataset.viewPanel !== selected;
  for (const item of navigation) {
    const active = item.dataset.view === selected;
    item.classList.toggle("active", active);
    item.setAttribute("aria-selected", String(active));
  }
  if (updateHash && window.location.hash !== `#${selected}`) {
    window.history.replaceState(null, "", `#${selected}`);
  }
  document.title = selected === "database" ? "QQbox · 数据库管理" : "QQbox · 气泡调节";
  window.scrollTo({ top: 0, behavior: "auto" });
}

for (const item of navigation) {
  item.addEventListener("click", () => selectView(item.dataset.view));
}
window.addEventListener("hashchange", () => selectView(window.location.hash.slice(1), false));
selectView(window.location.hash.slice(1) || "bubble", false);
