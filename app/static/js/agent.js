import { onReady, setSubmitting, showInlineError, writeTextToClipboard } from "./core.js";

function initAgentConnect() {
  const button = document.querySelector("[data-agent-connect-button='1']");
  const result = document.querySelector("[data-agent-connect-result='1']");
  const url = document.querySelector("[data-agent-register-url='1']");
  const meta = document.querySelector("[data-agent-register-meta='1']");
  const copy = document.querySelector("[data-agent-copy-url='1']");
  if (!button || !result || !url || !meta || !copy) return;
  const label = button.textContent.trim() || "Connect to Agent";
  button.addEventListener("click", async () => {
    setSubmitting(button, label, "Creating...", true);
    try {
      const response = await fetch("/agent/connect", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch", Accept: "application/json" },
      });
      let payload = {};
      try { payload = await response.json(); } catch (_error) { payload = {}; }
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Request failed.");
      url.value = String(payload.register_url || "");
      const expires = new Date(payload.expires_at);
      meta.textContent = Number.isFinite(expires.getTime()) ? `Expires at ${expires.toLocaleString()}` : "Registration URL created.";
      result.hidden = false;
    } catch (error) {
      showInlineError(null, error.message || "Request failed: network error");
    } finally {
      setSubmitting(button, label, "Creating...", false);
    }
  });
  copy.addEventListener("click", async () => {
    try {
      await writeTextToClipboard(url.value);
      const label = copy.textContent;
      copy.textContent = "Copied";
      window.setTimeout(() => { copy.textContent = label; }, 1200);
    } catch (_error) {
      showInlineError(null, "Copy failed; select the URL manually.");
    }
  });
}

onReady(initAgentConnect);
