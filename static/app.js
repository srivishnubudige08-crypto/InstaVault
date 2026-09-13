/* InstaVault dashboard */

const $ = (id) => document.getElementById(id);

const state = {
  auth: { authenticated: false, username: "", awaiting_two_factor: false },
  authMode: "session", // session | password — session works past checkpoints
  filters: {
    search: "", kind: "all", state: "all", collection: "",
    audio: "any", artist: "", owner: "", duration: "any",
    sort: "newest",
  },
  mode: "full", // full | audio — what a download fetches
  collectionNames: {},
  items: [],
  selected: new Set(),
  offset: 0,
  limit: 60,
  hasMore: false,
  total: 0,
  loading: false,
  job: null,
};

/* ── helpers ─────────────────────────────────────────────────────────────── */

const fmtBytes = (n) => {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
  const value = n / 1024 ** i;
  return `${value.toFixed(value >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
};

const fmtDuration = (seconds) => {
  if (seconds == null) return "";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
};

const clockTime = (iso) => {
  const d = iso ? new Date(iso) : new Date();
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
};

const escapeHtml = (str) =>
  String(str ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]),
  );

const debounce = (fn, ms) => {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let data = {};
  try {
    data = await res.json();
  } catch {
    /* empty body is fine */
  }
  if (!res.ok) {
    const err = new Error(data.error || `Request failed (${res.status})`);
    Object.assign(err, data, { status: res.status });
    throw err;
  }
  return data;
}

function withLoading(btn, fn) {
  // Wraps an async submit so the button shows a spinner and can't double-fire.
  return async (...args) => {
    if (btn.classList.contains("is-loading")) return;
    btn.classList.add("is-loading");
    btn.disabled = true;
    try {
      return await fn(...args);
    } finally {
      btn.classList.remove("is-loading");
      btn.disabled = false;
    }
  };
}

/* ── toasts ──────────────────────────────────────────────────────────────── */

function toast(title, body = "", kind = "info", ttl = 5000) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<strong>${escapeHtml(title)}</strong>${body ? `<span>${escapeHtml(body)}</span>` : ""}`;
  $("toasts").appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 250);
  }, ttl);
}

/* ── auth ────────────────────────────────────────────────────────────────── */

function showAuthError(message, hint = "") {
  $("auth-error-title").textContent = message;
  $("auth-error-hint").textContent = hint;
  $("auth-error").hidden = false;
}

function clearAuthError() {
  $("auth-error").hidden = true;
}

function renderAuth() {
  const { authenticated, username, awaiting_two_factor } = state.auth;

  $("auth-screen").hidden = authenticated;
  $("app").hidden = !authenticated;

  // A pending two-factor step outranks whichever sign-in mode was chosen.
  const mode = awaiting_two_factor ? "twofa" : state.authMode;
  $("login-form").hidden = mode !== "password";
  $("session-form").hidden = mode !== "session";
  $("twofa-form").hidden = mode !== "twofa";

  // Tabs hide during the two-factor step — you can't switch mode mid-flow.
  $("auth-tabs").hidden = awaiting_two_factor;
  $("tab-session").classList.toggle("active", mode === "session");
  $("tab-password").classList.toggle("active", mode === "password");

  if (mode === "twofa") $("twofa-code").focus();
  else if (mode === "session") $("session-id").focus();
  else if (mode === "password") $("login-username").focus();

  if (authenticated) {
    $("account-name").textContent = username;
    $("account-sub").textContent = "signed in";
    $("account-avatar").textContent = (username[0] || "?").toUpperCase();
  }
}

function switchAuthMode(mode) {
  state.authMode = mode;
  clearAuthError();
  renderAuth();
}

$("tab-session").addEventListener("click", () => switchAuthMode("session"));
$("tab-password").addEventListener("click", () => switchAuthMode("password"));
$("jump-session").addEventListener("click", () => switchAuthMode("session"));

