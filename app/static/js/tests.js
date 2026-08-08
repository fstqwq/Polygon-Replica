const { onReady } = window.PolygonUI;

function initSampleForms() {
  const sync = (form) => {
    const toggle = form.querySelector("[data-sample-toggle='1']");
    const group = form.querySelector("[data-sample-output-validate-group='1']");
    if (!toggle || !group) return;
    group.hidden = !toggle.checked;
    const checkbox = group.querySelector("input[name='sample_output_validate']");
    if (checkbox) checkbox.disabled = !toggle.checked;
  };
  document.querySelectorAll("form[data-sample-form='1']").forEach((form) => {
    sync(form);
    form.addEventListener("change", (event) => {
      if (event.target && event.target.dataset.sampleToggle === "1") sync(form);
    });
  });
}

function initStructuredFocus() {
  const focus = String(new URLSearchParams(window.location.search).get("focus") || "").trim();
  if (!/^\d+$/.test(focus)) return;
  const row = document.getElementById(`test-row-${focus}`);
  if (!row) return;
  row.classList.add("tests-editor-item-new-focus");
  row.tabIndex = -1;
  row.focus({ preventScroll: true });
  row.scrollIntoView({ block: "center", behavior: "smooth" });
  const query = new URLSearchParams(window.location.search);
  query.delete("focus");
  const suffix = query.toString();
  window.history.replaceState(null, "", `${window.location.pathname}${suffix ? `?${suffix}` : ""}${window.location.hash}`);
}

onReady(() => {
  initSampleForms();
  initStructuredFocus();
});
