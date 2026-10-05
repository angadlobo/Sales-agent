// Shared sidebar/topbar behavior for every AgentOS page.
(function () {
  const path = window.location.pathname;
  document.querySelectorAll(".nav-link[data-nav]").forEach((link) => {
    const nav = link.dataset.nav;
    const isActive =
      (nav === "dashboard" && (path === "/" || path === "/dashboard")) ||
      (nav !== "dashboard" && path.startsWith("/" + nav));
    link.classList.toggle("nav-active", isActive);
  });

  const search = document.getElementById("globalSearch");
  if (search) {
    search.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && search.value.trim()) {
        window.location = "/leads?q=" + encodeURIComponent(search.value.trim());
      }
    });
  }

  const pill = document.getElementById("agentStatusPill");
  if (pill) {
    fetch("/api/config")
      .then((r) => r.json())
      .then((c) => {
        const ready = c.anthropic_key_set || c.llm.api_key_set;
        pill.innerHTML = ready
          ? '<span class="w-2 h-2 rounded-full bg-success animate-pulse"></span><span class="font-label-mono text-label-mono font-bold">Agent Active</span>'
          : '<span class="w-2 h-2 rounded-full bg-warning"></span><span class="font-label-mono text-label-mono font-bold">Needs Setup</span>';
        pill.className =
          "flex items-center space-x-2 px-3 py-1.5 rounded-full border " +
          (ready
            ? "bg-success/10 text-success border-success/20"
            : "bg-warning/10 text-warning border-warning/20");
        if (!ready) pill.onclick = () => (window.location = "/settings");
      })
      .catch(() => {});
  }
})();

window.toast = function (msg, type) {
  const box = document.getElementById("toasts");
  if (!box) return;
  const t = document.createElement("div");
  t.className = "toast " + (type || "");
  t.textContent = msg;
  box.appendChild(t);
  setTimeout(() => {
    t.classList.add("out");
    setTimeout(() => t.remove(), 300);
  }, 4000);
};

window.escHtml = function (t) {
  const d = document.createElement("div");
  d.textContent = t == null ? "" : t;
  return d.innerHTML;
};
