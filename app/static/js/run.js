const { onReady } = window.PolygonUI;

function initExecuteSelectors() {
  const bind = (listId, name, selectId, clearId) => {
    const list = document.getElementById(listId);
    if (!list) return;
    const set = (checked) => list.querySelectorAll(`input[name="${name}"]`).forEach((input) => { input.checked = checked; });
    const select = document.getElementById(selectId);
    const clear = document.getElementById(clearId);
    if (select) select.addEventListener("click", (event) => { event.preventDefault(); set(true); });
    if (clear) clear.addEventListener("click", (event) => { event.preventDefault(); set(false); });
  };
  bind("solution-paths", "solution_paths", "solution-select-all", "solution-select-clear");
  bind("test-names", "test_names", "test-select-all", "test-select-clear");
}

function initRunDetails() {
  const table = document.querySelector(".verification-detail-table");
  if (!table) return;
  const title = document.getElementById("run-test-detail-popup-title");
  const content = document.getElementById("run-test-detail-popup-content");
  const base = String(table.dataset.runDetailsFragment || "").trim();
  const verificationId = String(table.dataset.verificationId || "").trim();
  if (!title || !content || !base || !verificationId) return;
  let pending = null;
  const cancelPending = () => {
    const previous = pending;
    pending = null;
    if (previous) previous.controller.abort();
  };

  const renderTitle = (testName, sourceKind, command) => {
    title.replaceChildren(document.createTextNode(`Test Details: ${testName}`));
    if (sourceKind === "manual") {
      title.appendChild(document.createTextNode(" (manual)"));
    } else if (sourceKind === "generated") {
      title.appendChild(document.createTextNode(" (generated: "));
      const commandText = document.createElement("span");
      commandText.className = "verification-test-title-command";
      commandText.textContent = command || "gen";
      title.append(commandText, document.createTextNode(")"));
    }
  };
  const loading = (message) => {
    content.replaceChildren();
    const text = document.createElement("p");
    text.className = "muted verification-detail-loading";
    text.textContent = message;
    content.appendChild(text);
  };
  const render = (html) => { content.innerHTML = html; };
  const load = async (testName, programId) => {
    if (!testName) {
      cancelPending();
      loading("Run details are unavailable.");
      return;
    }
    const key = JSON.stringify([base, verificationId, testName, programId]);
    if (pending && pending.key === key) return;
    cancelPending();
    const request = { key, controller: new AbortController() };
    pending = request;
    loading("Loading details...");
    const query = new URLSearchParams({ test: testName, verification_id: verificationId });
    if (programId) query.set("program_id", programId);
    try {
      const response = await fetch(`${base}${base.includes("?") ? "&" : "?"}${query}`, {
        signal: request.controller.signal,
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      if (!response.ok) throw new Error("detail fetch failed");
      const html = await response.text();
      if (pending === request) render(html);
    } catch (_error) {
      if (pending === request) loading("Failed to load details.");
    } finally {
      if (pending === request) pending = null;
    }
  };

  document.addEventListener("polygonlike:popup-opened", (event) => {
    const { overlay, opener } = event.detail;
    if (overlay.id !== "run-test-detail-popup" || !opener || !table.contains(opener)) return;
    const row = opener.closest("tr[data-test-name]");
    if (!row) return;
    const testName = String(row.dataset.testName || "").trim();
    renderTitle(testName, String(row.dataset.testSourceKind || ""), String(row.dataset.testCommand || ""));
    load(testName, String(opener.dataset.programId || ""));
  });
  document.addEventListener("polygonlike:popup-closed", (event) => {
    if (event.detail.overlay.id !== "run-test-detail-popup") return;
    cancelPending();
    title.replaceChildren();
    content.replaceChildren();
  });
}

onReady(() => {
  initExecuteSelectors();
  initRunDetails();
});
