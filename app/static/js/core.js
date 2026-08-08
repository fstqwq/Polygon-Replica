const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

let activeModal = null;
let confirmResolver = null;

export function onReady(callback) {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", callback, { once: true });
    return;
  }
  callback();
}

export function setSubmitting(button, normalLabel, pendingLabel, pending) {
  if (!button) return;
  button.disabled = Boolean(pending);
  button.textContent = pending ? pendingLabel : normalLabel;
}

export function submitForm(form, submitter = null) {
  if (!form) return;
  if (typeof form.requestSubmit === "function") {
    form.requestSubmit(submitter || undefined);
    return;
  }
  form.submit();
}

export function localStorageAvailable(probeKey) {
  try {
    window.localStorage.setItem(probeKey, "1");
    window.localStorage.removeItem(probeKey);
    return true;
  } catch (_error) {
    return false;
  }
}

export function storageScopeToken(value) {
  return encodeURIComponent(String(value || "").trim());
}

export function findCodeMirrorEditorForTextarea(textarea) {
  return textarea && textarea.__polygonCodeMirror ? textarea.__polygonCodeMirror : null;
}

export function syncCodeEditorsInForm(form) {
  if (!form) return;
  form.querySelectorAll("textarea[data-code-editor='1']").forEach((textarea) => {
    const editor = findCodeMirrorEditorForTextarea(textarea);
    if (editor && typeof editor.save === "function") editor.save();
  });
}

export function showInlineError(anchor, message, fallback = "Request failed") {
  const text = String(message || "").trim() || fallback;
  let region = anchor;
  if (!region || !(region instanceof Element)) {
    region = document.querySelector("[data-page-error='1']");
  }
  if (!region) {
    region = document.createElement("p");
    region.className = "flash flash-inline danger ui-page-error";
    region.dataset.pageError = "1";
    region.setAttribute("role", "alert");
    const main = document.querySelector("main .container, main, .login-card") || document.body;
    main.prepend(region);
  }
  region.textContent = text;
  region.hidden = false;
  if (!region.hasAttribute("role")) region.setAttribute("role", "alert");
  return region;
}

export async function writeTextToClipboard(text) {
  const safeText = String(text || "");
  if (!safeText) return;
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    await navigator.clipboard.writeText(safeText);
    return;
  }
  const probe = document.createElement("textarea");
  probe.value = safeText;
  probe.readOnly = true;
  probe.className = "clipboard-probe";
  document.body.appendChild(probe);
  try {
    probe.focus();
    probe.select();
    if (!document.execCommand || !document.execCommand("copy")) throw new Error("copy failed");
  } finally {
    probe.remove();
  }
}

function focusableElements(container) {
  return Array.from(container.querySelectorAll(FOCUSABLE_SELECTOR)).filter((element) => {
    return !element.hidden && element.getAttribute("aria-hidden") !== "true";
  });
}

function setBackgroundInert(overlay, inert) {
  Array.from(document.body.children).forEach((child) => {
    if (child === overlay) return;
    if (inert) {
      child.dataset.modalWasInert = child.inert ? "1" : "0";
      child.inert = true;
      return;
    }
    child.inert = child.dataset.modalWasInert === "1";
    delete child.dataset.modalWasInert;
  });
}

function closeModal(overlay, restoreFocus = true) {
  if (!activeModal || activeModal.overlay !== overlay) return;
  const trigger = activeModal.trigger;
  document.removeEventListener("keydown", activeModal.onKeydown, true);
  setBackgroundInert(overlay, false);
  overlay.hidden = true;
  document.body.classList.remove("popup-open", "confirm-open");
  activeModal = null;
  if (restoreFocus && trigger && typeof trigger.focus === "function") trigger.focus();
}

function openModal(overlay, trigger, onEscape) {
  if (activeModal) closeModal(activeModal.overlay, false);
  overlay.hidden = false;
  setBackgroundInert(overlay, true);
  const onKeydown = (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onEscape();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = focusableElements(overlay);
    if (!focusable.length) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };
  activeModal = { overlay, trigger, onKeydown };
  document.addEventListener("keydown", onKeydown, true);
  window.setTimeout(() => {
    const focusable = focusableElements(overlay);
    const target = focusable[0] || overlay.querySelector("[role='dialog']") || overlay;
    if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
    target.focus();
  }, 0);
}