$("toggle-password").addEventListener("click", () => {
  const input = $("login-password");
  const showing = input.type === "text";
  input.type = showing ? "password" : "text";
  $("toggle-password").setAttribute("aria-pressed", String(!showing));
  $("toggle-password").setAttribute("aria-label", showing ? "Show password" : "Hide password");
  $("toggle-password").querySelector(".eye").hidden = !showing;
  $("toggle-password").querySelector(".eye-off").hidden = showing;
  input.focus();
});

$("login-form").addEventListener(
  "submit",
  withLoading($("login-submit"), async (e) => {
    e.preventDefault();
    clearAuthError();

    const username = $("login-username").value.trim();
    if (!username) return showAuthError("Enter your Instagram username.");

    try {
      const result = await api("/api/login", {
        method: "POST",
        body: { username, password: $("login-password").value },
      });

      if (result.two_factor_required) {
        state.auth.awaiting_two_factor = true;
        renderAuth();
      } else {
        $("login-password").value = "";
        await boot();
        if (result.from_cache) toast("Welcome back", "Reused your saved session.", "success");
      }
    } catch (err) {
      // A checkpoint means the password path is a dead end — move them to the
      // session tab, but keep the explanation on screen.
      if (err.code === "challenge") {
        state.authMode = "session";
        renderAuth();
      }
      showAuthError(err.message, err.hint || "");
    }
  }),
);

$("session-form").addEventListener(
  "submit",
  withLoading($("session-submit"), async (e) => {
    e.preventDefault();
    clearAuthError();

    const sessionid = $("session-id").value.trim();
    if (!sessionid) return showAuthError("Paste the sessionid cookie value.");

    try {
      await api("/api/login-session", {
        method: "POST",
        body: { sessionid, username: $("login-username").value.trim() },
      });
      $("session-id").value = "";
      await boot();
      toast("Signed in", "Using your browser session.", "success");
    } catch (err) {
      showAuthError(err.message, err.hint || "");
    }
  }),
);

$("twofa-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  clearAuthError();
  try {
    await api("/api/two-factor", { method: "POST", body: { code: $("twofa-code").value.trim() } });
    $("twofa-code").value = "";
    $("login-password").value = "";
    state.auth.awaiting_two_factor = false;
    await boot();
  } catch (err) {
    showAuthError(err.message, err.hint || "");
  }
});

$("twofa-back").addEventListener("click", () => {
  state.auth.awaiting_two_factor = false;
  clearAuthError();
  renderAuth();
});

$("logout-btn").addEventListener("click", async () => {
  const forget = confirm("Sign out?\n\nOK clears the cached session too (you'll need your password next time).\nCancel keeps it for a quick sign-in.");
  await api("/api/logout", { method: "POST", body: { forget } });
  state.auth = { authenticated: false, username: "", awaiting_two_factor: false };
  renderAuth();
});

/* ── filters ─────────────────────────────────────────────────────────────── */

document.querySelectorAll(".nav-item[data-state]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-item[data-state]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.filters.state = btn.dataset.state;
    state.filters.collection = "";
    document.querySelectorAll(".collection-list .nav-item").forEach((b) => b.classList.remove("active"));
    reload();
  });
});

function wireChipRow(attr, key) {
  document.querySelectorAll(`.chip[data-${attr}]`).forEach((btn) => {
    btn.addEventListener("click", () => {
      document
        .querySelectorAll(`.chip[data-${attr}]`)
        .forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.filters[key] = btn.dataset[attr];
      reload();
    });
  });
}

wireChipRow("kind", "kind");
wireChipRow("audio", "audio");

$("more-filters-btn").addEventListener("click", () => {
  const row = $("filter-row");
  row.hidden = !row.hidden;
  $("more-filters-btn").setAttribute("aria-expanded", String(!row.hidden));
});

$("filter-artist").addEventListener(
  "input",
  debounce((e) => {
    state.filters.artist = e.target.value.trim();
    reload();
  }, 300),
);

$("filter-owner").addEventListener(
  "input",
  debounce((e) => {
    state.filters.owner = e.target.value.trim();
    reload();
  }, 300),
);

