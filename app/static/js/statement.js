const {
  findCodeMirrorEditorForTextarea,
  localStorageAvailable,
  onReady,
  setSubmitting,
  showConfirmDialog,
  storageScopeToken,
  submitForm,
} = window.PolygonUI;

const EDITOR_READY_EVENT = "polygonlike:code-editor-ready";
const HISTORY_LIMIT = 10;
const SAVE_DELAY_MS = 900;

function initCompileState() {
  document.querySelectorAll("form[data-preview-compile-form='1']").forEach((form) => {
    form.addEventListener("submit", () => {
      const button = form.querySelector("[data-preview-compile-button='1']") || form.querySelector("button[type='submit']");
      if (!button || button.disabled) return;
      setSubmitting(button, button.textContent.trim() || "Compile Statement", "Compiling...", true);
    });
  });
}

function initLanguageSwitch() {
  const canStore = localStorageAvailable("__polygonlike_statement_language_probe__");
  const hasExplicitLanguage = new URLSearchParams(window.location.search).has("language");
  document.querySelectorAll("form[data-statement-language-form='1']").forEach((form) => {
    const select = form.querySelector("[data-statement-language-select='1']");
    if (!select) return;
    const key = [
      "statement-language",
      storageScopeToken(form.dataset.statementLanguageProblem),
      storageScopeToken(form.dataset.statementLanguageUser),
    ].join(":");
    const remember = () => {
      if (!canStore || !select.value) return;
      try { window.localStorage.setItem(key, select.value); } catch (_error) { /* session-only */ }
    };
    if (hasExplicitLanguage) remember();
    else if (canStore) {
      let remembered = "";
      try { remembered = window.localStorage.getItem(key) || ""; } catch (_error) { remembered = ""; }
      if (remembered && Array.from(select.options).some((option) => option.value === remembered) && select.value !== remembered) {
        select.value = remembered;
        submitForm(form);
        return;
      }
    }
    select.addEventListener("change", () => {
      remember();
      submitForm(form);
    });
  });
}

function initExamplesTemplateToggle() {
  document.querySelectorAll("form[data-statement-examples-toggle-form='1']").forEach((form) => {
    const toggle = form.querySelector("[data-statement-examples-toggle='1']");
    if (!toggle) return;
    toggle.addEventListener("change", async () => {
      if (!toggle.checked) {
        const confirmed = await showConfirmDialog(
          "Disable the editable examples template? statement/examples.tex will be deleted and rendering will use the built-in default.",
          toggle,
        );
        if (!confirmed) {
          toggle.checked = true;
          return;
        }
      }
      submitForm(form);
    });
  });
}

