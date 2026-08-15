const { onReady, showInlineError } = window.PolygonUI;

function generateToken() {
  if (!window.crypto || typeof window.crypto.getRandomValues !== "function") throw new Error("Secure random is unavailable.");
  const bytes = new Uint8Array(24);
  window.crypto.getRandomValues(bytes);
  return window.btoa(Array.from(bytes, (byte) => String.fromCharCode(byte)).join(""))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

function initTokenGenerators() {
  document.querySelectorAll("button[data-token-generate='1']").forEach((button) => {
    button.addEventListener("click", () => {
      const target = document.getElementById(String(button.dataset.tokenTarget || ""));
      if (!target) return;
      try {
        target.value = generateToken();
      } catch (error) {
        showInlineError(null, error.message);
        return;
      }
      target.dispatchEvent(new Event("input", { bubbles: true }));
      target.dispatchEvent(new Event("change", { bubbles: true }));
      target.focus();
      target.select();
    });
  });
}

function initJudgehostConfiguration() {
  const enabled = document.querySelector("[data-judgehost-enable-toggle='1']");
  const auth = document.querySelector("[data-judgehost-auth-block='1']");
  if (enabled && auth) {
    const sync = () => {
      auth.hidden = !enabled.checked;
      auth.setAttribute("aria-hidden", enabled.checked ? "false" : "true");
    };
    sync();
    enabled.addEventListener("change", sync);
  }

  const cpuIds = document.querySelector("[data-gen-script-cpuids='1']");
  const runUidBase = document.querySelector("[data-gen-script-run-uid-base='1']");
  const baseUrl = document.querySelector("[data-gen-script-baseurl='1']");
  const sudo = document.querySelector("[data-gen-script-sudo='1']");
  const output = document.querySelector("[data-gen-script-output='1']");
  if (!cpuIds || !baseUrl || !output) return;
  const username = document.querySelector("[data-judgehost-api-username='1']");
  const token = document.querySelector("[data-judgehost-api-token='1']");

  const safeText = (value, fallback) => {
    const text = String(value || "").trim();
    return !text || text === "-" || text === "***" ? fallback : text;
  };
  const parseIds = () => {
    const seen = new Set();
    const ids = String(cpuIds.value || "").split(/[\s,;|]+/).map(Number).filter((value) => {
      const id = Math.floor(value);
      if (!Number.isFinite(value) || id < 1 || id > 1024 || seen.has(id)) return false;
      seen.add(id);
      return true;
    }).map(Math.floor);
    return ids.length ? ids : [2, 4, 6, 8];
  };
  const render = () => {
    const ids = parseIds();
    const parsedBase = Math.floor(Number(runUidBase && runUidBase.value));
    const uidBase = parsedBase >= 1 && parsedBase <= 65533 ? parsedBase : 60706;
    let endpoint = safeText(baseUrl.value, "http://host.docker.internal:8001/");
    if (!endpoint.endsWith("/")) endpoint += "/";
    const largestRunUidGid = uidBase + Math.max(...ids);
    if (largestRunUidGid > 65533) {
      output.value = "RUN_USER_UID_GID base is too large for the selected daemon IDs.";
      return;
    }
    const lines = [];
    if (uidBase === 60706 && largestRunUidGid > 61183) lines.push("# WARNING: generated IDs leave systemd's default unused 60706-61183 range.");
    ids.forEach((daemonId) => {
      const runUserUidGid = uidBase + daemonId;
      lines.push(
        `${sudo && sudo.checked ? "sudo " : ""}docker run -d --privileged --cgroupns=host --storage-opt size=10G ` +
        `-v /sys/fs/cgroup:/sys/fs/cgroup:rw --add-host=host.docker.internal:host-gateway ` +
        `--add-host=judgedaemon-${daemonId}:127.0.1.1 ` +
        `--name judgehost-${daemonId} --hostname judgedaemon-${daemonId} -e DAEMON_ID=${daemonId}` +
        " -e RUN_USER_UID_GID=" + runUserUidGid +
        ` -e CONTAINER_TIMEZONE=Asia/Shanghai -e DOMSERVER_BASEURL=${endpoint}` +
        ` -e JUDGEDAEMON_USERNAME=${safeText(username && username.value, "judgehost")}` +
        ` -e JUDGEDAEMON_PASSWORD=${safeText(token && token.value, "REPLACE_WITH_JUDGEHOST_API_TOKEN")}` +
        " domjudge/judgehost:latest",
      );
    });
    output.value = lines.join("\n");
  };
  [cpuIds, runUidBase, baseUrl, sudo, username, token].filter(Boolean).forEach((control) => {
    control.addEventListener("input", render);
    control.addEventListener("change", render);
  });
  document.querySelector("[data-popup-open='judgehost-gen-script-popup']")?.addEventListener("click", () => window.setTimeout(render, 0));
  render();
}

function initJudgehostFilter() {
  const input = document.querySelector("[data-judgehost-filter-input='1']");
  const table = document.querySelector("[data-judgehost-table='1']");
  if (!input || !table) return;
  let timer = 0;
  const apply = () => {
    const needle = input.value.trim().toLowerCase();
    table.querySelectorAll("[data-judgehost-row='1']").forEach((row) => {
      const haystack = [row.dataset.judgehostHostname, row.dataset.judgehostStatus, row.dataset.judgehostEnabled].join(" ").toLowerCase();
      row.hidden = Boolean(needle) && !haystack.includes(needle);
    });
  };
  input.addEventListener("input", () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(apply, 120);
  });
  input.addEventListener("change", apply);
  apply();
}

function initJudgehostToggles() {
  document.querySelectorAll("form[data-judgehost-toggle-form='1']").forEach((form) => {
    const toggle = form.querySelector("[data-judgehost-toggle='1']");
    const action = form.querySelector("input[name='action']");
    if (!toggle || !action) return;
    toggle.addEventListener("change", () => {
      action.value = toggle.checked ? "enable" : "disable";
      form.dataset.confirmApproved = toggle.checked ? "1" : "0";
      form.requestSubmit();
    });
  });
}

function initMaintenanceToggle() {
  const form = document.querySelector("[data-maintenance-toggle-form='1']");
  if (!form) return;
  const toggle = form.querySelector("[data-maintenance-toggle='1']");
  const action = form.querySelector("[data-maintenance-action='1']");
  if (!toggle || !action) return;
  toggle.addEventListener("change", () => {
    const intendedAction = toggle.checked ? "drain" : "resume";
    action.value = intendedAction;
    if (intendedAction === "resume") form.dataset.confirmApproved = "1";
    toggle.disabled = true;
    form.requestSubmit();
  });
}

onReady(() => {
  initTokenGenerators();
  initJudgehostConfiguration();
  initJudgehostFilter();
  initJudgehostToggles();
  initMaintenanceToggle();
});