$("filter-duration").addEventListener("change", (e) => {
  state.filters.duration = e.target.value;
  reload();
});

/* ── active filter chips ─────────────────────────────────────────────────── */

const FILTER_LABELS = {
  search: "Search",
  kind: "Type",
  state: "Status",
  collection: "Collection",
  audio: "Audio",
  artist: "Artist",
  owner: "Account",
  duration: "Length",
};

const FILTER_DEFAULTS = {
  search: "", kind: "all", state: "all", collection: "",
  audio: "any", artist: "", owner: "", duration: "any",
};

function clearFilter(key) {
  state.filters[key] = FILTER_DEFAULTS[key];

  if (key === "search") $("search").value = "";
  if (key === "artist") $("filter-artist").value = "";
  if (key === "owner") $("filter-owner").value = "";
  if (key === "duration") $("filter-duration").value = "any";
  if (key === "kind" || key === "audio") {
    document.querySelectorAll(`.chip[data-${key}]`).forEach((b) =>
      b.classList.toggle("active", b.dataset[key] === FILTER_DEFAULTS[key]),
    );
  }
  if (key === "state") {
    document.querySelectorAll(".nav-item[data-state]").forEach((b) =>
      b.classList.toggle("active", b.dataset.state === "all"),
    );
  }
  if (key === "collection") {
    document.querySelectorAll(".collection-row").forEach((b) => b.classList.remove("active"));
  }
  reload();
}

function renderActiveFilters() {
  const active = Object.entries(FILTER_DEFAULTS).filter(
    ([key, fallback]) => state.filters[key] !== fallback,
  );

  const bar = $("active-filters");
  bar.hidden = active.length === 0;
  if (!active.length) return;

  const display = (key) => {
    const value = state.filters[key];
    if (key === "collection") return state.collectionNames[value] || value;
    return value;
  };

  bar.innerHTML =
    active
      .map(
        ([key]) => `
      <span class="filter-chip">${FILTER_LABELS[key]}: <b>${escapeHtml(display(key))}</b>
        <button data-clear="${key}" aria-label="Remove ${FILTER_LABELS[key]} filter">✕</button>
      </span>`,
      )
      .join("") +
    `<button class="link-btn" id="clear-all-filters">Clear all</button>`;

  bar.querySelectorAll("[data-clear]").forEach((btn) =>
    btn.addEventListener("click", () => clearFilter(btn.dataset.clear)),
  );
  $("clear-all-filters").addEventListener("click", () => {
    Object.assign(state.filters, FILTER_DEFAULTS);
    $("search").value = "";
    $("filter-artist").value = "";
    $("filter-owner").value = "";
    $("filter-duration").value = "any";
    document.querySelectorAll(".chip[data-kind]").forEach((b) =>
      b.classList.toggle("active", b.dataset.kind === "all"));
    document.querySelectorAll(".chip[data-audio]").forEach((b) =>
      b.classList.toggle("active", b.dataset.audio === "any"));
    document.querySelectorAll(".nav-item[data-state]").forEach((b) =>
      b.classList.toggle("active", b.dataset.state === "all"));
    document.querySelectorAll(".collection-row").forEach((b) => b.classList.remove("active"));
    reload();
  });
}

$("search").addEventListener(
  "input",
  debounce((e) => {
    state.filters.search = e.target.value.trim();
    reload();
  }, 300),
);

$("sort").addEventListener("change", (e) => {
  state.filters.sort = e.target.value;
  reload();
});

/* ── items ───────────────────────────────────────────────────────────────── */

function itemQuery(extra = {}) {
  return new URLSearchParams({
    search: state.filters.search,
    kind: state.filters.kind,
    state: state.filters.state,
    collection: state.filters.collection,
    audio: state.filters.audio,
    artist: state.filters.artist,
    owner: state.filters.owner,
    duration: state.filters.duration,
    sort: state.filters.sort,
    ...extra,
  }).toString();
}

