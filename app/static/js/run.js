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
  const cache = new Map();
  let activeKey = "";

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
  const render = (html, runId) => {
    if (!runId) {
      content.innerHTML = html;
      return;
    }
    const wrapper = document.createElement("div");
    wrapper.innerHTML = html;
    const cards = Array.from(wrapper.querySelectorAll(".sol-card[data-run-id]"));
    const selected = cards.find((card) => card.dataset.runId === runId) || cards[0];
    cards.forEach((card) => { if (card !== selected) card.remove(); });
    content.innerHTML = wrapper.innerHTML;
  };
  const load = async (testName, runId) => {
    const key = testName;
    activeKey = key;
    if (cache.has(key)) {
      render(cache.get(key), runId);
      return;
    }
    loading("Loading details...");
    const query = new URLSearchParams({ test: testName, verification_id: verificationId });
    try {
      const response = await fetch(`${base}${base.includes("?") ? "&" : "?"}${query}`, {
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      if (!response.ok) throw new Error("detail fetch failed");
      const html = await response.text();
      cache.set(key, html);
      if (activeKey === key) render(html, runId);
    } catch (_error) {
      if (activeKey === key) loading("Failed to load details.");
    }
  };

  table.querySelectorAll('[data-popup-open="run-test-detail-popup"][data-test-name]').forEach((opener) => {
    opener.addEventListener("click", () => {
      const testName = String(opener.dataset.testName || "").trim();
      renderTitle(testName, String(opener.dataset.testSourceKind || ""), String(opener.dataset.testCommand || ""));
      load(testName, String(opener.dataset.runId || ""));
    });
  });
}

onReady(() => {
  initExecuteSelectors();
  initRunDetails();
});
