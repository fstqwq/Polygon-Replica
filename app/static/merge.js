(function () {
  "use strict";

  function makeElement(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderCell(cell, operation, side) {
    var node = makeElement("div", "merge-diff-cell merge-diff-cell-" + operation + " merge-diff-cell-" + side);
    var number = makeElement("span", "merge-diff-line-number", cell && cell.line_number !== null ? String(cell.line_number) : "");
    var code = makeElement("code", "merge-diff-code");
    node.appendChild(number);
    node.appendChild(code);
    if (!cell) return node;
    (cell.segments || []).forEach(function (segment) {
      var span = makeElement("span", segment.changed ? "merge-diff-inline-change" : "", segment.text);
      code.appendChild(span);
    });
    if (cell.no_newline) {
      code.appendChild(makeElement("span", "merge-diff-no-newline", " No newline at end of file"));
    }
    return node;
  }

  function renderComparison(root, payload) {
    root.querySelector("[data-merge-empty]").hidden = true;
    root.querySelector("[data-merge-error]").hidden = true;
    var content = root.querySelector("[data-merge-content]");
    content.hidden = false;
    root.querySelector("[data-merge-kind]").textContent = payload.change_kind;
    root.querySelector("[data-merge-path]").textContent = payload.path;
    root.querySelector("[data-merge-message]").textContent = payload.message || "";
    root.querySelector("[data-merge-left-label]").textContent = payload.left.label;
    root.querySelector("[data-merge-right-label]").textContent = payload.right.label;
    [
      ["left", payload.left],
      ["right", payload.right]
    ].forEach(function (item) {
      var link = root.querySelector("[data-merge-" + item[0] + "-open]");
      link.hidden = !item[1].open_url;
      if (item[1].open_url) {
        link.href = item[1].open_url;
        link.textContent = "Download file";
      }
    });
    var rows = root.querySelector("[data-merge-rows]");
    rows.replaceChildren();
    if (!payload.rows || !payload.rows.length) {
      rows.appendChild(makeElement("p", "merge-diff-unavailable", payload.message || "No text difference is present."));
      return;
    }
    payload.rows.forEach(function (row) {
      var line = makeElement("div", "merge-diff-row merge-diff-row-" + row.operation);
      line.appendChild(renderCell(row.left, row.operation, "left"));
      line.appendChild(renderCell(row.right, row.operation, "right"));
      rows.appendChild(line);
    });
  }

  function loadComparison(comparison, url, controller) {
    comparison.setAttribute("aria-busy", "true");
    comparison.querySelector("[data-merge-empty]").hidden = false;
    comparison.querySelector("[data-merge-empty]").textContent = "Loading comparison...";
    comparison.querySelector("[data-merge-content]").hidden = true;
    comparison.querySelector("[data-merge-error]").hidden = true;
    return fetch(url, { credentials: "same-origin", signal: controller && controller.signal })
      .then(function (response) {
        return response.json().then(function (payload) {
          if (!response.ok) throw new Error(payload.error || "Unable to load this comparison.");
          return payload;
        });
      })
      .then(function (payload) {
        renderComparison(comparison, payload);
      })
      .catch(function (error) {
        if (error.name === "AbortError") return;
        comparison.querySelector("[data-merge-empty]").hidden = true;
        comparison.querySelector("[data-merge-content]").hidden = true;
        var errorNode = comparison.querySelector("[data-merge-error]");
        errorNode.textContent = error.message || "Unable to load this comparison.";
        errorNode.hidden = false;
      })
      .finally(function () {
        comparison.setAttribute("aria-busy", "false");
      });
  }

  function initializeExpandedReview(review) {
    Array.prototype.slice.call(review.querySelectorAll("[data-merge-comparison][data-compare-url]")).forEach(function (comparison) {
      loadComparison(comparison, comparison.dataset.compareUrl, null);
    });
  }

  function start() {
    document.querySelectorAll("[data-merge-review]").forEach(function (review) {
      initializeExpandedReview(review);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