function showSkeletons(count = 12) {
  $("grid").innerHTML = Array.from({ length: count })
    .map(() => `<div class="skeleton"><div class="sk-media"></div><div class="sk-line" style="width:60%"></div><div class="sk-line" style="width:85%"></div></div>`)
    .join("");
}

async function reload() {
  state.offset = 0;
  state.items = [];
  showSkeletons();
  await loadItems();
}

async function loadItems() {
  if (state.loading) return;
  state.loading = true;

  try {
    const data = await api(`/api/items?${itemQuery({ limit: state.limit, offset: state.offset })}`);
    if (state.offset === 0) state.items = [];
    state.items.push(...data.items);
    state.total = data.total;
    state.hasMore = data.has_more;
    state.offset += data.items.length;
    renderGrid();
  } catch (err) {
    toast("Could not load items", err.message, "error");
  } finally {
    state.loading = false;
  }
}

function statusBadge(item) {
  if (item.download_status === "done") return `<span class="badge done" title="Downloaded">✓</span>`;
  if (item.download_status === "failed")
    return `<span class="badge failed" title="${escapeHtml(item.download_error || "Failed")}">!</span>`;
  return "";
}

function cardHtml(item) {
  const kind = item.typename === "GraphSidecar" ? `${item.media_count}×` : item.is_video ? "▶" : "";
  const duration = item.video_duration ? fmtDuration(item.video_duration) : "";

  return `
    <article class="card${state.selected.has(item.shortcode) ? " selected" : ""}"
             data-shortcode="${item.shortcode}">
      <div class="card-media">
        <img src="/api/thumb/${item.shortcode}" alt="" loading="lazy"
             onerror="this.style.display='none';this.nextElementSibling.hidden=false">
        <div class="fallback" hidden>Thumbnail expired<br>Re-sync to refresh</div>
        <div class="card-check">✓</div>
        <div class="badges">
          ${duration ? `<span class="badge">${duration}</span>` : ""}
          ${kind ? `<span class="badge">${kind}</span>` : ""}
          ${statusBadge(item)}
        </div>
      </div>
      <div class="card-body">
        <span class="card-owner">@${escapeHtml(item.owner || "unknown")}</span>
        <span class="card-caption">${escapeHtml(item.caption || "No caption")}</span>
        ${audioLine(item)}
      </div>
    </article>`;
}

function audioLine(item) {
  if (!item.audio_kind) return "";
  const label = [item.audio_title, item.audio_artist].filter(Boolean).join(" · ");
  const title = item.audio_kind === "original" ? "Creator sound" : "Licensed track";
  return `<span class="card-audio ${item.audio_kind}" title="${title}">
            <span class="audio-dot"></span>${escapeHtml(label || title)}
          </span>`;
}

function renderGrid() {
  const grid = $("grid");
  renderActiveFilters();

  if (!state.items.length) {
    grid.innerHTML = "";
    renderEmptyState();
    $("load-more").hidden = true;
    return;
  }

  $("empty-state").hidden = true;
  grid.innerHTML = state.items.map(cardHtml).join("");
  $("load-more").hidden = !state.hasMore;
  $("load-more").textContent = `Load more (${state.total - state.items.length} left)`;

  grid.querySelectorAll(".card").forEach((card) => {
    const code = card.dataset.shortcode;
    card.querySelector(".card-check").addEventListener("click", (e) => {
      e.stopPropagation();
      toggleSelect(code);
    });
    card.addEventListener("click", (e) => {
      if (e.shiftKey || e.ctrlKey || e.metaKey) toggleSelect(code);
      else openLightbox(code);
    });
  });
}

