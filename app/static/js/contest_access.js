function syncRoleTone(select) {
  select.dataset.roleTone = select.value;
  const control = select.closest(".contest-access-select-control");
  const label = control?.querySelector(".contest-access-select-label");
  const labelValue = label?.querySelector("[data-matrix-role-label]");
  if (!label || !labelValue) return;
  label.dataset.roleTone = select.value;
  labelValue.textContent = select.value;
}

function setBulkRole(selector, role) {
  if (!role) return;
  document.querySelectorAll(selector).forEach((cell) => {
    cell.value = role;
    syncRoleTone(cell);
  });
}

function initRoleTones() {
  document.querySelectorAll("[data-matrix-role='1']").forEach((select) => {
    syncRoleTone(select);
    select.addEventListener("input", () => syncRoleTone(select));
    select.addEventListener("change", () => syncRoleTone(select));
  });
}

function bindBulkSelector(select, selector) {
  const apply = () => {
    const role = select.value;
    if (!role) return;
    setBulkRole(selector(), role);
    select.value = "";
  };
  select.addEventListener("input", apply);
  select.addEventListener("change", apply);
}

function initBulkSelectors() {
  document.querySelectorAll("[data-matrix-all-bulk]").forEach((select) => {
    bindBulkSelector(select, () => "[data-matrix-role='1']");
  });
  document.querySelectorAll("[data-matrix-row-bulk]").forEach((select) => {
    bindBulkSelector(select, () => {
      const problemId = String(select.dataset.matrixRowBulk || "");
      return `[data-matrix-role='1'][data-problem-id='${CSS.escape(problemId)}']`;
    });
  });
  document.querySelectorAll("[data-matrix-column-bulk]").forEach((select) => {
    bindBulkSelector(select, () => {
      const userId = String(select.dataset.matrixColumnBulk || "");
      return `[data-matrix-role='1'][data-user-id='${CSS.escape(userId)}']`;
    });
  });
}

function focusNewAccessTarget() {
  const target = document.querySelector("[data-access-focus-primary='1']");
  if (!target) return;
  target.scrollIntoView({
    behavior: "auto",
    block: "nearest",
    inline: "nearest",
  });
}

initRoleTones();
initBulkSelectors();
focusNewAccessTarget();
