const { onReady } = window.PolygonUI;

function initSampleForms() {
  const utf8 = new TextEncoder();
  const examples = {
    multipass: {
      presentation: "pair",
      passes: [
        { number: 1, input: "3 1\n", output: "4\n" },
        { number: 2, input: "3 2\n", output: "5\n" },
      ],
    },
    interactive: {
      presentation: "interaction",
      passes: [{
        number: 1,
        events: [
          { source: "interactor", content: "3\n" },
          { source: "solution", content: "2\n" },
          { source: "interactor", content: "correct\n" },
        ],
      }],
    },
    "multipass-interactive": {
      presentation: "interaction",
      passes: [
        {
          number: 1,
          events: [
            { source: "interactor", content: "first round\n" },
            { source: "solution", content: "answer one\n" },
          ],
        },
        {
          number: 2,
          events: [
            { source: "interactor", content: "second round\n" },
            { source: "solution", content: "answer two\n" },
          ],
        },
      ],
    },
  };
  const unknownField = (value, allowed) => Object.keys(value).find((key) => !allowed.includes(key));
  const validateJson = (textarea) => {
    const raw = textarea.value.trim();
    if (!raw) return { error: "Enter a JSON object.", value: null };
    let value;
    try {
      value = JSON.parse(raw);
    } catch (error) {
      return { error: `Invalid JSON: ${error.message}`, value: null };
    }
    const fail = (error) => ({ error, value: null });
    if (!value || Array.isArray(value) || typeof value !== "object") return fail("The value must be an object.");
    const rootUnknown = unknownField(value, ["presentation", "passes"]);
    if (rootUnknown) return fail(`Unknown field: ${rootUnknown}.`);
    if (value.presentation !== "pair" && value.presentation !== "interaction") return fail("presentation must be pair or interaction.");
    if (!Array.isArray(value.passes) || !value.passes.length) return fail("passes must be a non-empty array.");
    let contentBytes = 0;
    for (let index = 0; index < value.passes.length; index += 1) {
      const pass = value.passes[index];
      if (!pass || Array.isArray(pass) || typeof pass !== "object") return fail(`Pass ${index + 1} must be an object.`);
      const passAllowed = value.presentation === "pair" ? ["number", "input", "output"] : ["number", "events"];
      const passUnknown = unknownField(pass, passAllowed);
      if (passUnknown) return fail(`Pass ${index + 1} has unknown field: ${passUnknown}.`);
      if ((pass.number ?? index + 1) !== index + 1) return fail(`Pass ${index + 1} must have number ${index + 1}.`);
      if (value.presentation === "pair") {
        if (typeof pass.input !== "string" || typeof pass.output !== "string") return fail(`Pass ${index + 1} requires string input and output.`);
        contentBytes += utf8.encode(pass.input).length + utf8.encode(pass.output).length;
      } else {
        if (!Array.isArray(pass.events)) return fail(`Pass ${index + 1} requires an events array.`);
        for (const event of pass.events) {
          if (!event || (event.source !== "interactor" && event.source !== "solution") || typeof event.content !== "string") {
            return fail(`Pass ${index + 1} events require source and string content.`);
          }
          const eventUnknown = unknownField(event, ["source", "content"]);
          if (eventUnknown) return fail(`Pass ${index + 1} event has unknown field: ${eventUnknown}.`);
          contentBytes += utf8.encode(event.content).length;
        }
      }
    }
    const maxBytes = Number.parseInt(textarea.dataset.maxBytes || "0", 10);
    if (maxBytes > 0 && contentBytes > maxBytes) return fail(`Sample content exceeds ${maxBytes} UTF-8 bytes.`);
    return { error: "", value };
  };

  const textBlock = (value) => {
    const pre = document.createElement("pre");
    pre.className = "tests-sample-preview-content";
    if (value) {
      pre.textContent = value;
    } else {
      pre.textContent = "(empty)";
      pre.classList.add("muted", "tests-sample-preview-empty");
    }
    return pre;
  };

  const renderPreview = (container, value) => {
    const body = container.querySelector("[data-sample-json-preview-body='1']");
    if (!body) return;
    body.replaceChildren();
    value.passes.forEach((pass, index) => {
      const section = document.createElement("section");
      section.className = "tests-sample-preview-pass";
      const title = document.createElement("h5");
      title.textContent = `Pass ${pass.number ?? index + 1}`;
      section.append(title);
      if (value.presentation === "pair") {
        const grid = document.createElement("div");
        grid.className = "tests-sample-preview-grid";
        [["Input", pass.input], ["Output", pass.output]].forEach(([label, content]) => {
          const card = document.createElement("section");
          card.className = "tests-sample-preview-card";
          const heading = document.createElement("h6");
          heading.textContent = label;
          card.append(heading, textBlock(content));
          grid.append(card);
        });
        section.append(grid);
      } else {
        const transcript = document.createElement("div");
        transcript.className = "tests-sample-preview-transcript";
        pass.events.forEach((event) => {
          const row = document.createElement("div");
          row.className = `tests-sample-preview-event tests-sample-preview-event-${event.source}`;
          row.append(textBlock(event.content));
          transcript.append(row);
        });
        section.append(transcript);
      }
      body.append(section);
    });
    container.hidden = false;
  };

  const sync = (form) => {
    const toggle = form.querySelector("[data-sample-toggle='1']");
    const group = form.querySelector("[data-sample-output-validate-group='1']");
    const format = form.querySelector("[data-sample-format='1']");
    const legacy = form.querySelector("[data-sample-legacy='1']");
    const jsonGroup = form.querySelector("[data-sample-json-group='1']");
    const textarea = form.querySelector("[data-sample-json='1']");
    if (!toggle || !group || !format || !legacy || !jsonGroup || !textarea) return;
    const legacySelected = toggle.checked && format.value === "legacy";
    const structured = toggle.checked && format.value === "json";
    legacy.hidden = !legacySelected;
    jsonGroup.hidden = !structured;
    legacy.querySelectorAll("input, textarea, select").forEach((control) => { control.disabled = !legacySelected; });
    jsonGroup.querySelectorAll("input, textarea, select").forEach((control) => { control.disabled = !structured; });
    group.hidden = !legacySelected;
    const result = structured ? validateJson(textarea) : { error: "", value: null };
    const status = form.querySelector("[data-sample-json-status='1']");
    const preview = form.querySelector("[data-sample-json-preview='1']");
    if (status) {
      status.textContent = result.error;
      status.hidden = !result.error;
      status.classList.toggle("tone-fail", Boolean(result.error));
    }
    if (preview) {
      preview.hidden = true;
      if (structured && !result.error && result.value) renderPreview(preview, result.value);
    }
    textarea.setCustomValidity(result.error);
  };
  document.querySelectorAll("form[data-sample-form='1']").forEach((form) => {
    form.querySelectorAll("[data-sample-json-example]").forEach((button) => {
      button.addEventListener("click", () => {
        const textarea = form.querySelector("[data-sample-json='1']");
        const example = examples[button.dataset.sampleJsonExample];
        if (!textarea || !example) return;
        textarea.value = JSON.stringify(example, null, 2);
        sync(form);
        textarea.focus();
      });
    });
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