function renderEmptyState() {
  const el = $("empty-state");
  el.hidden = false;
  const filtered =
    state.filters.search || state.filters.kind !== "all" || state.filters.state !== "all";

  if (filtered) {
    $("empty-title").textContent = "No matches";
    $("empty-body").textContent = "Nothing fits the current filters. Try clearing the search or switching back to All saved.";
    $("empty-action").textContent = "Clear filters";
    $("empty-action").onclick = () => {
      state.filters = { search: "", kind: "all", state: "all", collection: "", sort: state.filters.sort };
      $("search").value = "";
      document.querySelectorAll(".nav-item[data-state]").forEach((b) => b.classList.toggle("active", b.dataset.state === "all"));
      document.querySelectorAll(".chip[data-kind]").forEach((b) => b.classList.toggle("active", b.dataset.kind === "all"));
      reload();
    };
  } else {
    $("empty-title").textContent = "Your library is empty";
    $("empty-body").textContent = "Sync pulls the list of everything you've saved on Instagram. Nothing is downloaded until you pick items.";
    $("empty-action").textContent = "Sync saved items";
    $("empty-action").onclick = startSync;
  }
}

$("load-more").addEventListener("click", loadItems);

$("content").addEventListener("scroll", () => {
  const el = $("content");
  if (state.hasMore && !state.loading && el.scrollTop + el.clientHeight > el.scrollHeight - 400) {
    loadItems();
  }
});

/* ── selection ───────────────────────────────────────────────────────────── */

function toggleSelect(shortcode) {
  if (state.selected.has(shortcode)) state.selected.delete(shortcode);
  else state.selected.add(shortcode);

  document
    .querySelector(`.card[data-shortcode="${shortcode}"]`)
    ?.classList.toggle("selected", state.selected.has(shortcode));
  renderSelection();
}

function renderSelection() {
  const n = state.selected.size;
  $("selection-bar").hidden = n === 0;
  $("selection-count").textContent = `${n} selected`;
  const what = state.mode === "audio" ? "audio" : "";
  $("download-btn").textContent = n > 1
    ? `Download ${what} for ${n} items`.replace("  ", " ")
    : `Download ${what || "selected"}`.trim();
}

function downloadFolder() {
  // Audio-only runs go to their own folder so they never land on top of the
  // videos; a collection filter nests underneath it.
  const collection = state.collectionNames?.[state.filters.collection] || "";
  if (state.mode === "audio") {
    return collection ? `audio/${collection}` : "audio";
  }
  return collection || "saved";
}

document.querySelectorAll(".seg[data-mode]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".seg[data-mode]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.mode = btn.dataset.mode;
    renderSelection();
  });
});

$("audio-export-btn").addEventListener("click", async () => {
  const btn = $("audio-export-btn");
  btn.disabled = true;
  btn.textContent = "Reading…";
  try {
    // Fetch first so an auth/API error surfaces as a toast rather than a
    // broken file download.
    const res = await fetch("/api/audio/export?fmt=csv");
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || `Export failed (${res.status})`);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = Object.assign(document.createElement("a"), {
      href: url,
      download: "instavault-audio.csv",
    });
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    toast("Audio list exported", "Saved as instavault-audio.csv", "success");
  } catch (err) {
    toast("Could not export", err.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Export audio list";
  }
});

$("clear-selection").addEventListener("click", () => {
  state.selected.clear();
  document.querySelectorAll(".card.selected").forEach((c) => c.classList.remove("selected"));
  renderSelection();
});

$("select-all-matching").addEventListener("click", async () => {
  try {
    const { shortcodes } = await api(`/api/items/all?${itemQuery()}`);
    shortcodes.forEach((s) => state.selected.add(s));
    document.querySelectorAll(".card").forEach((c) => c.classList.add("selected"));
    renderSelection();
    toast("Selected", `${shortcodes.length} item(s) match the current filter.`);
  } catch (err) {
    toast("Selection failed", err.message, "error");
  }
});

/* ── jobs ────────────────────────────────────────────────────────────────── */

async function startSync() {
  try {
    await api("/api/sync", { method: "POST", body: {} });
    openDock();
    toast("Sync started", "Reading your saved feed.");
  } catch (err) {
    if (err.status === 409) toast("Already busy", "Wait for the running job to finish.", "warn");
    else toast("Sync failed", err.message, "error");
  }
}