function confirmDialog() {
  let overlay = document.getElementById("ui-confirm-overlay");
  if (overlay) return overlay;
  overlay = document.createElement("div");
  overlay.id = "ui-confirm-overlay";
  overlay.className = "ui-confirm-overlay";
  overlay.hidden = true;
  overlay.innerHTML = [
    '<div class="ui-confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="ui-confirm-title" aria-describedby="ui-confirm-message">',
    '<h3 id="ui-confirm-title">Please Confirm</h3>',
    '<p id="ui-confirm-message" class="ui-confirm-message"></p>',
    '<div class="ui-confirm-actions">',
    '<button type="button" class="btn ui-confirm-cancel">Cancel</button>',
    '<button type="button" class="btn danger-link ui-confirm-ok">Confirm</button>',
    "</div></div>",
  ].join("");
  document.body.appendChild(overlay);
  return overlay;
}

export function showConfirmDialog(message, trigger = document.activeElement) {
  const overlay = confirmDialog();
  const messageElement = overlay.querySelector("#ui-confirm-message");
  const cancel = overlay.querySelector(".ui-confirm-cancel");
  const confirm = overlay.querySelector(".ui-confirm-ok");
  if (confirmResolver) confirmResolver(false);
  messageElement.textContent = String(message || "Are you sure?").trim();
  document.body.classList.add("confirm-open");
  return new Promise((resolve) => {
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      cancel.removeEventListener("click", cancelHandler);
      confirm.removeEventListener("click", confirmHandler);
      overlay.removeEventListener("click", overlayHandler);
      confirmResolver = null;
      closeModal(overlay);
      resolve(Boolean(result));
    };
    const cancelHandler = () => finish(false);
    const confirmHandler = () => finish(true);
    const overlayHandler = (event) => {
      if (event.target === overlay) finish(false);
    };
    confirmResolver = finish;
    cancel.addEventListener("click", cancelHandler);
    confirm.addEventListener("click", confirmHandler);
    overlay.addEventListener("click", overlayHandler);
    openModal(overlay, trigger, cancelHandler);
  });
}

function initPopupDialogs() {
  const overlays = Array.from(document.querySelectorAll(".ui-popup-overlay[data-popup-overlay='1']"));
  if (!overlays.length) return;
  const close = (overlay) => closeModal(overlay);
  document.querySelectorAll("[data-popup-open]").forEach((opener) => {
    opener.addEventListener("click", (event) => {
      event.preventDefault();
      const overlay = document.getElementById(String(opener.dataset.popupOpen || ""));
      if (!overlay || !overlay.classList.contains("ui-popup-overlay")) return;
      document.body.classList.add("popup-open");
      openModal(overlay, opener, () => close(overlay));
    });
  });
  overlays.forEach((overlay) => {
    overlay.querySelectorAll("[data-popup-close]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.preventDefault();
        close(overlay);
      });
    });
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) close(overlay);
    });
  });
}

function initConfirmForms() {
  document.querySelectorAll("form").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (form.dataset.confirmApproved === "1") {
        delete form.dataset.confirmApproved;
        return;
      }
      const submitter = event.submitter || null;
      const message = String((submitter && submitter.dataset.confirmMessage) || form.dataset.confirmMessage || "").trim();
      if (!message) return;
      event.preventDefault();
      showConfirmDialog(message, submitter || form).then((confirmed) => {
        if (!confirmed) return;
        form.dataset.confirmApproved = "1";
        submitForm(form, submitter);
      });
    });
  });
}

function initCopyButtons() {
  document.querySelectorAll("[data-copy-text]").forEach((button) => {
    let resetTimer = 0;
    const normal = String(button.dataset.copyLabel || button.getAttribute("aria-label") || "Copy");
    const done = String(button.dataset.copyDoneLabel || "Copied");
    button.addEventListener("click", async () => {
      try {
        await writeTextToClipboard(button.dataset.copyText || "");
      } catch (_error) {
        showInlineError(null, "Copy failed; select the text manually.");
        return;
      }
      button.dataset.copyState = "copied";
      button.setAttribute("aria-label", done);
      window.clearTimeout(resetTimer);
      resetTimer = window.setTimeout(() => {
        button.dataset.copyState = "";
        button.setAttribute("aria-label", normal);
      }, 1200);
    });
  });
}

function initSubmitLinks() {
  document.querySelectorAll("a[data-submit-form='1']").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      if (link.getAttribute("aria-disabled") === "true") return;
      submitForm(link.closest("form"));
    });
  });
}

