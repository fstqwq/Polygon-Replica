const { onReady } = window.PolygonUI;

function packageFilename(input) {
  if (input.files && input.files[0] && input.files[0].name) return input.files[0].name;
  return String(input.value || "").split(/[/\\]/).pop() || "";
}

function slugify(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64)
    .replace(/-+$/g, "");
}

function initImportSlug() {
  const form = document.getElementById("polygon-import-form");
  const slug = document.getElementById("polygon-import-slug");
  const file = document.getElementById("polygon-import-package");
  const hint = document.getElementById("polygon-import-slug-hint");
  const hintUrl = form && String(form.dataset.slugHintUrl || "").trim();
  if (!form || !slug || !file || !hint || !hintUrl) return;
  let sequence = 0;
  let userEdited = Boolean(slug.value.trim());

  const show = (message, level = "muted") => {
    hint.textContent = message;
    hint.classList.remove("muted", "ok", "danger");
    hint.classList.add(level);
  };
  const fallback = (autofill) => {
    const filename = packageFilename(file);
    const stem = filename.replace(/\.[^.]*$/, "").replace(/-\d+\$linux$/i, "");
    const suggestion = slugify(stem) || "imported-problem";
    if (autofill && !userEdited && !slug.value.trim()) slug.value = suggestion;
    const valid = !slug.value || /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(slug.value);
    slug.setCustomValidity(valid ? "" : "Use lowercase words separated by dashes.");
    show(valid ? `Suggested slug: ${suggestion}` : "Slug format is invalid.", valid ? "muted" : "danger");
  };
  const refresh = async (autofill) => {
    const current = slug.value.trim();
    const filename = packageFilename(file);
    if (!current && !filename) {
      show("Selecting a package will suggest an available slug.");
      return;
    }
    const request = ++sequence;
    const query = new URLSearchParams();
    if (filename) query.set("filename", filename);
    if (current) query.set("requested_slug", current);
    try {
      const response = await fetch(`${hintUrl}${hintUrl.includes("?") ? "&" : "?"}${query}`, {
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch", Accept: "application/json" },
      });
      if (!response.ok) throw new Error("hint failed");
      const payload = await response.json();
      if (request !== sequence) return;
      const suggested = String(payload.suggested || "");
      if (!current && suggested && autofill && !userEdited) slug.value = suggested;
      if (!payload.valid || (current && payload.exists)) {
        const message = String(payload.message || (payload.exists ? "Problem already exists." : "Invalid problem id."));
        slug.setCustomValidity(message);
        show(suggested && suggested !== current ? `${message} Suggested: ${suggested}.` : message, "danger");
        return;
      }
      slug.setCustomValidity("");
      show(current ? "Slug is available." : `Suggested slug: ${suggested}`, current ? "ok" : "muted");
    } catch (_error) {
      if (request === sequence) fallback(autofill);
    }
  };
  file.addEventListener("change", () => {
    if (!slug.value.trim()) userEdited = false;
    refresh(true);
  });
  slug.addEventListener("input", () => {
    userEdited = Boolean(slug.value.trim());
    refresh(false);
  });
  form.addEventListener("submit", (event) => {
    if (!slug.value.trim()) fallback(true);
    if (!slug.checkValidity()) {
      event.preventDefault();
      slug.reportValidity();
    }
  });
  refresh(true);
}

onReady(initImportSlug);