$("sync-btn").addEventListener("click", startSync);

$("download-btn").addEventListener("click", async () => {
  const shortcodes = [...state.selected];
  if (!shortcodes.length) return;

  try {
    await api("/api/download", {
      method: "POST",
      body: {
        shortcodes,
        folder: downloadFolder(),
        mode: state.mode,
      },
    });
    openDock();
    state.selected.clear();
    document.querySelectorAll(".card.selected").forEach((c) => c.classList.remove("selected"));
    renderSelection();
    toast("Download queued", `${shortcodes.length} item(s) in the queue.`);
  } catch (err) {
    if (err.status === 409) toast("Already busy", "One job at a time keeps Instagram happy.", "warn");
    else toast("Download failed", err.message, "error");
  }
});

$("cancel-btn").addEventListener("click", async () => {
  try {
    await api("/api/cancel", { method: "POST", body: { job_id: state.job?.id || "" } });
  } catch (err) {
    toast("Could not cancel", err.message, "warn");
  }
});

function renderJob(job) {
  state.job = job;
  const status = $("dock-status");
  const fill = $("dock-progress-fill");
  const meta = $("dock-meta");

  if (!job) {
    status.textContent = "Idle";
    fill.style.width = "0%";
    fill.className = "dock-progress-fill";
    meta.textContent = "";
    $("cancel-btn").hidden = true;
    $("dock-detail").textContent = "No job running.";
    return;
  }

  const labels = {
    running: job.kind === "sync" ? "Syncing" : "Downloading",
    done: "Finished",
    failed: "Failed",
    cancelled: "Cancelled",
    queued: "Queued",
  };
  status.textContent = labels[job.status] || job.status;

  // A sync has no known total until it finishes walking the feed.
  const indeterminate = job.kind === "sync" && !job.total;
  fill.style.width = indeterminate && job.status === "running" ? "100%" : `${job.percent}%`;
  fill.className = `dock-progress-fill${job.status === "done" ? " done" : ""}${
    job.status === "failed" ? " failed" : ""
  }`;

  const bits = [];
  if (job.total) bits.push(`${job.processed}/${job.total}`);
  else if (job.completed) bits.push(`${job.completed} found`);
  if (job.failed) bits.push(`${job.failed} failed`);
  if (job.bytes) bits.push(fmtBytes(job.bytes));
  if (job.status === "running" && job.eta != null) bits.push(`~${fmtDuration(job.eta)} left`);
  meta.textContent = bits.join(" · ");

  $("cancel-btn").hidden = !job.cancellable;
  $("dock-detail").textContent = job.detail || job.error || "";
}

/* ── dock + log ──────────────────────────────────────────────────────────── */

function openDock() {
  $("dock").dataset.open = "true";
  $("dock-toggle").setAttribute("aria-expanded", "true");
}

$("dock-toggle").addEventListener("click", () => {
  const dock = $("dock");
  const open = dock.dataset.open === "true";
  dock.dataset.open = String(!open);
  $("dock-toggle").setAttribute("aria-expanded", String(!open));
});

