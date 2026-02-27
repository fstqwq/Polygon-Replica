(function () {
  "use strict";

  function onReady(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
      return;
    }
    fn();
  }

  function bytesToHex(bytes) {
    return Array.from(bytes)
      .map(function (b) {
        return b.toString(16).padStart(2, "0");
      })
      .join("");
  }

  function hexToBytes(hex) {
    var normalized = String(hex || "").trim();
    var out = new Uint8Array(normalized.length / 2);
    for (var i = 0; i < out.length; i += 1) {
      out[i] = parseInt(normalized.slice(i * 2, i * 2 + 2), 16);
    }
    return out;
  }

  async function sha256Hex(text) {
    var data = new TextEncoder().encode(String(text || ""));
    var digest = await crypto.subtle.digest("SHA-256", data);
    return bytesToHex(new Uint8Array(digest));
  }

  async function pbkdf2Hex(password, saltHex, iterations) {
    var keyMaterial = await crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(String(password || "")),
      "PBKDF2",
      false,
      ["deriveBits"]
    );
    var bits = await crypto.subtle.deriveBits(
      {
        name: "PBKDF2",
        hash: "SHA-256",
        salt: hexToBytes(saltHex),
        iterations: iterations,
      },
      keyMaterial,
      256
    );
    return bytesToHex(new Uint8Array(bits));
  }

  function requireWebCrypto() {
    if (window.crypto && window.crypto.subtle) {
      return true;
    }
    window.alert("WebCrypto is required for password submission.");
    return false;
  }

  function setSubmitting(button, baseLabel, loadingLabel, loading) {
    if (!button) return;
    button.disabled = !!loading;
    button.textContent = loading ? loadingLabel : baseLabel;
  }

  function initNavActiveState() {
    var pageLinks = document.querySelectorAll("a[data-page]");
    if (!pageLinks.length) return;

    var parts = window.location.pathname.split("/");
    var page = parts.length >= 5 ? parts[4] : "general";
    var qp = new URLSearchParams(window.location.search);

    if (page === "artifacts") page = "tests";
    if (page === "runs") page = "run";
    if (page === "git" || page === "history") page = "workspace";
    if (page === "build") page = "tests";
    if (page === "preview") page = "general";

    if (page === "files") {
      var selectedPath = qp.get("path") || "";
      if (selectedPath.indexOf("checkers/") === 0) page = "checker";
      else if (selectedPath.indexOf("interactors/") === 0) page = "interactor";
      else if (selectedPath.indexOf("validators/") === 0) page = "validator";
      else if (selectedPath.indexOf("solutions/") === 0) page = "solutions";
      else if (selectedPath === "generators" || selectedPath.indexOf("generators/") === 0) page = "generators";
    }

    var allowed = {
      general: 1,
      files: 1,
      generators: 1,
      checker: 1,
      interactor: 1,
      validator: 1,
      solutions: 1,
      workspace: 1,
      access: 1,
      tests: 1,
      run: 1,
      export: 1,
      settings: 1,
    };
    if (!allowed[page]) page = "general";

    document.querySelectorAll("a[data-main]").forEach(function (el) {
      if (el.getAttribute("data-main") === "problems") {
        el.classList.add("active");
      }
    });
    pageLinks.forEach(function (el) {
      if (el.getAttribute("data-page") === page) {
        el.classList.add("active");
      }
    });
    document.querySelectorAll(".problem-submenu-item").forEach(function (item) {
      var pageLink = item.querySelector("a[data-page]");
      if (pageLink && pageLink.classList.contains("active")) {
        item.classList.add("active");
      }
    });
    document.querySelectorAll("input.page-target").forEach(function (el) {
      el.value = allowed[page] ? page : "general";
    });
  }

  function initFlashAutohide() {
    var toasts = document.querySelectorAll(".flash-floating-center[data-autohide='1']");
    if (!toasts.length) return;
    window.setTimeout(function () {
      toasts.forEach(function (el) {
        el.classList.add("flash-hide");
        window.setTimeout(function () {
          el.style.display = "none";
        }, 280);
      });
    }, 5200);
  }

  function initDataTooltips() {
    var targets = document.querySelectorAll("[data-tooltip]");
    if (!targets.length) return;

    var tooltip = document.createElement("div");
    tooltip.className = "ui-tooltip";
    tooltip.setAttribute("role", "tooltip");
    tooltip.hidden = true;
    document.body.appendChild(tooltip);

    var activeTarget = null;
    var hideTimer = 0;

    function clearHideTimer() {
      if (hideTimer) {
        window.clearTimeout(hideTimer);
        hideTimer = 0;
      }
    }

    function positionTooltip(target) {
      if (!target || tooltip.hidden) return;
      var rect = target.getBoundingClientRect();
      var tipRect = tooltip.getBoundingClientRect();
      var gap = 8;
      var margin = 8;
      var top = rect.top - tipRect.height - gap;
      if (top < margin) {
        top = rect.bottom + gap;
      }
      if (top + tipRect.height > window.innerHeight - margin) {
        top = Math.max(margin, window.innerHeight - margin - tipRect.height);
      }
      var left = rect.left + rect.width / 2 - tipRect.width / 2;
      if (left < margin) {
        left = margin;
      }
      var maxLeft = window.innerWidth - margin - tipRect.width;
      if (left > maxLeft) {
        left = Math.max(margin, maxLeft);
      }
      tooltip.style.top = String(Math.round(top)) + "px";
      tooltip.style.left = String(Math.round(left)) + "px";
    }

    function hideTooltip() {
      clearHideTimer();
      activeTarget = null;
      tooltip.classList.remove("ui-tooltip-visible");
      tooltip.hidden = true;
    }

    function scheduleHide(delayMs) {
      clearHideTimer();
      hideTimer = window.setTimeout(hideTooltip, delayMs);
    }

    function showTooltip(target) {
      var text = String(target.getAttribute("data-tooltip") || "").trim();
      if (!text) {
        hideTooltip();
        return;
      }
      clearHideTimer();
      activeTarget = target;
      tooltip.textContent = text;
      tooltip.hidden = false;
      positionTooltip(target);
      tooltip.classList.add("ui-tooltip-visible");
    }

    targets.forEach(function (target) {
      var text = String(target.getAttribute("data-tooltip") || "").trim();
      if (!text) return;
      if (!target.hasAttribute("tabindex")) {
        target.setAttribute("tabindex", "0");
      }
      target.addEventListener("mouseenter", function () {
        showTooltip(target);
      });
      target.addEventListener("mouseleave", function () {
        scheduleHide(40);
      });
      target.addEventListener("focus", function () {
        showTooltip(target);
      });
      target.addEventListener("blur", hideTooltip);
      target.addEventListener("keydown", function (ev) {
        if (ev.key === "Escape") {
          hideTooltip();
        }
      });
      target.addEventListener("click", function () {
        showTooltip(target);
      });
    });

    window.addEventListener(
      "scroll",
      function () {
        if (activeTarget) {
          positionTooltip(activeTarget);
        }
      },
      true
    );
    window.addEventListener("resize", function () {
      if (activeTarget) {
        positionTooltip(activeTarget);
      }
    });
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) {
        hideTooltip();
      }
    });
  }

  function initNetworkEstimateProfile() {
    var backendSlot = document.getElementById("profile-backend-render");
    var ttfbSlot = document.getElementById("profile-ttfb");
    var networkSlot = document.getElementById("profile-network-estimate");
    if (!backendSlot && !ttfbSlot && !networkSlot) return;

    function formatMs(value) {
      if (!Number.isFinite(value) || value < 0) return "n/a";
      return String(Math.round(value)) + " ms";
    }

    function readBackendMs() {
      if (!backendSlot) return NaN;
      var raw = String(backendSlot.getAttribute("data-backend-render-ms") || "").trim();
      if (!raw) return NaN;
      var value = Number(raw);
      return Number.isFinite(value) && value >= 0 ? value : NaN;
    }

    function readTtfbMs() {
      if (!window.performance || typeof performance.getEntriesByType !== "function") {
        return NaN;
      }
      var navs = performance.getEntriesByType("navigation") || [];
      if (!navs.length) return NaN;
      var nav = navs[0];
      if (!nav) return NaN;
      var requestStart = Number(nav.requestStart);
      var responseStart = Number(nav.responseStart);
      if (!Number.isFinite(requestStart) || !Number.isFinite(responseStart) || responseStart < requestStart) {
        return NaN;
      }
      return responseStart - requestStart;
    }

    function update() {
      var backendMs = readBackendMs();
      var ttfbMs = readTtfbMs();
      var networkMs = NaN;
      if (Number.isFinite(ttfbMs) && Number.isFinite(backendMs)) {
        networkMs = Math.max(0, ttfbMs - backendMs);
      }
      if (backendSlot) backendSlot.textContent = formatMs(backendMs);
      if (ttfbSlot) ttfbSlot.textContent = formatMs(ttfbMs);
      if (networkSlot) networkSlot.textContent = formatMs(networkMs);
    }

    update();
    window.addEventListener("load", update);
  }

  function initRunDetailsToggle() {
    var toggles = document.querySelectorAll(".invocation-test-toggle[data-target], .invocation-cell-toggle[data-target]");
    if (!toggles.length) return;

    function toggleById(targetId) {
      if (!targetId) return;
      var row = document.getElementById(targetId);
      if (!row) return;
      row.hidden = !row.hidden;
    }

    toggles.forEach(function (el) {
      el.addEventListener("click", function (ev) {
        ev.preventDefault();
        toggleById(el.getAttribute("data-target"));
      });
    });
  }

  function initRunExecuteSelectors() {
    function bindSelectButtons(listId, inputName, allBtnId, clearBtnId) {
      var list = document.getElementById(listId);
      if (!list) return;

      function setChecked(value) {
        list.querySelectorAll("input[name='" + inputName + "']").forEach(function (el) {
          el.checked = value;
        });
      }

      var allBtn = document.getElementById(allBtnId);
      var clearBtn = document.getElementById(clearBtnId);
      if (allBtn) {
        allBtn.addEventListener("click", function () {
          setChecked(true);
        });
      }
      if (clearBtn) {
        clearBtn.addEventListener("click", function () {
          setChecked(false);
        });
      }
    }

    bindSelectButtons("solution-paths", "solution_paths", "solution-select-all", "solution-select-clear");
    bindSelectButtons("test-names", "test_names", "test-select-all", "test-select-clear");
  }

  function tagSelectState(value) {
    if (value === "main_correct") return "main-correct";
    if (value === "accepted") return "accepted";
    if (value === "wrong_answer") return "wrong-answer";
    if (value === "time_limit_exceeded") return "time-limit-exceeded";
    if (value === "run_time_error") return "run-time-error";
    if (value === "rejected") return "rejected";
    if (value === "unknown") return "neutral";
    return "expected";
  }

  function applyTagSelectColor(selectEl) {
    if (!selectEl) return;
    selectEl.classList.remove(
      "tag-select-main-correct",
      "tag-select-accepted",
      "tag-select-wrong-answer",
      "tag-select-time-limit-exceeded",
      "tag-select-run-time-error",
      "tag-select-rejected",
      "tag-select-expected",
      "tag-select-neutral"
    );
    selectEl.classList.add("tag-select-" + tagSelectState(selectEl.value));
  }

  function initTagSelects() {
    document.querySelectorAll("select.tag-select").forEach(function (selectEl) {
      applyTagSelectColor(selectEl);
      selectEl.addEventListener("change", function () {
        applyTagSelectColor(selectEl);
        if (selectEl.dataset.submitOnChange === "1") {
          var form = selectEl.closest("form");
          if (form) form.submit();
        }
      });
    });
  }

  function initConfirmForms() {
    document.querySelectorAll("form[data-confirm-message]").forEach(function (form) {
      form.addEventListener("submit", function (ev) {
        var msg = String(form.dataset.confirmMessage || "Are you sure?").trim();
        if (!window.confirm(msg)) {
          ev.preventDefault();
        }
      });
    });
  }

  function initSubmitLinks() {
    document.querySelectorAll("a[data-submit-form='1']").forEach(function (link) {
      link.addEventListener("click", function (ev) {
        ev.preventDefault();
        if (link.getAttribute("aria-disabled") === "true") {
          return;
        }
        var form = link.closest("form");
        if (!form) return;
        if (typeof form.requestSubmit === "function") {
          form.requestSubmit();
        } else {
          form.submit();
        }
      });
    });
  }

  function initSolutionEditorAsyncSave() {
    var form = document.getElementById("solution-save-form");
    if (!form) return;

    var submitBtn = document.getElementById("solution-save-submit");
    var errorBox = document.getElementById("solution-save-error");
    var baseLabel = submitBtn ? submitBtn.textContent : "Save Source";

    function syncEditors() {
      form.querySelectorAll("textarea[data-code-editor='1']").forEach(function (ta) {
        var cmWrap = ta.nextElementSibling;
        if (!cmWrap || !cmWrap.CodeMirror || typeof cmWrap.CodeMirror.save !== "function") {
          return;
        }
        cmWrap.CodeMirror.save();
      });
    }

    function showError(text) {
      if (!errorBox) {
        window.alert(String(text || "save failed"));
        return;
      }
      errorBox.textContent = String(text || "").trim() || "save failed";
      errorBox.hidden = false;
    }

    form.addEventListener("submit", async function (ev) {
      ev.preventDefault();
      if (submitBtn && submitBtn.disabled) return;
      if (errorBox) {
        errorBox.hidden = true;
        errorBox.textContent = "";
      }
      syncEditors();
      setSubmitting(submitBtn, baseLabel, "Saving...", true);
      try {
        var resp = await fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          headers: {
            "X-Requested-With": "fetch",
            Accept: "application/json",
          },
          credentials: "same-origin",
        });
        var payload = {};
        try {
          payload = await resp.json();
        } catch (_err) {
          payload = {};
        }
        if (resp.ok && payload && payload.ok && payload.redirect) {
          window.location.assign(String(payload.redirect));
          return;
        }
        showError((payload && (payload.error || payload.message)) || "save failed");
      } catch (_err) {
        showError("save failed: network error");
      }
      setSubmitting(submitBtn, baseLabel, "Saving...", false);
    });
  }

  function initLoginProofForm() {
    var form = document.getElementById("login-form");
    if (!form) return;
    form.addEventListener("submit", function (ev) {
      if (form.dataset.passwordPrepared === "1") return;
      ev.preventDefault();
      if (!requireWebCrypto()) return;

      (async function () {
        var usernameEl = form.querySelector("input[name='username']");
        var passwordEl = form.querySelector("input[name='password']");
        var csrfEl = form.querySelector("input[name='csrf_token']");
        var proofEl = form.querySelector("input[name='password_proof']");
        if (!usernameEl || !passwordEl || !csrfEl || !proofEl) {
          form.dataset.passwordPrepared = "1";
          form.submit();
          return;
        }

        var username = String(usernameEl.value || "").trim();
        var password = String(passwordEl.value || "");
        var csrfToken = String(csrfEl.value || "").trim();
        if (!password || !csrfToken) {
          form.dataset.passwordPrepared = "1";
          form.submit();
          return;
        }

        var qs = new URLSearchParams();
        qs.set("username", username);
        qs.set("csrf_token", csrfToken);
        var resp = await fetch("/auth/password-meta?" + qs.toString(), { credentials: "same-origin" });
        if (!resp.ok) {
          window.alert("Failed to prepare password proof.");
          return;
        }

        var meta = await resp.json();
        var salt = String(meta.salt || "").trim().toLowerCase();
        var iters = Number(meta.iters || 0);
        if (!/^[0-9a-f]{32}$/.test(salt) || !Number.isFinite(iters) || iters <= 0) {
          window.alert("Invalid password metadata.");
          return;
        }

        var verifier = await pbkdf2Hex(password, salt, Math.floor(iters));
        proofEl.value = await sha256Hex(csrfToken + verifier);
        passwordEl.value = await sha256Hex(csrfToken + password);
        form.dataset.passwordPrepared = "1";
        form.submit();
      })().catch(function () {
        window.alert("Failed to prepare password proof.");
      });
    });
  }

  function initRegisterLikeProofForm(formId) {
    var form = document.getElementById(formId);
    if (!form) return;

    form.addEventListener("submit", function (ev) {
      if (form.dataset.passwordPrepared === "1") return;
      ev.preventDefault();
      if (!requireWebCrypto()) return;

      (async function () {
        var passwordEl = form.querySelector("input[name='password']");
        var confirmEl = form.querySelector("input[name='password_confirm']");
        var csrfEl = form.querySelector("input[name='csrf_token']");
        var saltEl = form.querySelector("input[name='password_salt']");
        var itersEl = form.querySelector("input[name='password_iters']");
        var verifierEl = form.querySelector("input[name='password_verifier']");
        var proofEl = form.querySelector("input[name='password_proof']");
        if (!passwordEl || !confirmEl || !csrfEl || !saltEl || !itersEl || !verifierEl || !proofEl) {
          form.dataset.passwordPrepared = "1";
          form.submit();
          return;
        }

        var password = String(passwordEl.value || "");
        var confirm = String(confirmEl.value || "");
        var csrfToken = String(csrfEl.value || "").trim();
        var salt = String(saltEl.value || "").trim().toLowerCase();
        var iters = Number(itersEl.value || 0);

        if (password !== confirm) {
          window.alert("Password confirmation does not match.");
          return;
        }
        if (!password || !csrfToken) {
          form.dataset.passwordPrepared = "1";
          form.submit();
          return;
        }
        if (!/^[0-9a-f]{32}$/.test(salt) || !Number.isFinite(iters) || iters <= 0) {
          window.alert("Invalid password metadata.");
          return;
        }

        var verifier = await pbkdf2Hex(password, salt, Math.floor(iters));
        verifierEl.value = verifier;
        proofEl.value = await sha256Hex(csrfToken + verifier);
        passwordEl.value = await sha256Hex(csrfToken + password);
        confirmEl.value = await sha256Hex(csrfToken + confirm);
        form.dataset.passwordPrepared = "1";
        form.submit();
      })().catch(function () {
        window.alert("Failed to prepare password proof.");
      });
    });
  }

  function initSettingsPasswordProofForm() {
    var form = document.getElementById("settings-password-form");
    if (!form) return;

    form.addEventListener("submit", function (ev) {
      if (form.dataset.passwordPrepared === "1") return;
      ev.preventDefault();
      if (!requireWebCrypto()) return;

      (async function () {
        var currentEl = form.querySelector("input[name='current_password']");
        var nextEl = form.querySelector("input[name='new_password']");
        var confirmEl = form.querySelector("input[name='new_password_confirm']");
        var csrfEl = form.querySelector("input[name='csrf_token']");
        var currentSaltEl = form.querySelector("input[name='current_password_salt']");
        var currentItersEl = form.querySelector("input[name='current_password_iters']");
        var currentProofEl = form.querySelector("input[name='current_password_proof']");
        var newSaltEl = form.querySelector("input[name='new_password_salt']");
        var newItersEl = form.querySelector("input[name='new_password_iters']");
        var newVerifierEl = form.querySelector("input[name='new_password_verifier']");
        var newProofEl = form.querySelector("input[name='new_password_proof']");

        if (!currentEl || !nextEl || !confirmEl || !csrfEl || !currentSaltEl || !currentItersEl || !currentProofEl || !newSaltEl || !newItersEl || !newVerifierEl || !newProofEl) {
          form.dataset.passwordPrepared = "1";
          form.submit();
          return;
        }

        var currentPassword = String(currentEl.value || "");
        var nextPassword = String(nextEl.value || "");
        var confirmPassword = String(confirmEl.value || "");
        var csrfToken = String(csrfEl.value || "").trim();
        var currentSalt = String(currentSaltEl.value || "").trim().toLowerCase();
        var currentIters = Number(currentItersEl.value || 0);
        var newSalt = String(newSaltEl.value || "").trim().toLowerCase();
        var newIters = Number(newItersEl.value || 0);

        if (nextPassword !== confirmPassword) {
          window.alert("Password confirmation does not match.");
          return;
        }
        if (!currentPassword || !nextPassword || !csrfToken) {
          form.dataset.passwordPrepared = "1";
          form.submit();
          return;
        }
        if (!/^[0-9a-f]{32}$/.test(currentSalt) || !/^[0-9a-f]{32}$/.test(newSalt)) {
          window.alert("Invalid password metadata.");
          return;
        }
        if (!Number.isFinite(currentIters) || currentIters <= 0 || !Number.isFinite(newIters) || newIters <= 0) {
          window.alert("Invalid password metadata.");
          return;
        }

        var currentVerifier = await pbkdf2Hex(currentPassword, currentSalt, Math.floor(currentIters));
        var nextVerifier = await pbkdf2Hex(nextPassword, newSalt, Math.floor(newIters));
        currentProofEl.value = await sha256Hex(csrfToken + currentVerifier);
        newVerifierEl.value = nextVerifier;
        newProofEl.value = await sha256Hex(csrfToken + nextVerifier);
        currentEl.value = await sha256Hex(csrfToken + currentPassword);
        nextEl.value = await sha256Hex(csrfToken + nextPassword);
        confirmEl.value = await sha256Hex(csrfToken + confirmPassword);
        form.dataset.passwordPrepared = "1";
        form.submit();
      })().catch(function () {
        window.alert("Failed to prepare password proof.");
      });
    });
  }

  onReady(function () {
    initNavActiveState();
    initFlashAutohide();
    initDataTooltips();
    initNetworkEstimateProfile();
    initRunDetailsToggle();
    initRunExecuteSelectors();
    initTagSelects();
    initConfirmForms();
    initSubmitLinks();
    initSolutionEditorAsyncSave();
    initLoginProofForm();
    initRegisterLikeProofForm("register-form");
    initRegisterLikeProofForm("setup-form");
    initSettingsPasswordProofForm();
  });
})();
