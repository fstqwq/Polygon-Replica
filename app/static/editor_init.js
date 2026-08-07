(function () {
  "use strict";

  var TARGET_SELECTOR = "textarea[data-code-editor='1']";
  var targets = Array.prototype.slice.call(document.querySelectorAll(TARGET_SELECTOR));
  if (!targets.length) return;

  var EDITOR_READY_EVENT = "polygonlike:code-editor-ready";
  var LOCAL_BASE = "/static/vendor/codemirror";
  var ASSET_VERSION = "20260410-23";

  function assetUrl(path) {
    return path + "?v=" + ASSET_VERSION;
  }

  var CORE_CSS = assetUrl(LOCAL_BASE + "/lib/codemirror.min.css");
  var CORE_JS = assetUrl(LOCAL_BASE + "/lib/codemirror.min.js");
  var ADDON_JS = [
    assetUrl(LOCAL_BASE + "/addon/edit/matchbrackets.min.js"),
    assetUrl(LOCAL_BASE + "/addon/edit/closebrackets.min.js"),
  ];

  function editorConfig(path) {
    var normalizedPath = String(path || "").trim().toLowerCase();
    if (/\.(cpp|cc|cxx|hpp|hh|h)$/.test(normalizedPath)) {
      return {
        mode: "text/x-c++src",
        modeScript: assetUrl(LOCAL_BASE + "/mode/clike/clike.min.js"),
      };
    }
    if (normalizedPath.endsWith(".c")) {
      return {
        mode: "text/x-csrc",
        modeScript: assetUrl(LOCAL_BASE + "/mode/clike/clike.min.js"),
      };
    }
    if (normalizedPath.endsWith(".java")) {
      return {
        mode: "text/x-java",
        modeScript: assetUrl(LOCAL_BASE + "/mode/clike/clike.min.js"),
      };
    }
    if (normalizedPath.endsWith(".py")) {
      return {
        mode: "python",
        modeScript: assetUrl(LOCAL_BASE + "/mode/python/python.min.js"),
      };
    }
    if (/\.(tex|sty|cls)$/.test(normalizedPath)) {
      return {
        mode: "stex",
        modeScript: assetUrl(LOCAL_BASE + "/mode/stex/stex.min.js"),
      };
    }
    if (normalizedPath.endsWith(".json")) {
      return {
        mode: { name: "javascript", json: true },
        modeScript: assetUrl(LOCAL_BASE + "/mode/javascript/javascript.min.js"),
      };
    }
    return { mode: null, modeScript: "" };
  }

  function loadStylesheet(url) {
    return new Promise(function (resolve, reject) {
      var link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = url;
      link.onload = resolve;
      link.onerror = function () {
        reject(new Error("failed to load css: " + url));
      };
      document.head.appendChild(link);
    });
  }

  function loadScript(url) {
    return new Promise(function (resolve, reject) {
      var script = document.createElement("script");
      script.src = url;
      script.onload = resolve;
      script.onerror = function () {
        reject(new Error("failed to load js: " + url));
      };
      document.head.appendChild(script);
    });
  }

  function editorDependencyScripts() {
    var scripts = ADDON_JS.slice();
    var seen = Object.create(null);
    scripts.forEach(function (url) {
      seen[url] = true;
    });
    targets.forEach(function (textarea) {
      var config = editorConfig(textarea.getAttribute("data-code-path"));
      if (!config.modeScript || seen[config.modeScript]) return;
      seen[config.modeScript] = true;
      scripts.push(config.modeScript);
    });
    return scripts;
  }

  function enableAutoSize(editor) {
    function resize() {
      editor.setSize(null, "auto");
      editor.refresh();
    }
    editor.on("changes", resize);
    window.requestAnimationFrame(resize);
  }

  function initEditors() {
    targets.forEach(function (textarea) {
      if (textarea.dataset.editorReady === "1") return;
      var config = editorConfig(textarea.getAttribute("data-code-path"));
      var autoSizeEnabled = textarea.getAttribute("data-code-autosize") === "1";
      var editor = window.CodeMirror.fromTextArea(textarea, {
        mode: config.mode,
        lineNumbers: true,
        lineWrapping: textarea.getAttribute("data-code-wrap") === "1",
        tabSize: 4,
        indentUnit: 4,
        matchBrackets: true,
        autoCloseBrackets: true,
        readOnly: textarea.hasAttribute("readonly"),
        viewportMargin: autoSizeEnabled ? Infinity : 10,
      });
      textarea.__polygonCodeMirror = editor;
      textarea.dataset.editorReady = "1";
      if (autoSizeEnabled) {
        enableAutoSize(editor);
      } else {
        var height = Number(textarea.getAttribute("data-code-height") || 0);
        if (Number.isFinite(height) && height > 0) {
          editor.setSize(null, Math.floor(height));
        }
      }
      document.dispatchEvent(
        new CustomEvent(EDITOR_READY_EVENT, {
          detail: { textarea: textarea },
        })
      );
    });
  }

  Promise.all([loadStylesheet(CORE_CSS), loadScript(CORE_JS)])
    .then(function () {
      return Promise.all(editorDependencyScripts().map(loadScript));
    })
    .then(initEditors)
    .catch(function () {});
})();
