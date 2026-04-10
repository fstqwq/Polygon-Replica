(function () {
  "use strict";

  var TARGET_SELECTOR = "textarea[data-code-editor='1']";
  var targets = Array.prototype.slice.call(document.querySelectorAll(TARGET_SELECTOR));
  if (!targets.length) return;

  var LOCAL_BASE = "/static/vendor/codemirror";
  var ASSET_VERSION = "20260409-1";

  function assetUrl(path) {
    return path + "?v=" + ASSET_VERSION;
  }

  var CORE_CSS = assetUrl(LOCAL_BASE + "/lib/codemirror.min.css");
  var CORE_JS = assetUrl(LOCAL_BASE + "/lib/codemirror.min.js");
  var ADDON_JS = [
    assetUrl(LOCAL_BASE + "/addon/edit/matchbrackets.min.js"),
    assetUrl(LOCAL_BASE + "/addon/edit/closebrackets.min.js"),
  ];
  var MODE_JS = [
    assetUrl(LOCAL_BASE + "/mode/clike/clike.min.js"),
    assetUrl(LOCAL_BASE + "/mode/python/python.min.js"),
    assetUrl(LOCAL_BASE + "/mode/stex/stex.min.js"),
    assetUrl(LOCAL_BASE + "/mode/javascript/javascript.min.js"),
  ];

  function readWrapEnabled(el) {
    if (!el) return false;
    var raw = String(el.getAttribute("data-code-wrap") || "").trim().toLowerCase();
    if (!raw) return false;
    return raw === "1" || raw === "true" || raw === "yes" || raw === "on";
  }

  function pathToMode(path) {
    var raw = String(path || "").trim().toLowerCase();
    if (!raw) return null;
    if (raw.endsWith(".cpp") || raw.endsWith(".cc") || raw.endsWith(".cxx") || raw.endsWith(".hpp") || raw.endsWith(".hh") || raw.endsWith(".h")) {
      return "text/x-c++src";
    }
    if (raw.endsWith(".c")) {
      return "text/x-csrc";
    }
    if (raw.endsWith(".java")) {
      return "text/x-java";
    }
    if (raw.endsWith(".py")) {
      return "python";
    }
    if (raw.endsWith(".tex") || raw.endsWith(".sty") || raw.endsWith(".cls")) {
      return "stex";
    }
    if (raw.endsWith(".json")) {
      return { name: "javascript", json: true };
    }
    return null;
  }

  function loadStylesheet(url) {
    return new Promise(function (resolve, reject) {
      var existing = document.querySelector("link[data-editor-asset='" + url + "']");
      if (!existing) {
        existing = document.querySelector("link[rel='stylesheet'][href$='" + url + "']");
      }
      if (existing) {
        resolve();
        return;
      }
      var link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = url;
      link.dataset.editorAsset = url;
      link.onload = function () { resolve(); };
      link.onerror = function () { reject(new Error("failed to load css: " + url)); };
      document.head.appendChild(link);
    });
  }

  function loadScript(url) {
    return new Promise(function (resolve, reject) {
      var existing = document.querySelector("script[data-editor-asset='" + url + "']");
      if (!existing) {
        existing = document.querySelector("script[src$='" + url + "']");
      }
      if (existing) {
        if (existing.dataset.loaded === "1") {
          resolve();
          return;
        }
        if (url === CORE_JS && window.CodeMirror) {
          resolve();
          return;
        }
        existing.addEventListener("load", function () { resolve(); }, { once: true });
        existing.addEventListener("error", function () { reject(new Error("failed to load js: " + url)); }, { once: true });
        return;
      }
      var script = document.createElement("script");
      script.src = url;
      script.defer = true;
      script.dataset.editorAsset = url;
      script.onload = function () {
        script.dataset.loaded = "1";
        resolve();
      };
      script.onerror = function () {
        reject(new Error("failed to load js: " + url));
      };
      document.head.appendChild(script);
    });
  }

  function initEditors() {
    if (!window.CodeMirror) return;
    for (var i = 0; i < targets.length; i += 1) {
      var el = targets[i];
      if (!el || el.dataset.editorReady === "1") continue;
      var mode = pathToMode(el.getAttribute("data-code-path"));
      var wrapEnabled = readWrapEnabled(el);
      var cm = window.CodeMirror.fromTextArea(el, {
        mode: mode,
        lineNumbers: true,
        lineWrapping: wrapEnabled,
        tabSize: 4,
        indentUnit: 4,
        matchBrackets: true,
        autoCloseBrackets: true,
        readOnly: el.hasAttribute("readonly"),
      });
      var heightRaw = Number(el.getAttribute("data-code-height") || 0);
      if (Number.isFinite(heightRaw) && heightRaw > 0) {
        cm.setSize(null, Math.floor(heightRaw));
      }
      attachSaveHook(cm, el.form);
      el.dataset.editorReady = "1";
    }
  }

  function attachSaveHook(cm, form) {
    if (!cm || !form) return;
    form.addEventListener("submit", function () {
      cm.save();
    });
  }

  if (window.CodeMirror) {
    initEditors();
    return;
  }

  loadStylesheet(CORE_CSS)
    .then(function () { return loadScript(CORE_JS); })
    .then(function () {
      var scripts = ADDON_JS.concat(MODE_JS);
      return Promise.all(scripts.map(loadScript));
    })
    .then(function () {
      initEditors();
    })
    .catch(function () {});
})();
