// Register filters/sorting and the change-log form (posts to the JSON API and reports inline).
async function postJSON(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  let data = {};
  try { data = await r.json(); } catch (e) { /* IAP or proxy HTML error page */ }
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

// All-pages register: filter by URL text, Google/Bing status, language, gaps; click headers to sort.
const reg = document.getElementById("register");
if (reg) {
  const rows = Array.from(reg.tBodies[0].rows);
  const $ = (id) => document.getElementById(id);
  const apply = () => {
    const q = $("f-q").value.trim().toLowerCase();
    const g = $("f-g").value, b = $("f-b").value, l = $("f-l").value, gap = $("f-gap").checked, x = $("f-x").value;
    let n = 0;
    rows.forEach((r) => {
      const d = r.dataset;
      const show = (!q || d.path.includes(q)) && (!g || d.g === g) && (!b || d.b === b) &&
                   (!l || d.l === l) && (!gap || d.gap === "1") &&
                   (!x || (x === "in" ? d.x === "0" : d.x === "1"));
      r.hidden = !show;
      if (show) n++;
    });
    $("f-count").textContent = `${n} of ${rows.length}`;
  };
  ["f-q", "f-g", "f-b", "f-l", "f-x", "f-gap"].forEach((id) => $(id).addEventListener("input", apply));
  let sortKey = null, asc = true;
  reg.querySelectorAll("th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const k = th.dataset.sort;
      asc = sortKey === k ? !asc : true;
      sortKey = k;
      const val = (r) => k === "days" ? Number(r.dataset.days) : k === "google" ? r.dataset.g : k === "bing" ? r.dataset.b : r.dataset[k] || "";
      rows.sort((a, c) => (val(a) > val(c) ? 1 : val(a) < val(c) ? -1 : 0) * (asc ? 1 : -1));
      rows.forEach((r) => reg.tBodies[0].appendChild(r));
      reg.querySelectorAll("th").forEach((h) => h.classList.remove("asc", "desc"));
      th.classList.add(asc ? "asc" : "desc");
    });
  });
  apply();
}

const form = document.getElementById("logfix");
if (form) {
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(form);
    const b = form.querySelector("button");
    b.disabled = true;
    try {
      await postJSON("/api/changes", { url: f.get("url"), type: f.get("type"), note: f.get("note") });
      location.reload();
    } catch (e) {
      b.textContent = "Failed: " + e.message;
      b.disabled = false;
    }
  });
}

// Index coverage: manual GSC request ("Requested"), and the exclusion list (add / remove).
const fail = (b, e) => { b.textContent = "Failed: " + e.message; b.classList.add("err"); b.disabled = false; };
document.querySelectorAll("button.req").forEach((b) => b.addEventListener("click", async () => {
  b.disabled = true;
  try {
    await postJSON("/api/request-indexing", { url: b.dataset.url });
    b.textContent = "Requested ✓";
    b.classList.add("done");
  } catch (e) { fail(b, e); }
}));
document.querySelectorAll("button.excl-rm").forEach((b) => b.addEventListener("click", async () => {
  if (!confirm(`Remove ${b.dataset.pattern} from the exclusion list? Matching pages count again from the next rules run.`)) return;
  b.disabled = true;
  try { await postJSON("/api/exclusions/remove", { pattern: b.dataset.pattern }); location.reload(); } catch (e) { fail(b, e); }
}));
const exAdd = document.getElementById("excl-add");
if (exAdd) {
  exAdd.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(exAdd);
    const b = exAdd.querySelector("button");
    b.disabled = true;
    try {
      await postJSON("/api/exclusions", { pattern: f.get("pattern"), all_locales: f.get("all_locales") === "on", note: f.get("note") });
      location.reload();
    } catch (e) { fail(b, e); }
  });
}