function appendLog(entry) {
  const log = $("log");
  const li = document.createElement("li");
  li.innerHTML = `<time>${clockTime(entry.at)}</time><span class="lvl-${entry.level || "info"}">${escapeHtml(entry.message)}</span>`;
  log.appendChild(li);
  while (log.children.length > 300) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

/* ── stats ───────────────────────────────────────────────────────────────── */

function renderStats(stats) {
  $("count-all").textContent = stats.total;
  $("count-pending").textContent = stats.pending;
  $("count-downloaded").textContent = stats.downloaded;
  $("count-failed").textContent = stats.failed;
  $("stat-bytes").textContent = fmtBytes(stats.bytes);
  $("stat-progress").textContent = `${stats.downloaded} of ${stats.total} downloaded`;
  const pct = stats.total ? (stats.downloaded / stats.total) * 100 : 0;
  $("storage-fill").style.width = `${pct}%`;
}

async function loadArtists() {
  try {
    const { artists } = await api("/api/artists");
    $("artist-list").innerHTML = artists
      .map((a) => `<option value="${escapeHtml(a)}">`)
      .join("");
  } catch {
    /* autocomplete is a convenience; a failure here shouldn't block the page */
  }
}

function renderCollections(collections) {
  const block = $("collections-block");
  const list = $("collections-list");
  block.hidden = !collections.length;
  state.collectionNames = {};
  collections.forEach((c) => (state.collectionNames[c.id] = c.name));

  list.innerHTML = collections
    .map(
      (c) => `
      <div class="nav-item collection-row${state.filters.collection === c.id ? " active" : ""}"
           data-collection="${escapeHtml(c.id)}">
        <span class="collection-name">${escapeHtml(c.name)}</span>
        <button class="rename-btn" data-rename="${escapeHtml(c.id)}"
                title="Rename collection" aria-label="Rename collection">✎</button>
        <span class="pill">${c.count}</span>
      </div>`,
    )
    .join("");

  list.querySelectorAll(".collection-row").forEach((row) => {
    row.addEventListener("click", () => {
      list.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
      row.classList.add("active");
      state.filters.collection = row.dataset.collection;
      state.filters.state = "all";
      reload();
    });
  });

  list.querySelectorAll("[data-rename]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = btn.dataset.rename;
      const current = state.collectionNames[id] || "";
      const name = prompt("Name this collection:", current.startsWith("Collection ·") ? "" : current);
      if (name === null) return;
      try {
        const { collections: updated } = await api("/api/collections/rename", {
          method: "POST",
          body: { id, name },
        });
        renderCollections(updated);
      } catch (err) {
        toast("Rename failed", err.message, "error");
      }
    });
  });
}

/* ── live updates ────────────────────────────────────────────────────────── */

let sse;
let sseRetry = 0;

function connectEvents() {
  sse?.close();
  sse = new EventSource("/api/events");

  sse.onopen = () => {
    sseRetry = 0;
  };

  sse.onmessage = (raw) => {
    let event;
    try {
      event = JSON.parse(raw.data);
    } catch {
      return;
    }

    switch (event.kind) {
      case "job":
        renderJob(event.job);
        if (["done", "failed", "cancelled"].includes(event.job.status)) {
          const kind = { done: "success", failed: "error", cancelled: "warn" }[event.job.status];
          toast(event.job.kind === "sync" ? "Sync finished" : "Downloads finished", event.job.detail, kind);
          reload();
        }
        break;

      case "log":
        appendLog(event);
        break;

      case "stats":
        renderStats(event);
        break;

      case "item":
        updateCardStatus(event);
        break;

      case "auth":
        state.auth = { ...state.auth, ...event };
        if (!event.authenticated) renderAuth();
        break;

      case "auth_error":
        toast("Instagram rejected the request", event.error, "error", 9000);
        break;

      case "items_added":
        if (state.offset === 0) reload();
        break;
    }
  };

  sse.onerror = () => {
    sse.close();
    sseRetry = Math.min(sseRetry + 1, 6);
    setTimeout(connectEvents, 1000 * 2 ** sseRetry);
  };
}

function updateCardStatus({ shortcode, status, error }) {
  const card = document.querySelector(`.card[data-shortcode="${shortcode}"]`);
  if (!card) return;

  card.querySelectorAll(".badge.done, .badge.failed, .badge.busy").forEach((b) => b.remove());
  const badges = card.querySelector(".badges");
  if (status === "done" || status === "skipped") {
    badges.insertAdjacentHTML("beforeend", `<span class="badge done" title="Downloaded">✓</span>`);
  } else if (status === "failed") {
    badges.insertAdjacentHTML("beforeend", `<span class="badge failed" title="${escapeHtml(error || "Failed")}">!</span>`);
  }

  const item = state.items.find((i) => i.shortcode === shortcode);
  if (item) item.download_status = status === "skipped" ? "done" : status;
}

