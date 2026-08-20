const { onReady, showInlineError } = window.PolygonUI;

function bytesToHex(bytes) {
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

function hexToBytes(hex) {
  const output = new Uint8Array(hex.length / 2);
  for (let index = 0; index < output.length; index += 1) output[index] = Number.parseInt(hex.slice(index * 2, index * 2 + 2), 16);
  return output;
}

async function pbkdf2(password, salt, iterations) {
  const material = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt: hexToBytes(salt), iterations }, material, 256);
  return bytesToHex(new Uint8Array(bits));
}

function decodeBase64Url(value) {
  let normalized = String(value || "").replace(/-/g, "+").replace(/_/g, "/");
  while (normalized.length % 4) normalized += "=";
  return Uint8Array.from(window.atob(normalized), (character) => character.charCodeAt(0));
}

function encodeBase64Url(value) {
  const binary = Array.from(new Uint8Array(value), (byte) => String.fromCharCode(byte)).join("");
  return window.btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

async function createEnvelope(scope, purpose, username, csrfToken, verifier) {
  const query = new URLSearchParams({ scope, purpose, username, csrf_token: csrfToken });
  const response = await fetch(`/auth/password-envelope?${query}`, { credentials: "same-origin" });
  if (!response.ok) throw new Error("Password envelope request failed.");
  const payload = await response.json();
  const publicKey = await crypto.subtle.importKey(
    "spki",
    decodeBase64Url(payload.public_key),
    { name: "RSA-OAEP", hash: "SHA-256" },
    false,
    ["encrypt"],
  );
  const encrypted = await crypto.subtle.encrypt({ name: "RSA-OAEP" }, publicKey, new TextEncoder().encode(verifier));
  return {
    keyId: String(payload.key_id || ""),
    token: String(payload.envelope_token || ""),
    verifier: encodeBase64Url(encrypted),
  };
}

function fail(form, message) {
  delete form.dataset.passwordPending;
  showInlineError(form.querySelector("[data-password-error='1']"), message);
}

function cryptoAvailable(form) {
  if (window.crypto && window.crypto.subtle) return true;
  fail(form, "WebCrypto is required for password submission.");
  return false;
}

function field(form, name) {
  return form.querySelector(`[name="${name}"]`);
}

function metadata(form, saltName, iterationsName) {
  const salt = String(field(form, saltName)?.value || "").trim().toLowerCase();
  const iterations = Number(field(form, iterationsName)?.value || 0);
  if (!/^[0-9a-f]{32}$/.test(salt) || !Number.isFinite(iterations) || iterations <= 0) throw new Error("Invalid password metadata.");
  return { salt, iterations: Math.floor(iterations) };
}

function assignEnvelope(form, prefix, envelope) {
  const keyId = field(form, `${prefix}key_id`);
  const token = field(form, `${prefix}envelope_token`);
  const verifier = field(form, `${prefix}encrypted_verifier`);
  if (!keyId || !token || !verifier) throw new Error("Password envelope fields are missing.");
  keyId.value = envelope.keyId;
  token.value = envelope.token;
  verifier.value = envelope.verifier;
}

function bindEnvelopeForm(form, prepare) {
  if (!form) return;
  form.addEventListener("submit", (event) => {
    if (form.dataset.passwordPrepared === "1") return;
    event.preventDefault();
    if (form.dataset.passwordPending === "1" || !cryptoAvailable(form)) return;
    form.dataset.passwordPending = "1";
    Promise.resolve(prepare(form))
      .then(() => {
        form.dataset.passwordPrepared = "1";
        delete form.dataset.passwordPending;
        form.submit();
      })
      .catch((error) => fail(form, error && error.message ? error.message : "Failed to prepare password envelope."));
  });
}

function bindLogin() {
  bindEnvelopeForm(document.getElementById("login-form"), async (form) => {
    const username = String(field(form, "username")?.value || "").trim();
    const password = String(field(form, "password")?.value || "");
    if (!password) return;
    const csrf = String(field(form, "csrf_token")?.value || "").trim();
    if (!csrf) throw new Error("Invalid password token.");
    const query = new URLSearchParams({ username, csrf_token: csrf });
    const response = await fetch(`/auth/password-meta?${query}`, { credentials: "same-origin" });
    if (!response.ok) throw new Error("Failed to load password metadata.");
    const payload = await response.json();
    const salt = String(payload.salt || "").trim().toLowerCase();
    const iterations = Number(payload.iters || 0);
    if (!/^[0-9a-f]{32}$/.test(salt) || !Number.isFinite(iterations) || iterations <= 0) throw new Error("Invalid password metadata.");
    const verifier = await pbkdf2(password, salt, Math.floor(iterations));
    assignEnvelope(form, "", await createEnvelope("login-password", "login", username, csrf, verifier));
    field(form, "password").value = "";
  });
}

function bindRegister(id, scope, purpose) {
  bindEnvelopeForm(document.getElementById(id), async (form) => {
    const password = String(field(form, "password")?.value || "");
    const confirmation = String(field(form, "password_confirm")?.value || "");
    if (password !== confirmation) throw new Error("Password confirmation does not match.");
    if (!password) return;
    const csrf = String(field(form, "csrf_token")?.value || "").trim();
    if (!csrf) throw new Error("Invalid password token.");
    const meta = metadata(form, "password_salt", "password_iters");
    const username = String(field(form, "username")?.value || "").trim();
    const verifier = await pbkdf2(password, meta.salt, meta.iterations);
    assignEnvelope(form, "", await createEnvelope(scope, purpose, username, csrf, verifier));
    field(form, "password").value = "";
    field(form, "password_confirm").value = "";
  });
}

function bindEmailPatternCheck({
  emailId,
  buttonId,
  resultId,
  endpoint,
  patternId = "",
}) {
  const email = document.getElementById(emailId);
  const button = document.getElementById(buttonId);
  const result = document.getElementById(resultId);
  const pattern = patternId ? document.getElementById(patternId) : null;
  if (!email || !button || !result) return;

  const clearResult = () => {
    result.textContent = "";
    result.classList.remove("ok", "danger");
  };
  email.addEventListener("input", clearResult);
  pattern?.addEventListener("input", clearResult);

  button.addEventListener("click", async () => {
    const value = String(email.value || "").trim();
    clearResult();
    if (!value || !email.checkValidity()) {
      result.textContent = "Enter a valid email address first.";
      result.classList.add("danger");
      return;
    }
    if (pattern && !String(pattern.value || "")) {
      result.textContent = "Enter an allowed registration email pattern first.";
      result.classList.add("danger");
      return;
    }

    button.disabled = true;
    result.textContent = "Checking…";
    try {
      const body = new URLSearchParams({ email: value });
      if (pattern) body.set("email_allow_regex", String(pattern.value));
      const response = await fetch(endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        },
        body,
      });
      const payload = await response.json();
      result.textContent = String(payload.message || "Email validation failed.");
      result.classList.add(
        response.ok && payload.allowed === true ? "ok" : "danger",
      );
    } catch (_error) {
      result.textContent = "Email validation is unavailable.";
      result.classList.add("danger");
    } finally {
      button.disabled = false;
    }
  });
}

