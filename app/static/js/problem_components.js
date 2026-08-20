const {
  findCodeMirrorEditorForTextarea,
  onReady,
  setSubmitting,
  showConfirmDialog,
  showInlineError,
  syncCodeEditorsInForm,
} = window.PolygonUI;

const EDITOR_READY_EVENT = "polygonlike:code-editor-ready";
let suppressBeforeUnload = false;

function tagState(value) {
  const states = {
    main_correct: "main-correct",
    accepted: "accepted",
    wrong_answer: "wrong-answer",
    time_limit_exceeded: "time-limit-exceeded",
    run_time_error: "run-time-error",
    compile_error: "compile-error",
    rejected: "rejected",
    unknown: "neutral",
  };
  return states[value] || "expected";
}

function applyTagColor(select) {
  Array.from(select.classList).forEach((name) => {
    if (name.startsWith("tag-select-") && name !== "tag-select") select.classList.remove(name);
  });
  select.classList.add(`tag-select-${tagState(select.value)}`);
}

function initTagSelects() {
  document.querySelectorAll("select.tag-select").forEach((select) => {
    applyTagColor(select);
    select.addEventListener("change", () => {
      applyTagColor(select);
      if (select.dataset.submitOnChange === "1" && select.form) select.form.requestSubmit();
    });
  });
}

function guardState(form) {
  return form.__polygonCodeEditorGuard || null;
}

function initDirtyGuards() {
  const forms = Array.from(document.querySelectorAll("form[data-code-editor-guard='1']"));
  if (!forms.length) return;
  const markDirty = (form) => {
    const state = guardState(form);
    if (state && !state.pending) state.dirty = true;
  };
  const bindEditor = (textarea) => {
    if (!textarea || textarea.dataset.codeEditorGuardBound === "1") return;
    const form = textarea.closest("form[data-code-editor-guard='1']");
    const editor = findCodeMirrorEditorForTextarea(textarea);
    if (!form || !editor || typeof editor.on !== "function") return;
    textarea.dataset.codeEditorGuardBound = "1";
    editor.on("change", () => markDirty(form));
  };
  forms.forEach((form) => {
    form.__polygonCodeEditorGuard = { dirty: false, pending: false };
    form.addEventListener("input", (event) => {
      if (event.target && event.target.name && event.target.type !== "hidden") markDirty(form);
    });
    form.addEventListener("change", (event) => {
      if (event.target && event.target.name && event.target.type !== "hidden") markDirty(form);
    });
    form.querySelectorAll("textarea[data-code-editor='1']").forEach(bindEditor);
  });
  document.addEventListener(EDITOR_READY_EVENT, (event) => bindEditor(event.detail && event.detail.textarea));
  window.addEventListener("beforeunload", (event) => {
    if (suppressBeforeUnload) return;
    const dirty = forms.some((form) => {
      const state = guardState(form);
      return state && state.dirty && !state.pending;
    });
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

function setGuardPending(form, pending) {
  const state = guardState(form);
  if (state) state.pending = Boolean(pending);
}

function markSaved(form) {
  const state = guardState(form);
  if (state) {
    state.dirty = false;
    state.pending = false;
  }
  suppressBeforeUnload = true;
}

function editorError(form, message) {
  const region = form.querySelector("[data-component-editor-error='1']");
  showInlineError(region, message, "Save failed");
}

function clearEditorError(form) {
  const region = form.querySelector("[data-component-editor-error='1']");
  if (!region) return;
  region.hidden = true;
  region.textContent = "";
}

function bindAsyncSave(form, options = {}) {
  form.addEventListener("submit", async (event) => {
    const submitter = event.submitter || null;
    const action = String((submitter && submitter.getAttribute("formaction")) || "").trim();
    const method = String((submitter && submitter.getAttribute("formmethod")) || "").toUpperCase();
    if (action || (method && method !== "POST")) return;
    event.preventDefault();
    const button = options.button || (submitter instanceof HTMLButtonElement ? submitter : form.querySelector("button[type='submit']"));
    if (!button || button.disabled) return;
    const normalLabel = String(button.textContent || "Save Source").trim();
    clearEditorError(form);
    syncCodeEditorsInForm(form);
    setGuardPending(form, true);
    setSubmitting(button, normalLabel, "Saving...", true);
    try {
      const data = new FormData(form);
      if (options.responseMode) data.set("response_mode", options.responseMode);
      const response = await fetch(form.action, {
        method: "POST",
        body: data,
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch", Accept: "application/json" },
      });
      let payload = {};
      try { payload = await response.json(); } catch (_error) { payload = {}; }
      if (response.ok && payload.ok && payload.redirect) {
        markSaved(form);
        window.location.assign(String(payload.redirect));
        return;
      }
      setGuardPending(form, false);
      editorError(form, payload.error || payload.message || "Save failed");
    } catch (_error) {
      setGuardPending(form, false);
      editorError(form, "Save failed: network error");
    }
    setSubmitting(button, normalLabel, "Saving...", false);
  });
}

function initAsyncEditors() {
  document.querySelectorAll("form[data-component-source-save-form='1']").forEach((form) => {
    bindAsyncSave(form, { responseMode: "json" });
  });
  const solutionForm = document.getElementById("solution-save-form");
  if (solutionForm) bindAsyncSave(solutionForm, { button: document.getElementById("solution-save-submit") });
}

function initStarterActions() {
  document.querySelectorAll("[data-insert-code-starter='1']").forEach((button) => {
    button.addEventListener("click", async () => {
      const form = button.closest("form");
      const textarea = form && form.querySelector("textarea[data-code-editor='1']");
      const starter = form && form.querySelector("textarea[data-code-starter='1']");
      if (!form || !textarea || !starter) return;
      const editor = findCodeMirrorEditorForTextarea(textarea);
      const current = editor && typeof editor.getValue === "function" ? editor.getValue() : textarea.value;
      if (String(current || "").trim()) {
        const replace = await showConfirmDialog("Replace the current draft with the testlib template?", button);
        if (!replace) return;
      }
      if (editor && typeof editor.setValue === "function") {
        editor.setValue(starter.value);
        editor.focus();
      } else {
        textarea.value = starter.value;
        textarea.dispatchEvent(new Event("input", { bubbles: true }));
        textarea.focus();
      }
    });
  });
}

function initProblemComponents() {
  initTagSelects();
  initDirtyGuards();
  initAsyncEditors();
  initStarterActions();
}

onReady(initProblemComponents);
