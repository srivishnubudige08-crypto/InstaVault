const selected = new Set();

const grid = document.getElementById("grid");
const countEl = document.getElementById("count");
const downloadBtn = document.getElementById("download");
const loadBtn = document.getElementById("load");
const loginForm = document.getElementById("login-form");
const loginError = document.getElementById("login-error");

function refreshCount() {
  countEl.textContent = selected.size ? `${selected.size} selected` : "";
  downloadBtn.disabled = selected.size === 0;
}

function renderItems(items) {
  grid.innerHTML = "";
  for (const item of items) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <img src="${item.thumbnail_url}" alt="" loading="lazy">
      ${item.is_video ? '<span class="badge">video</span>' : ""}
      <div class="meta">@${item.owner}</div>
    `;
    card.addEventListener("click", () => {
      if (selected.has(item.shortcode)) {
        selected.delete(item.shortcode);
        card.classList.remove("selected");
      } else {
        selected.add(item.shortcode);
        card.classList.add("selected");
      }
      refreshCount();
    });
    grid.appendChild(card);
  }
}

loginForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginError.hidden = true;
  const data = Object.fromEntries(new FormData(loginForm));

  const res = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  const body = await res.json();

  if (!res.ok) {
    loginError.textContent = body.error || "Sign in failed.";
    loginError.hidden = false;
    return;
  }

  document.getElementById("login").hidden = true;
  document.getElementById("library").hidden = false;
  document.getElementById("status").textContent = `Signed in as ${body.username}`;
});

loadBtn?.addEventListener("click", async () => {
  loadBtn.disabled = true;
  loadBtn.textContent = "Loading…";
  try {
    const res = await fetch("/api/saved?limit=24");
    const body = await res.json();
    if (res.ok) renderItems(body);
    else grid.innerHTML = `<p class="error">${body.error}</p>`;
  } finally {
    loadBtn.disabled = false;
    loadBtn.textContent = "Load saved items";
  }
});

downloadBtn?.addEventListener("click", async () => {
  downloadBtn.disabled = true;
  downloadBtn.textContent = "Downloading…";
  try {
    const res = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ shortcodes: [...selected] }),
    });
    const results = await res.json();
    const ok = results.filter((r) => r.ok).length;
    countEl.textContent = `${ok} of ${results.length} downloaded`;
  } finally {
    downloadBtn.textContent = "Download selected";
    refreshCount();
  }
});
