const { onReady } = window.PolygonUI;

onReady(() => {
  const list = document.querySelector("[data-mark-save-url]");
  if (!list) return;
  let active = null;
  let opening = 0;

  list.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-mark-key]");
    if (!button) return;
    const generation = ++opening;
    if (active && !(await active.finish(true))) return;
    if (generation !== opening) return;

    const original = button.dataset.markValue;
    const editor = document.createElement("span");
    editor.className = "contest-mark-editor";
    const input = document.createElement("input");
    input.className = "contest-mark-input";
    input.value = original;
    input.autocomplete = "off";
    input.setAttribute("aria-label", button.getAttribute("aria-label"));
    const choices = document.createElement("span");
    choices.className = "contest-mark-choices";
    for (const emoji of ["\u2705", "\u274c", "\u2755", "\u2753", "\u2049\ufe0f"]) {
      const choice = document.createElement("button");
      choice.type = "button";
      choice.textContent = emoji;
      choice.setAttribute("aria-label", `Use ${emoji}`);
      choice.addEventListener("pointerdown", (e) => e.preventDefault());
      choice.addEventListener("click", () => {
        const text = input.value.replace(/^(?:\u2705|\u274c|\u2755|\u2753|\u2049\ufe0f?)\s*/u, "");
        input.value = emoji + (text ? ` ${text}` : "");
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      });
      choices.append(choice);
    }
    const error = document.createElement("span");
    error.className = "danger";
    error.setAttribute("role", "alert");
    error.hidden = true;
    editor.append(input, choices, error);
    button.hidden = true;
    button.after(editor);
    let pending = null;
    let finished = false;
    const state = { finish, dirty: () => pending || input.value !== original };
    active = state;

    function close(value, focus) {
      finished = true;
      if (active === state) active = null;
      button.dataset.markValue = value;
      button.textContent = value || "Add mark";
      button.classList.toggle("empty", !value);
      editor.remove();
      button.hidden = false;
      if (focus) button.focus();
    }

    function finish(save, focus = false) {
      if (pending) return pending;
      if (finished) return Promise.resolve(true);
      const value = input.value.trim();
      if (!save || value === original) {
        close(original, focus);
        return Promise.resolve(true);
      }
      input.readOnly = true;
      choices.querySelectorAll("button").forEach((choice) => { choice.disabled = true; });
      error.hidden = true;
      pending = (async () => {
        try {
          const response = await fetch(list.dataset.markSaveUrl, {
            method: "POST",
            headers: { "Accept": "application/json" },
            body: new URLSearchParams({
              property_keys: button.dataset.markKey,
              property_values: value,
              response_format: "json",
            }),
          });
          if (!response.ok || response.redirected) throw new Error(`Save failed (${response.status})`);
          const result = await response.json();
          const saved = result.values?.[button.dataset.markKey];
          if (typeof saved !== "string") throw new Error("Invalid save response");
          close(saved, focus);
          return true;
        } catch (reason) {
          error.textContent = reason instanceof Error ? reason.message : "Save failed";
          error.hidden = false;
          input.focus();
          return false;
        } finally {
          pending = null;
          input.readOnly = false;
          choices.querySelectorAll("button").forEach((choice) => { choice.disabled = false; });
        }
      })();
      return pending;
    }

    input.addEventListener("keydown", (e) => {
      if (e.isComposing) return;
      if (e.key === "Enter" || e.key === "Escape") {
        e.preventDefault();
        void finish(e.key === "Enter", true);
      }
    });
    editor.addEventListener("focusout", () => requestAnimationFrame(() => {
      if (!editor.contains(document.activeElement)) void finish(true);
    }));
    input.focus();
    input.select();
  });
  window.addEventListener("beforeunload", (event) => {
    if (active?.dirty()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
});
