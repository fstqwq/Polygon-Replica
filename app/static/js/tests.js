const { onReady } = window.PolygonUI;

function initSampleForms() {
  const utf8 = new TextEncoder();
  const unknownField = (value, allowed) => Object.keys(value).find((key) => !allowed.includes(key));
  const validateJson = (textarea) => {
    const raw = textarea.value.trim();
    if (!raw) return "Enter a JSON object.";
    let value;
    try {
      value = JSON.parse(raw);
    } catch (error) {
      return `Invalid JSON: ${error.message}`;
    }
    if (!value || Array.isArray(value) || typeof value !== "object") return "The value must be an object.";
    const rootUnknown = unknownField(value, ["presentation", "passes"]);
    if (rootUnknown) return `Unknown field: ${rootUnknown}.`;
    if (value.presentation !== "pair" && value.presentation !== "interaction") return "presentation must be pair or interaction.";
    if (!Array.isArray(value.passes) || !value.passes.length) return "passes must be a non-empty array.";
    let contentBytes = 0;
    for (let index = 0; index < value.passes.length; index += 1) {
      const pass = value.passes[index];
      if (!pass || Array.isArray(pass) || typeof pass !== "object") return `Pass ${index + 1} must be an object.`;
      const passAllowed = value.presentation === "pair" ? ["number", "input", "output"] : ["number", "events"];
      const passUnknown = unknownField(pass, passAllowed);
      if (passUnknown) return `Pass ${index + 1} has unknown field: ${passUnknown}.`;
      if ((pass.number ?? index + 1) !== index + 1) return `Pass ${index + 1} must have number ${index + 1}.`;
      if (value.presentation === "pair") {
        if (typeof pass.input !== "string" || typeof pass.output !== "string") return `Pass ${index + 1} requires string input and output.`;
        contentBytes += utf8.encode(pass.input).length + utf8.encode(pass.output).length;
      } else {
        if (!Array.isArray(pass.events)) return `Pass ${index + 1} requires an events array.`;
        for (const event of pass.events) {
          if (!event || (event.source !== "interactor" && event.source !== "solution") || typeof event.content !== "string") {
            return `Pass ${index + 1} events require source and string content.`;
          }
          const eventUnknown = unknownField(event, ["source", "content"]);
          if (eventUnknown) return `Pass ${index + 1} event has unknown field: ${eventUnknown}.`;
          contentBytes += utf8.encode(event.content).length;
        }
      }
    }
    const maxBytes = Number.parseInt(textarea.dataset.maxBytes || "0", 10);
    if (maxBytes > 0 && contentBytes > maxBytes) return `Sample content exceeds ${maxBytes} UTF-8 bytes.`;
    return "";
  };

  const sync = (form) => {
    const toggle = form.querySelector("[data-sample-toggle='1']");
    const group = form.querySelector("[data-sample-output-validate-group='1']");
    const format = form.querySelector("[data-sample-format='1']");
    const legacy = form.querySelector("[data-sample-legacy='1']");
    const jsonGroup = form.querySelector("[data-sample-json-group='1']");
    const textarea = form.querySelector("[data-sample-json='1']");
    if (!toggle || !group || !format || !legacy || !jsonGroup || !textarea) return;
    const structured = toggle.checked && format.value === "json";
    legacy.hidden = structured;
    jsonGroup.hidden = !structured;
    legacy.querySelectorAll("input, textarea, select").forEach((control) => { control.disabled = structured || !toggle.checked; });
    jsonGroup.querySelectorAll("input, textarea, select").forEach((control) => { control.disabled = !structured; });
    group.hidden = !toggle.checked || structured;
    const error = structured ? validateJson(textarea) : "";
    const status = form.querySelector("[data-sample-json-status='1']");
    if (status) {
      status.textContent = error || (structured ? "Valid structured sample." : "");
      status.classList.toggle("tone-fail", Boolean(error));
      status.classList.toggle("ok", structured && !error);
    }
    textarea.setCustomValidity(error);
  };
  document.querySelectorAll("form[data-sample-form='1']").forEach((form) => {
    sync(form);
    form.addEventListener("change", (event) => {
      if (event.target) sync(form);
    });
    form.addEventListener("input", () => sync(form));
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