function bindPasswordChange(form, options) {
  bindEnvelopeForm(form, async (activeForm) => {
    const next = String(field(activeForm, "new_password")?.value || "");
    const confirmation = String(field(activeForm, "new_password_confirm")?.value || "");
    if (next !== confirmation) throw new Error("Password confirmation does not match.");
    const current = options.current ? String(field(activeForm, "current_password")?.value || "") : "";
    if (!next || (options.current && !current)) return;
    const csrf = String(field(activeForm, "csrf_token")?.value || "").trim();
    if (!csrf) throw new Error("Invalid password token.");
    const username = options.username(activeForm);
    if (options.current) {
      const currentMeta = metadata(activeForm, "current_password_salt", "current_password_iters");
      const currentVerifier = await pbkdf2(current, currentMeta.salt, currentMeta.iterations);
      assignEnvelope(activeForm, "current_password_", await createEnvelope(options.scope, "settings-current", username, csrf, currentVerifier));
    }
    const nextMeta = metadata(activeForm, "new_password_salt", "new_password_iters");
    const nextVerifier = await pbkdf2(next, nextMeta.salt, nextMeta.iterations);
    assignEnvelope(activeForm, "new_password_", await createEnvelope(options.scope, options.purpose, username, csrf, nextVerifier));
    if (options.current) field(activeForm, "current_password").value = "";
    field(activeForm, "new_password").value = "";
    field(activeForm, "new_password_confirm").value = "";
  });
}

function bindSudo() {
  bindEnvelopeForm(document.getElementById("sudo-form"), async (form) => {
    const password = String(field(form, "password")?.value || "");
    if (!password) return;
    const csrf = String(field(form, "csrf_token")?.value || "").trim();
    if (!csrf) throw new Error("Invalid password token.");
    const meta = metadata(form, "password_salt", "password_iters");
    const verifier = await pbkdf2(password, meta.salt, meta.iterations);
    assignEnvelope(form, "", await createEnvelope("sudo-password", "sudo", "", csrf, verifier));
    field(form, "password").value = "";
  });
}

onReady(() => {
  bindLogin();
  bindRegister("register-form", "register-password", "register");
  bindEmailPatternCheck({
    emailId: "email",
    buttonId: "email-check",
    resultId: "email-check-result",
    endpoint: "/register/email-check",
  });
  bindEmailPatternCheck({
    emailId: "setup-email-test",
    buttonId: "setup-email-check",
    resultId: "setup-email-check-result",
    endpoint: "/setup/email-check",
    patternId: "auth-email-allow-regex",
  });
  bindRegister("setup-form", "setup-password", "setup");
  bindPasswordChange(document.getElementById("settings-password-form"), {
    current: true,
    scope: "settings-password",
    purpose: "settings-new",
    username: () => "",
  });
  bindPasswordChange(document.getElementById("admin-password-form"), {
    current: false,
    scope: "admin-password",
    purpose: "admin-new",
    username: (form) => String(field(form, "target_username")?.value || "").trim(),
  });
  bindSudo();
});