/* ── lightbox ────────────────────────────────────────────────────────────── */

let lightboxIndex = -1;

function openLightbox(shortcode) {
  lightboxIndex = state.items.findIndex((i) => i.shortcode === shortcode);
  if (lightboxIndex < 0) return;
  renderLightbox();
  $("lightbox").hidden = false;
}

function renderLightbox() {
  const item = state.items[lightboxIndex];
  if (!item) return;

  const media = $("lightbox-media");
  const downloaded = item.download_status === "done";

  if (downloaded && item.is_video) {
    media.innerHTML = `<video src="/api/media/${item.shortcode}" controls autoplay playsinline></video>`;
  } else if (downloaded) {
    media.innerHTML = `<img src="/api/media/${item.shortcode}" alt="">`;
  } else {
    media.innerHTML = `<img src="/api/thumb/${item.shortcode}" alt=""
      onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'placeholder',textContent:'Preview unavailable — download the item to view it here.'}))">`;
  }

  $("lightbox-owner").textContent = `@${item.owner || "unknown"}`;
  $("lightbox-caption").textContent = item.caption || "No caption";
  $("lightbox-link").href = `https://www.instagram.com/p/${item.shortcode}/`;
  $("lightbox-download").textContent = downloaded ? "Re-download" : "Download";
  $("lightbox-download").onclick = async () => {
    await api("/api/download", {
      method: "POST",
      body: {
        shortcodes: [item.shortcode],
        skip_existing: false,
        mode: state.mode,
        folder: downloadFolder(),
      },
    });
    openDock();
    toast("Download queued", `@${item.owner}`);
  };
}

function closeLightbox() {
  $("lightbox").hidden = true;
  $("lightbox-media").innerHTML = "";
}

function stepLightbox(delta) {
  const next = lightboxIndex + delta;
  if (next < 0 || next >= state.items.length) return;
  lightboxIndex = next;
  renderLightbox();
}

$("lightbox-close").addEventListener("click", closeLightbox);
$("lightbox-prev").addEventListener("click", () => stepLightbox(-1));
$("lightbox-next").addEventListener("click", () => stepLightbox(1));
$("lightbox").addEventListener("click", (e) => {
  if (e.target === $("lightbox")) closeLightbox();
});

/* ── misc UI ─────────────────────────────────────────────────────────────── */

$("theme-btn").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("instavault-theme", next);
});

$("reveal-btn").addEventListener("click", async () => {
  try {
    await api("/api/reveal", { method: "POST", body: {} });
  } catch (err) {
    toast("Could not open folder", err.message, "warn");
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!$("lightbox").hidden) closeLightbox();
    return;
  }

  if (!$("lightbox").hidden) {
    if (e.key === "ArrowLeft") stepLightbox(-1);
    if (e.key === "ArrowRight") stepLightbox(1);
    return;
  }

  const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName);
  if (typing) return;

  if (e.key === "/") {
    e.preventDefault();
    $("search").focus();
  }
  if (e.key === "a") {
    e.preventDefault();
    $("select-all-matching").click();
  }
  if (e.key === "d" && state.selected.size) {
    e.preventDefault();
    $("download-btn").click();
  }
});

/* ── boot ────────────────────────────────────────────────────────────────── */

async function boot() {
  const status = await api("/api/status");
  state.auth = status.auth;
  renderAuth();

  if (!status.auth.authenticated) {
    $("login-username").focus();
    return;
  }

  renderStats(status.stats);
  renderCollections(status.collections);
  renderJob(status.job);
  loadArtists();

  const history = await api("/api/log");
  $("log").innerHTML = "";
  history.filter((e) => e.kind === "log").slice(-60).forEach(appendLog);

  await reload();
}

(function init() {
  const saved = localStorage.getItem("instavault-theme");
  if (saved) document.documentElement.dataset.theme = saved;

  connectEvents();
  boot().catch((err) => {
    $("auth-screen").hidden = false;
    showAuthError("Could not reach the InstaVault server.", err.message);
  });
})();
