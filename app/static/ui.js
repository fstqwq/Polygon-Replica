(function () {
  "use strict";
  var cachedFlashMessageText = "";

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

  function generateStrongToken32() {
    if (!window.crypto || typeof window.crypto.getRandomValues !== "function" || typeof window.btoa !== "function") {
      throw new Error("secure random unavailable");
    }
    var bytes = new Uint8Array(24);
    window.crypto.getRandomValues(bytes);
    var binary = "";
    for (var i = 0; i < bytes.length; i += 1) {
      binary += String.fromCharCode(bytes[i]);
    }
    var token = window
      .btoa(binary)
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/g, "");
    if (token.length !== 32) {
      throw new Error("invalid token length");
    }
    return token;
  }

  function initSettingsTokenGenerators() {
    var buttons = document.querySelectorAll("button[data-token-generate='1']");
    if (!buttons.length) return;
    buttons.forEach(function (button) {
      button.addEventListener("click", function () {
        var targetId = String(button.getAttribute("data-token-target") || "").trim();
        if (!targetId) return;
        var target = document.getElementById(targetId);
        if (!target) return;
        try {
          target.value = generateStrongToken32();
        } catch (_err) {
          window.alert("WebCrypto secure random is required for token generation.");
          return;
        }
        target.dispatchEvent(
          new Event("input", {
            bubbles: true,
          })
        );
        target.dispatchEvent(
          new Event("change", {
            bubbles: true,
          })
        );
        if (typeof target.focus === "function") target.focus();
        if (typeof target.select === "function") target.select();
      });
    });
  }

  function initSettingsJudgehostRunnerControls() {
    var judgehostToggle = document.querySelector("[data-judgehost-enable-toggle='1']");
    var judgehostAuthBlock = document.querySelector("[data-judgehost-auth-block='1']");

    function syncJudgehostAuthVisibility() {
      if (!judgehostToggle || !judgehostAuthBlock) return;
      var enabled = !!judgehostToggle.checked;
      judgehostAuthBlock.hidden = !enabled;
      judgehostAuthBlock.setAttribute("aria-hidden", enabled ? "false" : "true");
    }

    if (judgehostToggle && judgehostAuthBlock) {
      syncJudgehostAuthVisibility();
      judgehostToggle.addEventListener("change", syncJudgehostAuthVisibility);
    }

    var cpuidInput = document.querySelector("[data-gen-script-cpuids='1']");
    var baseurlInput = document.querySelector("[data-gen-script-baseurl='1']");
    var sudoInput = document.querySelector("[data-gen-script-sudo='1']");
    var output = document.querySelector("[data-gen-script-output='1']");
    if (!cpuidInput || !baseurlInput || !output) return;

    var usernameInput = document.querySelector("[data-judgehost-api-username='1']");
    var tokenInput = document.querySelector("[data-judgehost-api-token='1']");
    var popupOpener = document.querySelector("[data-popup-open='judgehost-gen-script-popup']");

    function safeText(raw, fallback) {
      var text = String(raw || "").trim();
      if (!text || text === "-" || text === "***") return String(fallback || "").trim();
      return text;
    }

    function normalizeBaseurl(raw) {
      var text = safeText(raw, "http://host.docker.internal:8001/");
      if (!text) {
        return "http://host.docker.internal:8001/";
      }
      if (!/\/$/.test(text)) {
        text += "/";
      }
      return text;
    }

    function parseCpuIds(raw) {
      var text = String(raw || "").trim();
      var fallback = [2, 4, 6, 8];
      if (!text) return fallback;
      var tokens = text.split(/[\s,;|]+/);
      var ids = [];
      var seen = {};
      tokens.forEach(function (token) {
        var part = String(token || "").trim();
        if (!part) return;
        var num = Number(part);
        if (!Number.isFinite(num)) return;
        var safe = Math.floor(num);
        if (safe < 1 || safe > 1024) return;
        var key = String(safe);
        if (seen[key]) return;
        seen[key] = true;
        ids.push(safe);
      });
      return ids.length ? ids : fallback;
    }

    function renderJudgehostGenScript() {
      var cpuIds = parseCpuIds(cpuidInput.value);
      var baseurl = normalizeBaseurl(baseurlInput.value);
      var username = safeText(usernameInput ? usernameInput.value : "", "judgehost");
      var password = safeText(tokenInput ? tokenInput.value : "", "REPLACE_WITH_JUDGEHOST_API_TOKEN");
      var commandPrefix = sudoInput && sudoInput.checked ? "sudo " : "";
      var lines = [];
      cpuIds.forEach(function (daemonId) {
        lines.push(
          commandPrefix +
            "docker run -d --privileged --cgroupns=host --storage-opt size=10G -v /sys/fs/cgroup:/sys/fs/cgroup:rw --add-host=host.docker.internal:host-gateway --name judgehost-" +
            String(daemonId) +
            " --hostname judgedaemon-" +
            String(daemonId) +
            " -e DAEMON_ID=" +
            String(daemonId) +
            " -e CONTAINER_TIMEZONE=Asia/Shanghai -e DOMSERVER_BASEURL=" +
            baseurl +
            " -e JUDGEDAEMON_USERNAME=" +
            username +
            " -e JUDGEDAEMON_PASSWORD=" +
            password +
            " domjudge/judgehost:latest"
        );
      });
      output.value = lines.join("\n");
    }

    cpuidInput.addEventListener("input", renderJudgehostGenScript);
    cpuidInput.addEventListener("change", renderJudgehostGenScript);
    baseurlInput.addEventListener("input", renderJudgehostGenScript);
    baseurlInput.addEventListener("change", renderJudgehostGenScript);
    if (sudoInput) {
      sudoInput.addEventListener("change", renderJudgehostGenScript);
    }
    if (usernameInput) {
      usernameInput.addEventListener("input", renderJudgehostGenScript);
      usernameInput.addEventListener("change", renderJudgehostGenScript);
    }
    if (tokenInput) {
      tokenInput.addEventListener("input", renderJudgehostGenScript);
      tokenInput.addEventListener("change", renderJudgehostGenScript);
    }
    if (popupOpener) {
      popupOpener.addEventListener("click", function () {
        window.setTimeout(renderJudgehostGenScript, 0);
      });
    }
    renderJudgehostGenScript();
  }

  function initSettingsJudgehostTableFilter() {
    var filterInput = document.querySelector("[data-judgehost-filter-input='1']");
    var table = document.querySelector("[data-judgehost-table='1']");
    if (!filterInput || !table) return;

    var debounceTimer = 0;

    function applyFilter() {
      var needle = String(filterInput.value || "").trim().toLowerCase();
      var rows = table.querySelectorAll("[data-judgehost-row='1']");
      rows.forEach(function (row) {
        var hostname = String(row.getAttribute("data-judgehost-hostname") || "").toLowerCase();
        var status = String(row.getAttribute("data-judgehost-status") || "").toLowerCase();
        var enabled = String(row.getAttribute("data-judgehost-enabled") || "").toLowerCase();
        var text = [hostname, status, enabled].join(" ");
        row.hidden = !!needle && text.indexOf(needle) < 0;
      });
    }

    function scheduleApplyFilter() {
      if (debounceTimer) {
        window.clearTimeout(debounceTimer);
      }
      debounceTimer = window.setTimeout(function () {
        debounceTimer = 0;
        applyFilter();
      }, 120);
    }

    filterInput.addEventListener("input", scheduleApplyFilter);
    filterInput.addEventListener("change", applyFilter);
    applyFilter();
  }

  function initSettingsJudgehostToggles() {
    var forms = document.querySelectorAll("form[data-judgehost-toggle-form='1']");
    if (!forms.length) return;
    forms.forEach(function (form) {
      var toggle = form.querySelector("input[data-judgehost-toggle='1']");
      var action = form.querySelector("input[name='action']");
      if (!toggle || !action) return;

      toggle.addEventListener("change", function () {
        var enabling = !!toggle.checked;
        action.value = enabling ? "enable" : "disable";
        // Skip confirmation on enable; keep confirmation on disable.
        form.dataset.confirmApproved = enabling ? "1" : "0";
        if (typeof form.requestSubmit === "function") {
          form.requestSubmit();
        } else {
          form.submit();
        }
      });
    });
  }

  function setSubmitting(button, baseLabel, loadingLabel, loading) {
    if (!button) return;
    button.disabled = !!loading;
    button.textContent = loading ? loadingLabel : baseLabel;
  }

  function submitForm(form) {
    if (!form) return;
    if (typeof form.requestSubmit === "function") {
      form.requestSubmit();
      return;
    }
    form.submit();
  }

  function isLocalStorageUsable(probeKey) {
    try {
      if (!window.localStorage) return false;
      window.localStorage.setItem(probeKey, "1");
      window.localStorage.removeItem(probeKey);
      return true;
    } catch (_err) {
      return false;
    }
  }

  function storageScopeToken(raw) {
    return String(raw || "")
      .trim()
      .replace(/[:|]/g, "_");
  }

  function initNavActiveState() {
    var pageLinks = document.querySelectorAll("a[data-page]");
    if (!pageLinks.length) return;

    var parts = window.location.pathname
      .split("/")
      .map(function (item) {
        return String(item || "").trim();
      })
      .filter(function (item) {
        return item.length > 0;
      });
    var page = "statement";
    var rawPageToken = "";
    var qp = new URLSearchParams(window.location.search);

    var allowed = {
      statement: 1,
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

    if (window.location.pathname.indexOf("/problems/") === 0) {
      for (var idx = parts.length - 1; idx >= 0; idx -= 1) {
        var token = parts[idx];
        var prev = idx > 0 ? parts[idx - 1] : "";
        if ((token === "details" || token === "test-fragment") && prev === "run") {
          rawPageToken = "run";
          break;
        }
        if (token === "preview") {
          rawPageToken = "preview";
          break;
        }
        if (token === "git" || token === "history") {
          rawPageToken = "workspace";
          break;
        }
        if (token === "artifacts") {
          rawPageToken = "tests";
          break;
        }
        if (token === "runs") {
          rawPageToken = "run";
          break;
        }
        if (allowed[token]) {
          rawPageToken = token;
          break;
        }
      }
    }
    if (!rawPageToken) rawPageToken = "statement";
    page = rawPageToken;

    if (page === "artifacts") page = "tests";
    if (page === "runs") page = "run";
    if (page === "git" || page === "history") page = "workspace";
    if (page === "preview") page = "statement";

    if (page === "files") {
      var selectedPath = qp.get("path") || "";
      if (selectedPath.indexOf("checkers/") === 0) page = "checker";
      else if (selectedPath.indexOf("interactors/") === 0) page = "interactor";
      else if (selectedPath.indexOf("validators/") === 0) page = "validator";
      else if (selectedPath.indexOf("solutions/") === 0) page = "solutions";
      else if (selectedPath === "generators" || selectedPath.indexOf("generators/") === 0) page = "generators";
    }

    if (!allowed[page]) page = "statement";

    if (window.location.pathname.indexOf("/problems/") === 0) {
      document.querySelectorAll("a[data-main]").forEach(function (el) {
        if (el.getAttribute("data-main") === "problems") {
          el.classList.add("active");
        }
      });
    }
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
    var targetPage = allowed[page] ? page : "statement";
    if (rawPageToken === "preview") {
      targetPage = "preview";
    }
    document.querySelectorAll("input.page-target").forEach(function (el) {
      el.value = targetPage;
    });
  }

  function removeNode(node) {
    if (!node || !node.parentNode) return;
    node.parentNode.removeChild(node);
  }

  function readFlashMessageText() {
    if (cachedFlashMessageText) {
      return cachedFlashMessageText;
    }
    var payload = document.querySelector("[data-top-event='1']");
    var text = String(payload ? payload.textContent || "" : "").trim();
    if (text) {
      cachedFlashMessageText = text;
      return text;
    }
    var inline = document.querySelector(".flash-inline");
    text = String(inline ? inline.textContent || "" : "").trim();
    if (text) {
      cachedFlashMessageText = text;
    }
    return text;
  }

  function normalizeTopEventLevel(raw) {
    var level = String(raw || "").trim().toLowerCase();
    if (level === "success" || level === "warning" || level === "error") {
      return level;
    }
    return "info";
  }

  function normalizeTopEventText(raw) {
    var text = String(raw || "").trim();
    if (!text) return "";
    text = text.replace(/\bok\b/gi, "OK");
    if (/^[a-z]/.test(text)) {
      text = text.charAt(0).toUpperCase() + text.slice(1);
    }
    return text;
  }

  function loadTopEventSeenMap() {
    try {
      var raw = window.sessionStorage ? window.sessionStorage.getItem("polygonlike.top_event_seen.v1") : "";
      if (!raw) return {};
      var parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object") return {};
      return parsed;
    } catch (_err) {
      return {};
    }
  }

  function saveTopEventSeenMap(map) {
    try {
      if (!window.sessionStorage) return;
      var pairs = Object.keys(map || {}).map(function (eventId) {
        return {
          id: eventId,
          ts: Number((map && map[eventId]) || 0),
        };
      });
      pairs.sort(function (a, b) {
        return b.ts - a.ts;
      });
      var trimmed = {};
      pairs.slice(0, 128).forEach(function (item) {
        if (!item.id) return;
        trimmed[item.id] = Number.isFinite(item.ts) ? item.ts : Date.now();
      });
      window.sessionStorage.setItem("polygonlike.top_event_seen.v1", JSON.stringify(trimmed));
    } catch (_err) {
      // ignore storage write failures
    }
  }

  function initTopEventNotice() {
    var payloads = Array.prototype.slice.call(document.querySelectorAll("[data-top-event='1']"));
    if (!payloads.length) return;
    var host = document.getElementById("top-event-slot");
    if (!host) {
      payloads.forEach(removeNode);
      return;
    }

    var payload = payloads[0];
    var text = normalizeTopEventText(payload.textContent);
    if (!text) {
      payloads.forEach(removeNode);
      return;
    }
    cachedFlashMessageText = text;
    var eventId = String(payload.getAttribute("data-event-id") || "").trim();
    if (!eventId) {
      eventId = text.toLowerCase();
    }
    var seenMap = loadTopEventSeenMap();
    if (eventId && Object.prototype.hasOwnProperty.call(seenMap, eventId)) {
      payloads.forEach(removeNode);
      return;
    }
    if (eventId) {
      seenMap[eventId] = Date.now();
      saveTopEventSeenMap(seenMap);
    }

    var level = normalizeTopEventLevel(payload.getAttribute("data-level"));
    var dismissible = String(payload.getAttribute("data-dismissible") || "1").trim() !== "0";
    host.textContent = "";
    var notice = document.createElement("div");
    var toneClass = level === "success" ? "tone-ok" : level === "warning" ? "tone-warn" : level === "error" ? "tone-fail" : "tone-info";
    notice.className = "top-event-notice top-event-" + level + " " + toneClass;
    notice.setAttribute("role", level === "error" ? "alert" : "status");
    if (eventId) {
      notice.setAttribute("data-event-id", eventId);
    }

    var textEl = document.createElement("span");
    textEl.className = "top-event-text";
    textEl.textContent = text;
    notice.appendChild(textEl);

    if (dismissible) {
      var dismissBtn = document.createElement("button");
      dismissBtn.type = "button";
      dismissBtn.className = "linkish-button top-event-dismiss";
      dismissBtn.setAttribute("aria-label", "Dismiss notification");
      dismissBtn.textContent = "Dismiss";
      dismissBtn.addEventListener("click", function () {
        removeNode(notice);
      });
      notice.appendChild(dismissBtn);
    }

    host.appendChild(notice);
    payloads.forEach(removeNode);
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
    var networkSlot = document.getElementById("profile-network-estimate");
    if (!backendSlot && !networkSlot) return;

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
      if (networkSlot) networkSlot.textContent = formatMs(networkMs);
    }

    update();
    window.addEventListener("load", update);
  }

  function initRunDetailsToggle() {
    var table = document.querySelector(".verification-detail-table");
    if (!table) return;
    var toggles = table.querySelectorAll('[data-popup-open="run-test-detail-popup"][data-test-name]');
    if (!toggles.length) return;
    var popupTitle = document.getElementById("run-test-detail-popup-title");
    var popupContent = document.getElementById("run-test-detail-popup-content");
    if (!popupTitle || !popupContent) return;
      var detailFetchBase = String(table.getAttribute("data-run-details-fragment") || "").trim();
      var verificationId = String(table.getAttribute("data-verification-id") || "").trim();

      function detailUrlForTest(testName) {
        var safeTest = String(testName || "").trim();
        if (!detailFetchBase || !safeTest) return "";
        if (!verificationId) return "";
        var params = new URLSearchParams();
        params.set("test", safeTest);
        params.set("verification_id", verificationId);
        var query = params.toString();
        if (!query) return "";
        return detailFetchBase + (detailFetchBase.indexOf("?") >= 0 ? "&" : "?") + query;
      }

    function renderLoading(message) {
      popupContent.innerHTML = "";
      var tip = document.createElement("p");
      tip.className = "muted verification-detail-loading";
      tip.textContent = String(message || "Loading details...");
      popupContent.appendChild(tip);
    }

    var detailHtmlCache = Object.create(null);
    var detailLoading = Object.create(null);
    var activeTestName = "";
    var activeRunId = "";

    function renderFilteredHtml(testName, runId) {
      var safeTestName = String(testName || "").trim();
      var safeRunId = String(runId || "").trim();
      var fullHtml = String(detailHtmlCache[safeTestName] || "");
      if (!fullHtml) {
        renderLoading("No detail context.");
        return;
      }
      if (!safeRunId) {
        popupContent.innerHTML = fullHtml;
        return;
      }
      var wrapper = document.createElement("div");
      wrapper.innerHTML = fullHtml;
      var solutionList = wrapper.querySelector(".sol-list");
      if (!solutionList) {
        popupContent.innerHTML = fullHtml;
        return;
      }
      var cards = Array.prototype.slice.call(solutionList.querySelectorAll(".sol-card[data-run-id]"));
      var matchedCard = null;
      cards.forEach(function (card) {
        if (String(card.getAttribute("data-run-id") || "").trim() === safeRunId) {
          matchedCard = card;
        }
      });
      if (!matchedCard) {
        var first = cards.length ? cards[0] : null;
        if (first) {
          matchedCard = first;
        }
      }
      if (matchedCard) {
        cards.forEach(function (card) {
          if (card !== matchedCard && card.parentNode) {
            card.parentNode.removeChild(card);
          }
        });
      }
      popupContent.innerHTML = wrapper.innerHTML;
    }

    function ensureLoaded(testName, runId) {
      var safeTestName = String(testName || "").trim();
      if (!safeTestName) {
        renderLoading("No detail context.");
        return;
      }
      var safeRunId = String(runId || "").trim();
      if (detailHtmlCache[safeTestName]) {
        renderFilteredHtml(safeTestName, safeRunId);
        return;
      }
      if (detailLoading[safeTestName]) {
        renderLoading("Loading details...");
        return;
      }
      var detailUrl = detailUrlForTest(safeTestName);
      if (!detailUrl) {
        renderLoading("No detail context.");
        return;
      }
      detailLoading[safeTestName] = true;
      renderLoading("Loading details...");
      fetch(detailUrl, {
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      })
        .then(function (resp) {
          if (!resp.ok) throw new Error("detail fetch failed");
          return resp.text();
        })
        .then(function (html) {
          detailHtmlCache[safeTestName] = String(html || "");
          if (activeTestName === safeTestName) {
            renderFilteredHtml(safeTestName, activeRunId);
          }
        })
        .catch(function () {
          if (activeTestName === safeTestName) {
            renderLoading("Failed to load details.");
          }
        })
        .finally(function () {
          delete detailLoading[safeTestName];
        });
    }

    toggles.forEach(function (el) {
      el.addEventListener("click", function (ev) {
        ev.preventDefault();
        var testName = String(el.getAttribute("data-test-name") || "").trim();
        var runId = String(el.getAttribute("data-run-id") || "").trim();
        var solutionTitle = String(el.getAttribute("data-solution-title") || "").trim();
        activeTestName = testName;
        activeRunId = runId;
        if (testName && solutionTitle) {
          popupTitle.textContent = "Test " + testName + " - " + solutionTitle;
        } else if (testName) {
          popupTitle.textContent = "Test Details: " + testName;
        } else {
          popupTitle.textContent = "Test Details";
        }
        ensureLoaded(testName, runId);
      });
    });
  }

  function initLifecycleTabs() {
    var groups = document.querySelectorAll("[data-lifecycle-tabs='1']");
    if (!groups.length) return;

    groups.forEach(function (group) {
      var buttons = Array.prototype.slice.call(group.querySelectorAll("[data-lifecycle-tab-button]"));
      var panels = Array.prototype.slice.call(group.querySelectorAll("[data-lifecycle-tab-panel]"));
      if (!buttons.length || !panels.length) return;

      function activate(tabId, focusButton) {
        var token = String(tabId || "").trim();
        if (!token) return;
        buttons.forEach(function (button) {
          var active = String(button.getAttribute("data-lifecycle-tab-button") || "") === token;
          button.classList.toggle("is-active", active);
          button.setAttribute("aria-selected", active ? "true" : "false");
          button.setAttribute("tabindex", active ? "0" : "-1");
          if (active && focusButton) {
            try {
              button.focus({ preventScroll: true });
            } catch (_err) {
              button.focus();
            }
          }
        });
        panels.forEach(function (panel) {
          var active = String(panel.getAttribute("data-lifecycle-tab-panel") || "") === token;
          panel.classList.toggle("is-hidden", !active);
          panel.hidden = !active;
        });
      }

      var initial = buttons.find(function (button) {
        return button.classList.contains("is-active");
      });
      if (!initial) {
        initial = buttons[0];
      }
      if (initial) {
        activate(initial.getAttribute("data-lifecycle-tab-button"), false);
      }

      buttons.forEach(function (button) {
        button.addEventListener("click", function () {
          activate(button.getAttribute("data-lifecycle-tab-button"), false);
        });
      });

      group.addEventListener("keydown", function (ev) {
        var target = ev.target;
        if (!target || !target.hasAttribute("data-lifecycle-tab-button")) return;
        var key = String(ev.key || "");
        if (key !== "ArrowUp" && key !== "ArrowDown" && key !== "ArrowLeft" && key !== "ArrowRight" && key !== "Home" && key !== "End") {
          return;
        }
        ev.preventDefault();
        var currentIndex = buttons.indexOf(target);
        if (currentIndex < 0) return;
        var nextIndex = currentIndex;
        if (key === "Home") nextIndex = 0;
        else if (key === "End") nextIndex = buttons.length - 1;
        else if (key === "ArrowUp" || key === "ArrowLeft") nextIndex = (currentIndex - 1 + buttons.length) % buttons.length;
        else nextIndex = (currentIndex + 1) % buttons.length;
        var next = buttons[nextIndex];
        if (!next) return;
        activate(next.getAttribute("data-lifecycle-tab-button"), true);
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
        allBtn.addEventListener("click", function (ev) {
          ev.preventDefault();
          setChecked(true);
        });
      }
      if (clearBtn) {
        clearBtn.addEventListener("click", function (ev) {
          ev.preventDefault();
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

  function ensureConfirmDialogParts() {
    var overlay = document.getElementById("ui-confirm-overlay");
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.id = "ui-confirm-overlay";
      overlay.className = "ui-confirm-overlay";
      overlay.hidden = true;
      overlay.innerHTML =
        '<div class="ui-confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="ui-confirm-title">' +
        '<h3 id="ui-confirm-title">Please Confirm</h3>' +
        '<p id="ui-confirm-message" class="ui-confirm-message"></p>' +
        '<div class="ui-confirm-actions">' +
        '<button type="button" class="ui-confirm-cancel">Cancel</button>' +
        '<button type="button" class="ui-confirm-ok">Confirm</button>' +
        "</div>" +
        "</div>";
      document.body.appendChild(overlay);
    }
    return {
      overlay: overlay,
      message: overlay.querySelector("#ui-confirm-message"),
      cancelBtn: overlay.querySelector(".ui-confirm-cancel"),
      okBtn: overlay.querySelector(".ui-confirm-ok"),
    };
  }

  function initTestsSampleForms() {
    var forms = document.querySelectorAll("form[data-sample-form='1']");
    if (!forms.length) return;

    function syncSampleForm(form) {
      var toggle = form.querySelector("[data-sample-toggle='1']");
      var group = form.querySelector("[data-sample-output-validate-group='1']");
      if (!toggle || !group) {
        return;
      }
      var checkbox = group.querySelector("input[name='sample_output_validate']");
      var visible = !!toggle.checked;
      group.hidden = !visible;
      if (checkbox) {
        checkbox.disabled = !visible;
      }
    }

    forms.forEach(syncSampleForm);
    document.addEventListener("change", function (event) {
      var target = event.target;
      if (!(target instanceof HTMLInputElement)) {
        return;
      }
      if (target.getAttribute("data-sample-toggle") !== "1") {
        return;
      }
      var form = target.closest("form[data-sample-form='1']");
      if (form instanceof HTMLFormElement) {
        syncSampleForm(form);
      }
    });
  }

  function initTestsEditorAutoFocusNewest() {
    if (window.location.pathname.indexOf("/tests") < 0) return;
    var rows = document.querySelectorAll(".tests-editor-item");
    if (!rows.length) return;

    function focusRow(row, behavior) {
      if (!row) return;
      row.classList.add("tests-editor-item-new-focus");
      if (!row.hasAttribute("tabindex")) {
        row.setAttribute("tabindex", "-1");
      }
      try {
        row.focus({ preventScroll: true });
      } catch (_err) {
        row.focus();
      }
      row.scrollIntoView({ block: "center", behavior: behavior || "smooth" });
    }

    var query = new URLSearchParams(window.location.search);
    var focusRaw = String(query.get("focus") || "").trim();
    if (/^\d+$/.test(focusRaw)) {
      var focusIndex = Number(focusRaw);
      if (Number.isFinite(focusIndex) && focusIndex > 0) {
        var targeted = document.getElementById("test-row-" + String(focusIndex));
        if (targeted) {
          focusRow(targeted, "smooth");
          query.delete("focus");
          if (window.history && typeof window.history.replaceState === "function") {
            var cleanQuery = query.toString();
            var cleanUrl = window.location.pathname + (cleanQuery ? "?" + cleanQuery : "") + window.location.hash;
            window.history.replaceState(null, "", cleanUrl);
          }
          return;
        }
      }
    }

    var text = readFlashMessageText().toLowerCase();
    if (text.indexOf("test added") < 0 && text.indexOf("tests added") < 0) return;
    focusRow(rows[rows.length - 1], "smooth");
  }

  function showConfirmDialog(messageText) {
    var parts = ensureConfirmDialogParts();
    var overlay = parts.overlay;
    var messageEl = parts.message;
    var cancelBtn = parts.cancelBtn;
    var okBtn = parts.okBtn;

    return new Promise(function (resolve) {
      var settled = false;

      function cleanup() {
        overlay.hidden = true;
        document.body.classList.remove("confirm-open");
        overlay.removeEventListener("click", onOverlayClick);
        cancelBtn.removeEventListener("click", onCancel);
        okBtn.removeEventListener("click", onConfirm);
        document.removeEventListener("keydown", onKeydown, true);
      }

      function finish(result) {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(!!result);
      }

      function onOverlayClick(ev) {
        if (ev.target === overlay) {
          ev.preventDefault();
          finish(false);
        }
      }

      function onCancel(ev) {
        ev.preventDefault();
        finish(false);
      }

      function onConfirm(ev) {
        ev.preventDefault();
        finish(true);
      }

      function onKeydown(ev) {
        if (ev.key === "Escape") {
          ev.preventDefault();
          finish(false);
        }
      }

      messageEl.textContent = String(messageText || "Are you sure?").trim();
      overlay.hidden = false;
      document.body.classList.add("confirm-open");
      overlay.addEventListener("click", onOverlayClick);
      cancelBtn.addEventListener("click", onCancel);
      okBtn.addEventListener("click", onConfirm);
      document.addEventListener("keydown", onKeydown, true);
      window.setTimeout(function () {
        cancelBtn.focus();
      }, 0);
    });
  }

  function initPopupDialogs() {
    var openers = Array.prototype.slice.call(document.querySelectorAll("[data-popup-open]"));
    if (!openers.length) return;

    var activeOverlay = null;
    var activeTrigger = null;

    function isPopupOverlay(node) {
      return !!(node && node.classList && node.classList.contains("ui-popup-overlay"));
    }

    function getOverlayById(id) {
      var token = String(id || "").trim();
      if (!token) return null;
      var found = document.getElementById(token);
      return isPopupOverlay(found) ? found : null;
    }

    function firstFocusableIn(overlay) {
      if (!overlay) return null;
      var selector =
        ".ui-popup-content input:not([type='hidden']), .ui-popup-content select, .ui-popup-content textarea, .ui-popup-content button, .ui-popup-content a[href], .ui-popup-header button, .ui-popup-header a[href]";
      var candidate = overlay.querySelector(selector);
      if (candidate && typeof candidate.focus === "function") return candidate;
      var fallback = overlay.querySelector("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])");
      return fallback && typeof fallback.focus === "function" ? fallback : null;
    }

    function syncPopupBodyClass() {
      var opened = document.querySelector(".ui-popup-overlay[data-popup-overlay='1']:not([hidden])");
      if (opened) {
        document.body.classList.add("popup-open");
      } else {
        document.body.classList.remove("popup-open");
      }
    }

    function closeOverlay(overlay, restoreFocus) {
      if (!isPopupOverlay(overlay)) return;
      overlay.hidden = true;
      if (activeOverlay === overlay) {
        activeOverlay = null;
      }
      syncPopupBodyClass();
      if (restoreFocus && activeTrigger && typeof activeTrigger.focus === "function") {
        activeTrigger.focus();
      }
      if (restoreFocus) {
        activeTrigger = null;
      }
    }

    function openOverlay(overlay, trigger) {
      if (!isPopupOverlay(overlay)) return;
      if (activeOverlay && activeOverlay !== overlay) {
        closeOverlay(activeOverlay, false);
      }
      activeOverlay = overlay;
      activeTrigger = trigger || null;
      overlay.hidden = false;
      syncPopupBodyClass();
      window.setTimeout(function () {
        var target = firstFocusableIn(overlay);
        if (target) {
          target.focus();
        }
      }, 0);
    }

    openers.forEach(function (opener) {
      opener.addEventListener("click", function (ev) {
        ev.preventDefault();
        var overlay = getOverlayById(opener.getAttribute("data-popup-open"));
        if (!overlay) return;
        openOverlay(overlay, opener);
      });
    });

    document.querySelectorAll("[data-popup-close]").forEach(function (closer) {
      closer.addEventListener("click", function (ev) {
        ev.preventDefault();
        var overlay = closer.closest(".ui-popup-overlay");
        closeOverlay(overlay, true);
      });
    });

    document.querySelectorAll(".ui-popup-overlay[data-popup-overlay='1']").forEach(function (overlay) {
      overlay.addEventListener("click", function (ev) {
        if (ev.target === overlay) {
          ev.preventDefault();
          closeOverlay(overlay, true);
        }
      });
    });

    document.addEventListener(
      "keydown",
      function (ev) {
        if (ev.key !== "Escape") return;
        if (!activeOverlay || activeOverlay.hidden) return;
        ev.preventDefault();
        closeOverlay(activeOverlay, true);
      },
      true
    );
  }

  function initConfirmForms() {
    document.querySelectorAll("form[data-confirm-message]").forEach(function (form) {
      form.addEventListener("submit", function (ev) {
        if (form.dataset.confirmApproved === "1") {
          form.dataset.confirmApproved = "0";
          return;
        }
        ev.preventDefault();
        var msg = String(form.dataset.confirmMessage || "Are you sure?").trim();
        showConfirmDialog(msg).then(function (ok) {
          if (!ok) return;
          form.dataset.confirmApproved = "1";
          if (typeof form.requestSubmit === "function") {
            form.requestSubmit();
          } else {
            form.submit();
          }
        });
      });
    });
  }

  function initSudoPopupBridge() {
    var query = new URLSearchParams(window.location.search);
    if (query.get("sudo_popup_done") !== "1") return;
    if (!window.opener || window.opener.closed) return;
    try {
      window.opener.postMessage({ type: "polygonlike:sudo-enabled" }, window.location.origin);
    } catch (_err) {
      return;
    }
    window.setTimeout(function () {
      window.close();
    }, 0);
  }

  function initSudoGatedForms() {
    var forms = Array.prototype.slice.call(document.querySelectorAll("form[data-sudo-gated='1']"));
    if (!forms.length) return;

    function markSudoGranted() {
      forms.forEach(function (form) {
        form.dataset.sudoRequired = "0";
      });
    }

    window.addEventListener("message", function (ev) {
      if (!ev || ev.origin !== window.location.origin) return;
      if (!ev.data || ev.data.type !== "polygonlike:sudo-enabled") return;
      markSudoGranted();
    });

    forms.forEach(function (form) {
      form.addEventListener("submit", function (ev) {
        if (String(form.dataset.sudoRequired || "0") !== "1") return;
        var sudoUrl = String(form.dataset.sudoUrl || "").trim();
        if (!sudoUrl) return;
        ev.preventDefault();
        var popup = null;
        try {
          popup = window.open(
            sudoUrl,
            "polygonlike-sudo",
            "popup=yes,width=540,height=720,resizable=yes,scrollbars=yes"
          );
        } catch (_err) {
          popup = null;
        }
        if (popup && typeof popup.focus === "function") {
          popup.focus();
          return;
        }
        window.location.assign(sudoUrl);
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

  var suppressCodeEditorBeforeUnload = false;

  function suppressCodeEditorUnloadOnce() {
    suppressCodeEditorBeforeUnload = true;
  }

  function findCodeMirrorEditorForTextarea(textarea) {
    if (!textarea) return null;
    var prev = textarea.previousElementSibling;
    if (prev && prev.CodeMirror) return prev.CodeMirror;
    var next = textarea.nextElementSibling;
    if (next && next.CodeMirror) return next.CodeMirror;
    var parent = textarea.parentElement;
    if (!parent) return null;
    var wrappers = parent.querySelectorAll(".CodeMirror");
    if (wrappers.length !== 1) return null;
    return wrappers[0] && wrappers[0].CodeMirror ? wrappers[0].CodeMirror : null;
  }

  function syncCodeEditorsInForm(form) {
    if (!form) return;
    form.querySelectorAll("textarea[data-code-editor='1']").forEach(function (ta) {
      var cm = findCodeMirrorEditorForTextarea(ta);
      if (!cm || typeof cm.save !== "function") return;
      cm.save();
    });
  }

  function clearComponentEditorError(errorBox) {
    if (!errorBox) return;
    errorBox.hidden = true;
    errorBox.textContent = "";
  }

  function showComponentEditorError(errorBox, text, fallbackText) {
    var safeText = String(text || "").trim() || String(fallbackText || "save failed").trim() || "save failed";
    if (!errorBox) {
      window.alert(safeText);
      return;
    }
    errorBox.textContent = safeText;
    errorBox.hidden = false;
  }

  function codeEditorGuardState(form) {
    return form && form.__polygonCodeEditorGuard ? form.__polygonCodeEditorGuard : null;
  }

  function markCodeEditorFormDirty(form) {
    var state = codeEditorGuardState(form);
    if (!state || state.pending) return;
    state.dirty = true;
  }

  function markCodeEditorFormClean(form) {
    var state = codeEditorGuardState(form);
    if (!state) return;
    state.dirty = false;
    state.pending = false;
  }

  function setCodeEditorFormPending(form, pending) {
    var state = codeEditorGuardState(form);
    if (!state) return;
    state.pending = !!pending;
  }

  function initCodeEditorUnloadGuard() {
    var forms = Array.prototype.slice.call(document.querySelectorAll("form[data-code-editor-guard='1']"));
    if (!forms.length) return;

    function bindCodeMirrorDirtyTracking(form) {
      form.querySelectorAll("textarea[data-code-editor='1']").forEach(function (ta) {
        if (ta.dataset.codeEditorGuardBound === "1") return;
        var cm = findCodeMirrorEditorForTextarea(ta);
        if (!cm || typeof cm.on !== "function") return;
        ta.dataset.codeEditorGuardBound = "1";
        cm.on("change", function () {
          markCodeEditorFormDirty(form);
        });
      });
    }

    forms.forEach(function (form) {
      if (form.__polygonCodeEditorGuard) return;
      form.__polygonCodeEditorGuard = { dirty: false, pending: false };
      form.addEventListener("input", function (ev) {
        var target = ev.target;
        if (!target || !target.name) return;
        if (String(target.type || "").toLowerCase() === "hidden") return;
        markCodeEditorFormDirty(form);
      });
      form.addEventListener("change", function (ev) {
        var target = ev.target;
        if (!target || !target.name) return;
        if (String(target.type || "").toLowerCase() === "hidden") return;
        markCodeEditorFormDirty(form);
      });
      bindCodeMirrorDirtyTracking(form);
      [400, 1200, 2400].forEach(function (delayMs) {
        window.setTimeout(function () {
          bindCodeMirrorDirtyTracking(form);
        }, delayMs);
      });
    });

    window.addEventListener("beforeunload", function (event) {
      if (suppressCodeEditorBeforeUnload) return;
      var hasDirty = forms.some(function (form) {
        var state = codeEditorGuardState(form);
        return !!(state && state.dirty && !state.pending);
      });
      if (!hasDirty) return;
      if (event) {
        event.preventDefault();
        event.returnValue = "";
      }
      return "";
    });
  }

  function initComponentSourceEditorAsyncSave() {
    document.querySelectorAll("form[data-component-source-save-form='1']").forEach(function (form) {
      form.addEventListener("submit", async function (ev) {
        var submitter = ev.submitter || null;
        var submitterAction = String((submitter && submitter.getAttribute("formaction")) || "").trim();
        var submitterMethod = String((submitter && submitter.getAttribute("formmethod")) || "").trim().toUpperCase();
        if (submitterAction || (submitterMethod && submitterMethod !== "POST")) {
          return;
        }
        ev.preventDefault();
        var btn =
          (submitter && submitter.tagName === "BUTTON" ? submitter : null) ||
          form.querySelector("button[type='submit']");
        if (!btn || btn.disabled) return;
        var errorBox = form.querySelector("[data-component-editor-error='1']");
        var baseLabel = String(btn.textContent || "Save Source").trim() || "Save Source";
        clearComponentEditorError(errorBox);
        syncCodeEditorsInForm(form);
        setCodeEditorFormPending(form, true);
        setSubmitting(btn, baseLabel, "Saving...", true);
        try {
          var formData = new FormData(form);
          formData.set("response_mode", "json");
          var resp = await fetch(form.action, {
            method: "POST",
            body: formData,
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
            markCodeEditorFormClean(form);
            suppressCodeEditorUnloadOnce();
            window.location.assign(String(payload.redirect));
            return;
          }
          setCodeEditorFormPending(form, false);
          showComponentEditorError(errorBox, payload && (payload.error || payload.message), "save failed");
        } catch (_err) {
          setCodeEditorFormPending(form, false);
          showComponentEditorError(errorBox, "save failed: network error", "save failed");
        }
        setSubmitting(btn, baseLabel, "Saving...", false);
      });
    });
  }

  function initSolutionEditorAsyncSave() {
    var form = document.getElementById("solution-save-form");
    if (!form) return;

    var submitBtn = document.getElementById("solution-save-submit");
    var errorBox = document.getElementById("solution-save-error");
    var baseLabel = submitBtn ? submitBtn.textContent : "Save Source";

    function showError(text) {
      showComponentEditorError(errorBox, text, "save failed");
    }

    form.addEventListener("submit", async function (ev) {
      ev.preventDefault();
      if (submitBtn && submitBtn.disabled) return;
      clearComponentEditorError(errorBox);
      syncCodeEditorsInForm(form);
      setCodeEditorFormPending(form, true);
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
          markCodeEditorFormClean(form);
          suppressCodeEditorUnloadOnce();
          window.location.assign(String(payload.redirect));
          return;
        }
        setCodeEditorFormPending(form, false);
        showError((payload && (payload.error || payload.message)) || "save failed");
      } catch (_err) {
        setCodeEditorFormPending(form, false);
        showError("save failed: network error");
      }
      setSubmitting(submitBtn, baseLabel, "Saving...", false);
    });
  }

  function initPreviewCompileAsync() {
    document.querySelectorAll("form[data-preview-compile-form='1']").forEach(function (form) {
      form.addEventListener("submit", function () {
        var btn =
          form.querySelector("button[data-preview-compile-button='1']") ||
          form.querySelector("button[type='submit']");
        if (!btn || btn.disabled) return;
        var baseLabel = String(btn.textContent || "Compile Statement").trim() || "Compile Statement";
        setSubmitting(btn, baseLabel, "Compiling...", true);
      });
    });
  }

  function initLoginProofForm() {
    var form = document.getElementById("login-form");
    if (!form) return;
    form.addEventListener("submit", function (ev) {
      if (form.dataset.passwordPrepared === "1") return;
      if (form.dataset.passwordPending === "1") {
        ev.preventDefault();
        return;
      }
      ev.preventDefault();
      if (!requireWebCrypto()) return;
      form.dataset.passwordPending = "1";

      (async function () {
        var usernameEl = form.querySelector("input[name='username']");
        var passwordEl = form.querySelector("input[name='password']");
        var csrfEl = form.querySelector("input[name='csrf_token']");
        var proofEl = form.querySelector("input[name='password_proof']");
        if (!usernameEl || !passwordEl || !csrfEl || !proofEl) {
          form.dataset.passwordPrepared = "1";
          delete form.dataset.passwordPending;
          form.submit();
          return;
        }

        var username = String(usernameEl.value || "").trim();
        var password = String(passwordEl.value || "");
        var csrfToken = String(csrfEl.value || "").trim();
        if (!password || !csrfToken) {
          form.dataset.passwordPrepared = "1";
          delete form.dataset.passwordPending;
          form.submit();
          return;
        }

        var qs = new URLSearchParams();
        qs.set("username", username);
        qs.set("csrf_token", csrfToken);
        var resp = await fetch("/auth/password-meta?" + qs.toString(), { credentials: "same-origin" });
        if (!resp.ok) {
          throw new Error("password meta fetch failed");
        }

        var meta = await resp.json();
        var salt = String(meta.salt || "").trim().toLowerCase();
        var iters = Number(meta.iters || 0);
        if (!/^[0-9a-f]{32}$/.test(salt) || !Number.isFinite(iters) || iters <= 0) {
          window.alert("Invalid password metadata.");
          delete form.dataset.passwordPending;
          return;
        }

        var verifier = await pbkdf2Hex(password, salt, Math.floor(iters));
        proofEl.value = await sha256Hex(csrfToken + verifier);
        passwordEl.value = await sha256Hex(csrfToken + password);
        form.dataset.passwordPrepared = "1";
        delete form.dataset.passwordPending;
        form.submit();
      })().catch(function () {
        delete form.dataset.passwordPending;
        window.alert("Failed to prepare password proof.");
      });
    });
  }

  function initRegisterLikeProofForm(formId) {
    var form = document.getElementById(formId);
    if (!form) return;

    form.addEventListener("submit", function (ev) {
      if (form.dataset.passwordPrepared === "1") return;
      if (form.dataset.passwordPending === "1") {
        ev.preventDefault();
        return;
      }
      ev.preventDefault();
      if (!requireWebCrypto()) return;
      form.dataset.passwordPending = "1";

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
          delete form.dataset.passwordPending;
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
          delete form.dataset.passwordPending;
          return;
        }
        if (!password || !csrfToken) {
          form.dataset.passwordPrepared = "1";
          delete form.dataset.passwordPending;
          form.submit();
          return;
        }
        if (!/^[0-9a-f]{32}$/.test(salt) || !Number.isFinite(iters) || iters <= 0) {
          window.alert("Invalid password metadata.");
          delete form.dataset.passwordPending;
          return;
        }

        var verifier = await pbkdf2Hex(password, salt, Math.floor(iters));
        verifierEl.value = verifier;
        proofEl.value = await sha256Hex(csrfToken + verifier);
        passwordEl.value = await sha256Hex(csrfToken + password);
        confirmEl.value = await sha256Hex(csrfToken + confirm);
        form.dataset.passwordPrepared = "1";
        delete form.dataset.passwordPending;
        form.submit();
      })().catch(function () {
        delete form.dataset.passwordPending;
        window.alert("Failed to prepare password proof.");
      });
    });
  }

  function initSettingsPasswordProofForm() {
    var form = document.getElementById("settings-password-form");
    if (!form) return;

    form.addEventListener("submit", function (ev) {
      if (form.dataset.passwordPrepared === "1") return;
      if (form.dataset.passwordPending === "1") {
        ev.preventDefault();
        return;
      }
      ev.preventDefault();
      if (!requireWebCrypto()) return;
      form.dataset.passwordPending = "1";

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
          delete form.dataset.passwordPending;
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
          delete form.dataset.passwordPending;
          return;
        }
        if (!currentPassword || !nextPassword || !csrfToken) {
          form.dataset.passwordPrepared = "1";
          delete form.dataset.passwordPending;
          form.submit();
          return;
        }
        if (!/^[0-9a-f]{32}$/.test(currentSalt) || !/^[0-9a-f]{32}$/.test(newSalt)) {
          window.alert("Invalid password metadata.");
          delete form.dataset.passwordPending;
          return;
        }
        if (!Number.isFinite(currentIters) || currentIters <= 0 || !Number.isFinite(newIters) || newIters <= 0) {
          window.alert("Invalid password metadata.");
          delete form.dataset.passwordPending;
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
        delete form.dataset.passwordPending;
        form.submit();
      })().catch(function () {
        delete form.dataset.passwordPending;
        window.alert("Failed to prepare password proof.");
      });
    });
  }

  function initSudoProofForm() {
    var form = document.getElementById("sudo-form");
    if (!form) return;

    form.addEventListener("submit", function (ev) {
      if (form.dataset.passwordPrepared === "1") return;
      if (form.dataset.passwordPending === "1") {
        ev.preventDefault();
        return;
      }
      ev.preventDefault();
      if (!requireWebCrypto()) return;
      form.dataset.passwordPending = "1";

      (async function () {
        var passwordEl = form.querySelector("input[name='password']");
        var csrfEl = form.querySelector("input[name='csrf_token']");
        var saltEl = form.querySelector("input[name='password_salt']");
        var itersEl = form.querySelector("input[name='password_iters']");
        var proofEl = form.querySelector("input[name='password_proof']");
        if (!passwordEl || !csrfEl || !saltEl || !itersEl || !proofEl) {
          form.dataset.passwordPrepared = "1";
          delete form.dataset.passwordPending;
          form.submit();
          return;
        }

        var password = String(passwordEl.value || "");
        var csrfToken = String(csrfEl.value || "").trim();
        var salt = String(saltEl.value || "").trim().toLowerCase();
        var iters = Number(itersEl.value || 0);
        if (!password || !csrfToken) {
          form.dataset.passwordPrepared = "1";
          delete form.dataset.passwordPending;
          form.submit();
          return;
        }
        if (!/^[0-9a-f]{32}$/.test(salt) || !Number.isFinite(iters) || iters <= 0) {
          window.alert("Invalid password metadata.");
          delete form.dataset.passwordPending;
          return;
        }

        var verifier = await pbkdf2Hex(password, salt, Math.floor(iters));
        proofEl.value = await sha256Hex(csrfToken + verifier);
        passwordEl.value = await sha256Hex(csrfToken + password);
        form.dataset.passwordPrepared = "1";
        delete form.dataset.passwordPending;
        form.submit();
      })().catch(function () {
        delete form.dataset.passwordPending;
        window.alert("Failed to prepare password proof.");
      });
    });
  }

  function initStatementDraftBackup() {
    var form = document.querySelector("form[data-statement-draft-form='1']");
    if (!form) return;
    var triggers = Array.prototype.slice.call(document.querySelectorAll("[data-statement-draft-trigger='1']"));
    var popup = document.getElementById("statement-draft-popup");
    if (!triggers.length || !popup) return;

    var popupTitleEl = popup.querySelector("#statement-draft-popup-title");
    var statusEl = popup.querySelector("[data-statement-draft-status='1']");
    var errorEl = popup.querySelector("[data-statement-draft-error='1']");
    var emptyEl = popup.querySelector("[data-statement-draft-empty='1']");
    var listEl = popup.querySelector("[data-statement-draft-list='1']");
    if (!statusEl || !errorEl || !emptyEl || !listEl) return;

    var MAX_HISTORY = 20;
    var AUTO_SAVE_DEBOUNCE_MS = 5000;
    var debounceTimers = Object.create(null);
    var lastSerializedByField = Object.create(null);
    var baselineSerializedByField = Object.create(null);
    var dirtyByField = Object.create(null);
    var storageUnavailable = false;
    var activeFieldName = "";
    var activeFieldLabel = "";
    var fieldLabelByName = Object.create(null);
    var isFormSubmitting = false;

    function findCodeMirrorEditor(textarea) {
      if (!textarea) return null;
      var prev = textarea.previousElementSibling;
      if (prev && prev.CodeMirror) return prev.CodeMirror;
      var next = textarea.nextElementSibling;
      if (next && next.CodeMirror) return next.CodeMirror;
      var parent = textarea.parentElement;
      if (!parent) return null;
      var wrappers = parent.querySelectorAll(".CodeMirror");
      if (wrappers.length !== 1) return null;
      return wrappers[0] && wrappers[0].CodeMirror ? wrappers[0].CodeMirror : null;
    }

    function syncCodeEditor(textarea) {
      if (!textarea) return;
      var cm = findCodeMirrorEditor(textarea);
      if (!cm || typeof cm.save !== "function") return;
      cm.save();
    }

    function isLocalStorageAvailable() {
      try {
        if (!window.localStorage) return false;
        var probeKey = "__polygonlike_statement_draft_probe__";
        window.localStorage.setItem(probeKey, "1");
        window.localStorage.removeItem(probeKey);
        return true;
      } catch (_err) {
        return false;
      }
    }

    function scopeToken(raw) {
      return String(raw || "")
        .trim()
        .replace(/[|]/g, "_");
    }

    var draftStorageKeyPrefix =
      "polygonlike:statement-draft:v2:" +
      [
        scopeToken(form.getAttribute("data-draft-scope-problem") || ""),
        scopeToken(form.getAttribute("data-draft-scope-user") || ""),
        scopeToken(form.getAttribute("data-draft-scope-page") || ""),
        scopeToken(form.getAttribute("action") || ""),
        scopeToken(window.location.pathname || ""),
      ].join("|");

    function draftStorageKeyForField(fieldName) {
      return draftStorageKeyPrefix + "|field=" + scopeToken(fieldName || "");
    }

    function setStatus(text) {
      statusEl.textContent = String(text || "").trim();
    }

    function clearError() {
      errorEl.hidden = true;
      errorEl.textContent = "";
    }

    function setError(text) {
      var safe = String(text || "").trim();
      if (!safe) {
        clearError();
        return;
      }
      errorEl.textContent = safe;
      errorEl.hidden = false;
    }

    function updateTriggerLabel() {
      triggers.forEach(function (triggerEl) {
        triggerEl.textContent = "Draft";
      });
    }

    function updatePopupTitle(fieldLabel) {
      if (!popupTitleEl) return;
      var safeLabel = String(fieldLabel || "").trim();
      popupTitleEl.textContent = safeLabel ? safeLabel + " Draft History" : "Draft History";
    }

    function fieldDisplayName(name) {
      var key = String(name || "").trim();
      if (!key) return "";
      var mapping = {
        legend_tex: "Legend",
        input_tex: "Input",
        output_tex: "Output",
        notes_tex: "Notes",
        interaction_tex: "Interaction",
        tutorial_tex: "Tutorial",
      };
      if (mapping[key]) return mapping[key];
      var human = key.replace(/_tex$/i, "").replace(/_/g, " ").trim();
      if (!human) return key;
      return human.charAt(0).toUpperCase() + human.slice(1);
    }

    function resolveFieldNameFromTrigger(triggerEl) {
      if (!triggerEl) return "";
      var direct = String(triggerEl.getAttribute("data-draft-field") || "").trim();
      if (direct) return direct;
      var container = triggerEl.closest ? triggerEl.closest(".statement-editor-field") : null;
      if (!container) {
        var cursor = triggerEl.parentElement;
        while (cursor && cursor !== form) {
          if (cursor.classList && cursor.classList.contains("statement-editor-field")) {
            container = cursor;
            break;
          }
          cursor = cursor.parentElement;
        }
      }
      if (!container) return "";
      var field = container.querySelector("textarea[name]");
      if (!field) return "";
      return String(field.getAttribute("name") || "").trim();
    }

    function resolveFieldLabel(fieldName) {
      var safeFieldName = String(fieldName || "").trim();
      if (!safeFieldName) return "";
      if (fieldLabelByName[safeFieldName]) return fieldLabelByName[safeFieldName];
      return fieldDisplayName(safeFieldName) || safeFieldName;
    }

    function listFieldNames() {
      var out = [];
      var seen = Object.create(null);
      form.querySelectorAll("textarea[name]").forEach(function (field) {
        var name = String(field.getAttribute("name") || "").trim();
        if (!name || seen[name]) return;
        seen[name] = true;
        out.push(name);
      });
      return out;
    }

    function getFieldTextarea(fieldName) {
      var safeFieldName = String(fieldName || "").trim();
      if (!safeFieldName) return null;
      var selector = 'textarea[name="' + safeFieldName.replace(/"/g, '\\"') + '"]';
      return form.querySelector(selector);
    }

    function collectFieldValue(fieldName) {
      var textarea = getFieldTextarea(fieldName);
      if (!textarea) return null;
      syncCodeEditor(textarea);
      return String(textarea.value || "");
    }

    function snapshotDigest(value) {
      return String(value || "");
    }

    function readHistory(fieldName) {
      var safeFieldName = String(fieldName || "").trim();
      if (!safeFieldName || storageUnavailable) return [];
      try {
        var key = draftStorageKeyForField(safeFieldName);
        var raw = window.localStorage.getItem(key);
        if (!raw) return [];
        var parsed = JSON.parse(raw);
        if (!Array.isArray(parsed)) return [];
        var rows = [];
        parsed.forEach(function (item) {
          if (!item || typeof item !== "object") return;
          var id = String(item.id || "").trim();
          var savedAt = String(item.savedAt || "").trim();
          if (!id || !savedAt) return;
          if (typeof item.value === "string") {
            rows.push({
              id: id,
              savedAt: savedAt,
              value: item.value,
            });
            return;
          }
          var legacyFields = item.fields;
          if (!legacyFields || typeof legacyFields !== "object" || Array.isArray(legacyFields)) return;
          if (!Object.prototype.hasOwnProperty.call(legacyFields, safeFieldName)) return;
          var legacyValue = String(legacyFields[safeFieldName] || "");
          rows.push({
            id: id,
            savedAt: savedAt,
            value: legacyValue,
          });
        });
        return rows;
      } catch (_err) {
        return [];
      }
    }

    function writeHistory(fieldName, rows) {
      var safeFieldName = String(fieldName || "").trim();
      if (!safeFieldName || storageUnavailable) return false;
      try {
        window.localStorage.setItem(draftStorageKeyForField(safeFieldName), JSON.stringify(rows || []));
        clearError();
        return true;
      } catch (_err) {
        setError("Failed to write local draft history (browser storage unavailable or full).");
        return false;
      }
    }

    function formatSavedAt(raw) {
      var text = String(raw || "").trim();
      if (!text) return "unknown time";
      var dt = new Date(text);
      if (!Number.isFinite(dt.getTime())) return text;
      return dt.toLocaleString();
    }

    function setDraftFieldValue(name, value) {
      var selector = 'textarea[name="' + String(name || "").replace(/"/g, '\\"') + '"]';
      var textarea = form.querySelector(selector);
      if (!textarea) return;
      var safeValue = String(value || "");
      textarea.value = safeValue;
      var cm = findCodeMirrorEditor(textarea);
      if (!cm || typeof cm.setValue !== "function") return;
      cm.setValue(safeValue);
    }

    function renderPreviewContent(container, value) {
      var safeValue = String(value || "");
      if (!safeValue) {
        var empty = document.createElement("p");
        empty.className = "statement-draft-entry-preview";
        empty.textContent = "(empty draft)";
        container.appendChild(empty);
        return;
      }
      var body = document.createElement("pre");
      body.className = "statement-draft-entry-preview";
      body.textContent = safeValue;
      container.appendChild(body);
    }

    function renderHistory(fieldName, rows) {
      var safeFieldName = String(fieldName || "").trim();
      var label = resolveFieldLabel(safeFieldName);
      activeFieldName = safeFieldName;
      activeFieldLabel = label;
      updatePopupTitle(label);

      var history = Array.isArray(rows) ? rows : [];
      listEl.textContent = "";
      updateTriggerLabel();
      if (!safeFieldName) {
        emptyEl.hidden = false;
        setStatus("No draft field selected.");
        return;
      }
      if (!history.length) {
        emptyEl.hidden = false;
        setStatus("No local drafts for " + (label || safeFieldName) + ".");
        return;
      }
      emptyEl.hidden = true;
      setStatus("Local drafts for " + (label || safeFieldName) + ": " + String(history.length));

      history.forEach(function (item, index) {
        var entry = document.createElement("article");
        entry.className = "statement-draft-entry";

        var head = document.createElement("div");
        head.className = "statement-draft-entry-head";
        entry.appendChild(head);

        var title = document.createElement("p");
        title.className = "statement-draft-entry-title";
        title.textContent = "Draft " + String(index + 1);
        head.appendChild(title);

        var timestamp = document.createElement("span");
        timestamp.className = "statement-draft-entry-meta";
        timestamp.textContent = formatSavedAt(item.savedAt);
        head.appendChild(timestamp);

        renderPreviewContent(entry, item.value);

        var actions = document.createElement("div");
        actions.className = "statement-draft-entry-actions";
        entry.appendChild(actions);

        var restore = document.createElement("a");
        restore.href = "#";
        restore.className = "linkish";
        restore.textContent = "Restore";
        restore.addEventListener("click", function (ev) {
          ev.preventDefault();
          var timeLabel = formatSavedAt(item.savedAt);
          showConfirmDialog("Restore " + (label || safeFieldName) + " draft from " + timeLabel + "? Current editor content will be replaced.").then(function (ok) {
            if (!ok) return;
            setDraftFieldValue(safeFieldName, item.value);
            var currentValue = collectFieldValue(safeFieldName);
            lastSerializedByField[safeFieldName] = snapshotDigest(currentValue === null ? "" : currentValue);
            setStatus((label || safeFieldName) + " restored from " + timeLabel + ".");
          });
        });
        actions.appendChild(restore);

        listEl.appendChild(entry);
      });
    }

    function saveSnapshotForField(fieldName) {
      var safeFieldName = String(fieldName || "").trim();
      if (!safeFieldName) return false;
      if (storageUnavailable) return false;
      var currentFieldValue = collectFieldValue(safeFieldName);
      if (currentFieldValue === null) return false;
      var value = currentFieldValue;
      var digest = snapshotDigest(value);
      if (digest === lastSerializedByField[safeFieldName]) return false;

      var history = readHistory(safeFieldName);
      if (history.length) {
        var latestDigest = snapshotDigest(history[0].value || "");
        if (latestDigest === digest) {
          lastSerializedByField[safeFieldName] = digest;
          return false;
        }
      }

      var row = {
        id: "draft-" + String(Date.now()) + "-" + String(Math.floor(Math.random() * 1000000)),
        savedAt: new Date().toISOString(),
        value: value,
      };
      history.unshift(row);
      if (history.length > MAX_HISTORY) {
        history = history.slice(0, MAX_HISTORY);
      }
      if (!writeHistory(safeFieldName, history)) return false;
      lastSerializedByField[safeFieldName] = digest;
      if (activeFieldName === safeFieldName) {
        renderHistory(safeFieldName, history);
      }
      return true;
    }

    function saveAllSnapshots() {
      listFieldNames().forEach(function (fieldName) {
        saveSnapshotForField(fieldName);
      });
    }

    function updateDirtyState(fieldName) {
      var safeFieldName = String(fieldName || "").trim();
      if (!safeFieldName) return;
      var currentValue = collectFieldValue(safeFieldName);
      var currentDigest = snapshotDigest(currentValue === null ? "" : currentValue);
      var baselineDigest = snapshotDigest(baselineSerializedByField[safeFieldName] || "");
      dirtyByField[safeFieldName] = currentDigest !== baselineDigest;
    }

    function hasDirtyEditors() {
      var names = Object.keys(dirtyByField);
      for (var i = 0; i < names.length; i += 1) {
        if (dirtyByField[names[i]]) return true;
      }
      return false;
    }

    function resetDirtyToCurrentSnapshot() {
      listFieldNames().forEach(function (fieldName) {
        var currentValue = collectFieldValue(fieldName);
        var digest = snapshotDigest(currentValue === null ? "" : currentValue);
        baselineSerializedByField[fieldName] = digest;
        dirtyByField[fieldName] = false;
      });
    }

    function scheduleSnapshot(fieldName) {
      var safeFieldName = String(fieldName || "").trim();
      if (!safeFieldName) return;
      if (debounceTimers[safeFieldName]) {
        window.clearTimeout(debounceTimers[safeFieldName]);
      }
      debounceTimers[safeFieldName] = window.setTimeout(function () {
        debounceTimers[safeFieldName] = 0;
        saveSnapshotForField(safeFieldName);
      }, AUTO_SAVE_DEBOUNCE_MS);
    }

    function bindEditorListeners() {
      form.querySelectorAll("textarea[name]").forEach(function (field) {
        var fieldName = String(field.getAttribute("name") || "").trim();
        if (!fieldName) return;
        if (field.dataset.statementDraftNativeBound !== "1") {
          field.dataset.statementDraftNativeBound = "1";
          field.addEventListener("input", function () {
            scheduleSnapshot(fieldName);
            updateDirtyState(fieldName);
          });
          field.addEventListener("change", function () {
            scheduleSnapshot(fieldName);
            updateDirtyState(fieldName);
          });
        }
        var cm = findCodeMirrorEditor(field);
        if (!cm || typeof cm.on !== "function") return;
        if (field.dataset.statementDraftCodeMirrorBound === "1") return;
        field.dataset.statementDraftCodeMirrorBound = "1";
        cm.on("change", function () {
          scheduleSnapshot(fieldName);
          updateDirtyState(fieldName);
        });
      });
    }

    triggers.forEach(function (triggerEl) {
      var fieldName = resolveFieldNameFromTrigger(triggerEl);
      if (fieldName) {
        var fieldLabel = String(triggerEl.getAttribute("data-draft-label") || "").trim();
        fieldLabelByName[fieldName] = fieldLabel || fieldDisplayName(fieldName) || fieldName;
      }
      triggerEl.addEventListener("click", function () {
        var openFieldName = resolveFieldNameFromTrigger(triggerEl);
        renderHistory(openFieldName, readHistory(openFieldName));
      });
    });

    form.addEventListener("submit", function () {
      isFormSubmitting = true;
      saveAllSnapshots();
      resetDirtyToCurrentSnapshot();
    });

    window.addEventListener("beforeunload", function (event) {
      saveAllSnapshots();
      if (isFormSubmitting) return;
      if (!hasDirtyEditors()) return;
      if (event) {
        event.preventDefault();
        event.returnValue = "";
      }
      return "";
    });

    storageUnavailable = !isLocalStorageAvailable();
    if (storageUnavailable) {
      setError("Local draft backup is unavailable in this browser session.");
      setStatus("Local draft backup unavailable.");
      updateTriggerLabel();
      return;
    }

    bindEditorListeners();
    // Editor wrappers may attach after first paint; retry several times.
    [400, 1200, 2400, 4800].forEach(function (delayMs) {
      window.setTimeout(bindEditorListeners, delayMs);
    });

    listFieldNames().forEach(function (fieldName) {
      var currentValue = collectFieldValue(fieldName);
      var digest = snapshotDigest(currentValue === null ? "" : currentValue);
      lastSerializedByField[fieldName] = digest;
      baselineSerializedByField[fieldName] = digest;
      dirtyByField[fieldName] = false;
      if (!fieldLabelByName[fieldName]) {
        fieldLabelByName[fieldName] = fieldDisplayName(fieldName) || fieldName;
      }
    });

    var initialFieldName = "";
    if (triggers.length) {
      initialFieldName = resolveFieldNameFromTrigger(triggers[0]);
    }
    if (!initialFieldName) {
      var fieldNames = listFieldNames();
      if (fieldNames.length) initialFieldName = fieldNames[0];
    }
    renderHistory(initialFieldName, readHistory(initialFieldName));
    if (activeFieldName) {
      setStatus("Local drafts ready for " + (activeFieldLabel || activeFieldName) + ".");
    } else {
      setStatus("Local drafts ready.");
    }
  }

  function slugifyImportProblemId(raw) {
    var token = String(raw || "").trim().toLowerCase();
    if (!token) return "";
    token = token.replace(/[^a-z0-9]+/g, "-");
    token = token.replace(/-{2,}/g, "-").replace(/^-+|-+$/g, "");
    if (token.length > 64) {
      token = token.slice(0, 64).replace(/-+$/g, "");
    }
    return token;
  }

  function importPackageFilename(fileInput) {
    if (!fileInput) return "";
    if (fileInput.files && fileInput.files.length > 0 && fileInput.files[0] && fileInput.files[0].name) {
      return String(fileInput.files[0].name || "").trim();
    }
    var raw = String(fileInput.value || "").trim();
    if (!raw) return "";
    return raw.split(/[/\\]/).pop();
  }

  function initPolygonImportSlugSuggest() {
    var form = document.getElementById("polygon-import-form");
    if (!form) return;
    var slugInput = document.getElementById("polygon-import-slug");
    var fileInput = document.getElementById("polygon-import-package");
    var hint = document.getElementById("polygon-import-slug-hint");
    var hintUrl = String(form.getAttribute("data-slug-hint-url") || "").trim();
    if (!slugInput || !fileInput || !hint || !hintUrl) return;

    var requestSeq = 0;
    var userTouchedSlug = false;

    function setHint(text, level) {
      hint.textContent = String(text || "").trim();
      hint.classList.remove("muted");
      hint.classList.remove("ok");
      hint.classList.remove("danger");
      if (level === "ok") hint.classList.add("ok");
      else if (level === "danger") hint.classList.add("danger");
      else hint.classList.add("muted");
    }

    function localFallbackBase(filename) {
      var raw = String(filename || "").trim();
      if (!raw) return "imported-problem";
      var stem = raw.replace(/\.[^.]*$/, "");
      stem = stem.replace(/-\d+\$linux$/i, "");
      if (!stem) {
        stem = raw.replace(/\.[^.]*$/, "");
      }
      var slug = slugifyImportProblemId(stem);
      return slug || "imported-problem";
    }

    function applyLocalFallback(allowAutofill) {
      var filename = importPackageFilename(fileInput);
      var requested = String(slugInput.value || "").trim();
      if (requested) {
        if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(requested)) {
          slugInput.setCustomValidity("invalid problem id. Use lowercased words separated by dash.");
          setHint("Slug format is invalid.", "danger");
          return;
        }
        slugInput.setCustomValidity("");
        setHint("Slug availability will be checked on import.", "muted");
        return;
      }
      var suggested = localFallbackBase(filename);
      if (allowAutofill && !userTouchedSlug) {
        slugInput.value = suggested;
      }
      slugInput.setCustomValidity("");
      if (filename) {
        setHint("Suggested slug: " + suggested, "muted");
      } else {
        setHint("Slug can be customized. Selecting a package will auto-suggest an available slug.", "muted");
      }
    }

    async function refreshHint(allowAutofill) {
      var filename = importPackageFilename(fileInput);
      var requested = String(slugInput.value || "").trim();
      if (!filename && !requested) {
        slugInput.setCustomValidity("");
        setHint("Slug can be customized. Selecting a package will auto-suggest an available slug.", "muted");
        return;
      }

      var seq = ++requestSeq;
      try {
        var params = new URLSearchParams();
        if (filename) params.set("filename", filename);
        if (requested) params.set("requested_slug", requested);
        var url = hintUrl + (hintUrl.indexOf("?") >= 0 ? "&" : "?") + params.toString();
        var resp = await fetch(url, {
          credentials: "same-origin",
          headers: {
            "X-Requested-With": "fetch",
            Accept: "application/json",
          },
        });
        if (!resp.ok) {
          throw new Error("slug hint request failed");
        }
        var payload = await resp.json();
        if (seq !== requestSeq) return;

        var valid = !!(payload && payload.valid);
        var exists = !!(payload && payload.exists);
        var suggested = String((payload && payload.suggested) || "").trim();

        if (!requested && suggested && allowAutofill && !userTouchedSlug) {
          slugInput.value = suggested;
        }

        if (!valid) {
          var invalidMsg = String((payload && payload.message) || "invalid problem id");
          slugInput.setCustomValidity(invalidMsg);
          setHint(invalidMsg, "danger");
          return;
        }

        if (requested && exists) {
          var conflictMsg = String((payload && payload.message) || "problem already exists");
          slugInput.setCustomValidity(conflictMsg);
          if (suggested && suggested !== requested) {
            setHint(conflictMsg + ". Suggested available slug: " + suggested, "danger");
          } else {
            setHint(conflictMsg, "danger");
          }
          return;
        }

        slugInput.setCustomValidity("");
        if (requested) {
          setHint("Slug is available.", "ok");
          return;
        }
        if (suggested) {
          if (exists) {
            setHint("Suggested slug: " + suggested + " (auto-adjusted to avoid duplicates).", "muted");
          } else {
            setHint("Suggested slug: " + suggested, "muted");
          }
        } else {
          setHint("Slug can be customized. Selecting a package will auto-suggest an available slug.", "muted");
        }
      } catch (_err) {
        if (seq !== requestSeq) return;
        applyLocalFallback(allowAutofill);
      }
    }

    fileInput.addEventListener("change", function () {
      if (!String(slugInput.value || "").trim()) {
        userTouchedSlug = false;
      }
      refreshHint(true);
    });

    slugInput.addEventListener("input", function () {
      userTouchedSlug = String(slugInput.value || "").trim().length > 0;
      refreshHint(false);
    });

    form.addEventListener("submit", function (ev) {
      if (!String(slugInput.value || "").trim()) {
        userTouchedSlug = false;
        applyLocalFallback(true);
      }
      if (typeof slugInput.checkValidity === "function" && !slugInput.checkValidity()) {
        ev.preventDefault();
        if (typeof slugInput.reportValidity === "function") {
          slugInput.reportValidity();
        }
      }
    });

    refreshHint(true);
  }

  function initAutoSubmitSelects() {
    document.querySelectorAll("[data-auto-submit-select='1']").forEach(function (select) {
      select.addEventListener("change", function () {
        submitForm(select.form);
      });
    });
  }

  function initStatementLanguageSwitch() {
    var forms = document.querySelectorAll("form[data-statement-language-form='1']");
    if (!forms.length) return;
    var canUseLocalStorage = isLocalStorageUsable("__polygonlike_statement_language_probe__");
    var query = new URLSearchParams(window.location.search);
    var hasExplicitLanguage = query.has("language");

    function storedLanguageKey(form) {
      return (
        "statement-language:" +
        storageScopeToken(form.getAttribute("data-statement-language-problem")) +
        ":" +
        storageScopeToken(form.getAttribute("data-statement-language-user"))
      );
    }

    function readStoredLanguage(key) {
      if (!canUseLocalStorage || !key) return "";
      try {
        return String(window.localStorage.getItem(key) || "").trim();
      } catch (_err) {
        return "";
      }
    }

    function writeStoredLanguage(key, value) {
      var safeValue = String(value || "").trim();
      if (!canUseLocalStorage || !key || !safeValue) return;
      try {
        window.localStorage.setItem(key, safeValue);
      } catch (_err) {
        return;
      }
    }

    function optionExists(select, value) {
      var safeValue = String(value || "").trim();
      if (!safeValue) return false;
      return Array.from(select.options).some(function (option) {
        return String(option.value || "") === safeValue;
      });
    }

    forms.forEach(function (form) {
      var select = form.querySelector("select[data-statement-language-select='1']");
      if (!select) return;
      var key = storedLanguageKey(form);
      var currentValue = String(select.value || "").trim();
      if (hasExplicitLanguage && currentValue) {
        writeStoredLanguage(key, currentValue);
      }
      if (!hasExplicitLanguage) {
        var rememberedLanguage = readStoredLanguage(key);
        if (optionExists(select, rememberedLanguage) && rememberedLanguage !== currentValue) {
          select.value = rememberedLanguage;
          submitForm(form);
          return;
        }
      }
      select.addEventListener("change", function () {
        var nextValue = String(select.value || "").trim();
        if (nextValue) {
          writeStoredLanguage(key, nextValue);
        }
        submitForm(form);
      });
    });
  }

  onReady(function () {
    initSudoPopupBridge();
    initNavActiveState();
    initTopEventNotice();
    initDataTooltips();
    initNetworkEstimateProfile();
    initRunDetailsToggle();
    initLifecycleTabs();
    initRunExecuteSelectors();
    initTestsSampleForms();
    initTestsEditorAutoFocusNewest();
    initTagSelects();
    initPopupDialogs();
    initConfirmForms();
    initSudoGatedForms();
    initSubmitLinks();
    initPreviewCompileAsync();
    initComponentSourceEditorAsyncSave();
    initSolutionEditorAsyncSave();
    initCodeEditorUnloadGuard();
    initLoginProofForm();
    initRegisterLikeProofForm("register-form");
    initRegisterLikeProofForm("setup-form");
    initSettingsPasswordProofForm();
    initSudoProofForm();
    initStatementDraftBackup();
    initStatementLanguageSwitch();
    initAutoSubmitSelects();
    initPolygonImportSlugSuggest();
    initSettingsTokenGenerators();
    initSettingsJudgehostRunnerControls();
    initSettingsJudgehostTableFilter();
    initSettingsJudgehostToggles();
  });
})();