function initAutoSubmit() {
  document.querySelectorAll("[data-auto-submit-select='1']").forEach((select) => {
    select.addEventListener("change", () => submitForm(select.form));
  });
}

function initSudoBridge() {
  const query = new URLSearchParams(window.location.search);
  if (query.get("sudo_popup_done") === "1" && window.opener && !window.opener.closed) {
    window.opener.postMessage({ type: "polygonlike:sudo-enabled" }, window.location.origin);
    window.setTimeout(() => window.close(), 0);
  }
  const forms = Array.from(document.querySelectorAll("form[data-sudo-gated='1']"));
  if (!forms.length) return;
  window.addEventListener("message", (event) => {
    if (event.origin !== window.location.origin || !event.data || event.data.type !== "polygonlike:sudo-enabled") return;
    forms.forEach((form) => { form.dataset.sudoRequired = "0"; });
  });
  forms.forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (form.dataset.sudoRequired !== "1") return;
      const url = String(form.dataset.sudoUrl || "").trim();
      if (!url) return;
      event.preventDefault();
      const popup = window.open(url, "polygonlike-sudo", "popup=yes,width=540,height=720,resizable=yes,scrollbars=yes");
      if (popup) popup.focus();
      else window.location.assign(url);
    });
  });
}

function initTopEventNotice() {
  const payload = document.querySelector("[data-top-event='1']");
  const slot = document.querySelector(".top-event-slot");
  if (!payload || !slot) return;
  const eventId = String(payload.dataset.eventId || "").trim();
  const storageKey = "polygonlike:top-events";
  let seen = {};
  try { seen = JSON.parse(window.localStorage.getItem(storageKey) || "{}") || {}; } catch (_error) { seen = {}; }
  if (eventId && seen[eventId]) return;
  const notice = document.createElement("div");
  notice.className = "top-event-notice";
  notice.dataset.level = payload.dataset.level || "info";
  const text = document.createElement("span");
  text.className = "top-event-text";
  text.textContent = payload.textContent.trim();
  const dismiss = document.createElement("button");
  dismiss.type = "button";
  dismiss.className = "top-event-dismiss";
  dismiss.textContent = "Dismiss";
  dismiss.addEventListener("click", () => {
    notice.remove();
    if (!eventId) return;
    seen[eventId] = true;
    try { window.localStorage.setItem(storageKey, JSON.stringify(seen)); } catch (_error) { /* session-only */ }
  });
  notice.append(text, dismiss);
  slot.appendChild(notice);
}

function initTooltips() {
  let tooltip = null;
  const hide = () => {
    if (tooltip) tooltip.remove();
    tooltip = null;
  };
  document.querySelectorAll("[data-tooltip]").forEach((target) => {
    const show = () => {
      hide();
      tooltip = document.createElement("div");
      tooltip.className = "ui-tooltip ui-tooltip-visible";
      tooltip.setAttribute("role", "tooltip");
      tooltip.textContent = target.dataset.tooltip || "";
      document.body.appendChild(tooltip);
      const rect = target.getBoundingClientRect();
      tooltip.style.left = `${Math.max(8, rect.left)}px`;
      tooltip.style.top = `${rect.bottom + 6}px`;
    };
    target.addEventListener("mouseenter", show);
    target.addEventListener("focus", show);
    target.addEventListener("mouseleave", hide);
    target.addEventListener("blur", hide);
  });
}

function initProfileTiming() {
  const backend = document.getElementById("profile-backend-render");
  const network = document.getElementById("profile-network-estimate");
  if (!backend && !network) return;
  const backendMs = Number(backend && backend.dataset.backendRenderMs);
  const nav = performance.getEntriesByType ? performance.getEntriesByType("navigation")[0] : null;
  const ttfb = nav ? Number(nav.responseStart) - Number(nav.requestStart) : NaN;
  const format = (value) => Number.isFinite(value) && value >= 0 ? `${Math.round(value)} ms` : "n/a";
  if (backend) backend.textContent = format(backendMs);
  if (network) network.textContent = format(Number.isFinite(ttfb) && Number.isFinite(backendMs) ? Math.max(0, ttfb - backendMs) : NaN);
}

export function initCore() {
  initSudoBridge();
  initTopEventNotice();
  initTooltips();
  initProfileTiming();
  initPopupDialogs();
  initConfirmForms();
  initSubmitLinks();
  initAutoSubmit();
  initCopyButtons();
}
