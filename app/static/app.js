// Resubmit-to-Bing buttons and the change-log form. Both call the JSON API and report inline.
async function postJSON(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  let data = {};
  try { data = await r.json(); } catch (e) { /* IAP or proxy HTML error page */ }
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

document.querySelectorAll("button.resubmit").forEach((btn) => {
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "Submitting…";
    try {
      await postJSON("/api/resubmit", { url: btn.dataset.url });
      btn.textContent = "Submitted ✓";
      btn.classList.add("done");
    } catch (e) {
      btn.textContent = "Failed";
      btn.title = e.message;
      btn.classList.add("err");
      btn.disabled = false;
    }
  });
});

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