function initDraftHistory() {
  const form = document.querySelector("form[data-statement-draft-form='1']");
  if (!form) return;
  const status = document.querySelector("[data-statement-draft-status='1']");
  const error = document.querySelector("[data-statement-draft-error='1']");
  const empty = document.querySelector("[data-statement-draft-empty='1']");
  const list = document.querySelector("[data-statement-draft-list='1']");
  const triggers = Array.from(document.querySelectorAll("[data-statement-draft-trigger='1']"));
  if (!status || !error || !empty || !list || !triggers.length) return;
  if (!localStorageAvailable("__polygonlike_statement_draft_probe__")) {
    error.textContent = "Local draft backup is unavailable in this browser session.";
    error.hidden = false;
    status.textContent = "Local drafts unavailable.";
    return;
  }

  const scope = [
    "statement-drafts-v2",
    storageScopeToken(form.dataset.draftScopeProblem),
    storageScopeToken(form.dataset.draftScopeUser),
    storageScopeToken(form.dataset.draftScopePage),
    storageScopeToken(form.dataset.draftScopeLanguage),
  ].join(":");
  const timers = new Map();
  const baselines = new Map();
  let activeField = "";
  let submitting = false;

  const storageKey = (fieldName) => `${scope}:${storageScopeToken(fieldName)}`;
  const textareaFor = (fieldName) => form.querySelector(`textarea[name="${CSS.escape(fieldName)}"]`);
  const valueFor = (fieldName) => {
    const textarea = textareaFor(fieldName);
    if (!textarea) return null;
    const editor = findCodeMirrorEditorForTextarea(textarea);
    return editor && typeof editor.getValue === "function" ? editor.getValue() : textarea.value;
  };
  const historyFor = (fieldName) => {
    try {
      const parsed = JSON.parse(window.localStorage.getItem(storageKey(fieldName)) || "[]");
      if (!Array.isArray(parsed)) return [];
      return parsed.filter((row) => row && typeof row.value === "string" && typeof row.savedAt === "string").slice(0, HISTORY_LIMIT);
    } catch (_error) {
      return [];
    }
  };
  const fieldLabel = (fieldName) => {
    const trigger = triggers.find((item) => item.dataset.draftField === fieldName);
    return trigger ? (trigger.dataset.draftLabel || fieldName) : fieldName;
  };
  const render = (fieldName) => {
    activeField = fieldName;
    const rows = historyFor(fieldName);
    list.replaceChildren();
    empty.hidden = rows.length > 0;
    status.textContent = rows.length ? `${rows.length} local draft${rows.length === 1 ? "" : "s"} for ${fieldLabel(fieldName)}.` : `No local drafts for ${fieldLabel(fieldName)}.`;
    rows.forEach((row, index) => {
      const entry = document.createElement("article");
      entry.className = "statement-draft-entry";
      const head = document.createElement("div");
      head.className = "statement-draft-entry-head";
      const title = document.createElement("strong");
      title.textContent = `Draft ${index + 1}`;
      const time = document.createElement("span");
      time.className = "statement-draft-entry-meta";
      time.textContent = new Date(row.savedAt).toLocaleString();
      head.append(title, time);
      const preview = document.createElement("pre");
      preview.className = "statement-draft-entry-preview";
      preview.textContent = row.value;
      const actions = document.createElement("div");
      actions.className = "statement-draft-entry-actions";
      const restore = document.createElement("button");
      restore.type = "button";
      restore.className = "linkish-button";
      restore.textContent = "Restore";
      restore.addEventListener("click", async () => {
        const confirmed = await showConfirmDialog(`Restore this ${fieldLabel(fieldName)} draft? Current editor content will be replaced.`, restore);
        if (!confirmed) return;
        const textarea = textareaFor(fieldName);
        const editor = findCodeMirrorEditorForTextarea(textarea);
        if (editor && typeof editor.setValue === "function") editor.setValue(row.value);
        else {
          textarea.value = row.value;
          textarea.dispatchEvent(new Event("input", { bubbles: true }));
        }
        status.textContent = `${fieldLabel(fieldName)} restored.`;
      });
      actions.appendChild(restore);
      entry.append(head, preview, actions);
      list.appendChild(entry);
    });
  };
  const save = (fieldName) => {
    const value = valueFor(fieldName);
    if (value === null || value === baselines.get(fieldName)) return;
    const rows = historyFor(fieldName);
    if (!rows.length || rows[0].value !== value) {
      rows.unshift({ savedAt: new Date().toISOString(), value });
      try { window.localStorage.setItem(storageKey(fieldName), JSON.stringify(rows.slice(0, HISTORY_LIMIT))); } catch (_error) {
        error.textContent = "Unable to save the local draft.";
        error.hidden = false;
        return;
      }
    }
    baselines.set(fieldName, value);
    if (activeField === fieldName) render(fieldName);
  };
  const schedule = (fieldName) => {
    window.clearTimeout(timers.get(fieldName));
    timers.set(fieldName, window.setTimeout(() => save(fieldName), SAVE_DELAY_MS));
  };
  const bind = (textarea) => {
    if (!textarea || textarea.dataset.statementDraftBound === "1") return;
    const fieldName = textarea.name;
    if (!fieldName) return;
    textarea.dataset.statementDraftBound = "1";
    const changed = () => schedule(fieldName);
    textarea.addEventListener("input", changed);
    textarea.addEventListener("change", changed);
    const editor = findCodeMirrorEditorForTextarea(textarea);
    if (editor && typeof editor.on === "function") editor.on("change", changed);
  };

  form.querySelectorAll("textarea[name]").forEach((textarea) => {
    baselines.set(textarea.name, valueFor(textarea.name));
    bind(textarea);
  });
  document.addEventListener(EDITOR_READY_EVENT, (event) => {
    const textarea = event.detail && event.detail.textarea;
    if (textarea && textarea.form === form) bind(textarea);
  });
  triggers.forEach((trigger) => trigger.addEventListener("click", () => render(trigger.dataset.draftField || "")));
  form.addEventListener("submit", () => {
    submitting = true;
    form.querySelectorAll("textarea[name]").forEach((textarea) => save(textarea.name));
  });
  window.addEventListener("beforeunload", (event) => {
    form.querySelectorAll("textarea[name]").forEach((textarea) => save(textarea.name));
    if (submitting) return;
    const dirty = Array.from(form.querySelectorAll("textarea[name]")).some((textarea) => valueFor(textarea.name) !== baselines.get(textarea.name));
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
  render(triggers[0].dataset.draftField || "");
}

onReady(() => {
  initCompileState();
  initLanguageSwitch();
  initExamplesTemplateToggle();
  initDraftHistory();
});
