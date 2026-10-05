const stateUrl = "/api/state";
const refreshRateMs = 2500;
const statusLabels = {
  unowned: "Unowned",
  owned: "Owned",
  locked: "Locked",
  entry_blocked: "Entry blocked",
  not_found: "Not found",
  rate_limited: "Rate limited",
  scan_complete: "Range complete",
  error: "Error",
};

const elements = {
  scannerState: document.querySelector("#scanner-state"),
  startButton: document.querySelector("#start-button"),
  stopButton: document.querySelector("#stop-button"),
  notice: document.querySelector("#notice"),
  activityList: document.querySelector("#activity-list"),
  logCount: document.querySelector("#log-count"),
  groupsBody: document.querySelector("#groups-body"),
  groupCount: document.querySelector("#group-count"),
  groupSearch: document.querySelector("#group-search"),
  groupFilter: document.querySelector("#group-filter"),
  groupDialog: document.querySelector("#group-dialog"),
  detailClose: document.querySelector("#detail-close"),
  lastUpdated: document.querySelector("#last-updated"),
  settingsForm: document.querySelector("#settings-form"),
  webhookState: document.querySelector("#webhook-state"),
};

let visibleGroups = [];
let noticeTimer;
let settingsDirty = false;
let previousLogKey = null;
let previousGroupKey = null;
const thumbnailCache = new Map();

function showNotice(message, isError = false) {
  elements.notice.textContent = message;
  elements.notice.classList.toggle("notice-error", isError);
  elements.notice.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { elements.notice.hidden = true; }, 4200);
}

async function postJson(url, body = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || result.message || "Request failed.");
  return result;
}

function setScannerState(scanner) {
  const state = scanner.state || "idle";
  elements.scannerState.className = `state-pill state-${state}`;
  elements.scannerState.innerHTML = "";
  const dot = document.createElement("span");
  dot.className = "state-dot";
  const label = document.createTextNode(` ${state.toUpperCase()}`);
  elements.scannerState.append(dot, label);
  elements.startButton.disabled = state === "running" || state === "starting";
  elements.stopButton.disabled = state !== "running" && state !== "starting";
}

function updateMetrics(metrics) {
  document.querySelector("#metric-checks").textContent = (metrics.checks || 0).toLocaleString();
  document.querySelector("#metric-unowned").textContent = (metrics.unowned || 0).toLocaleString();
  document.querySelector("#metric-owned").textContent = (metrics.owned || 0).toLocaleString();
  document.querySelector("#metric-locked").textContent = (metrics.locked || 0).toLocaleString();
  document.querySelector("#metric-blocked").textContent = (metrics.entry_blocked || 0).toLocaleString();
  document.querySelector("#metric-limited").textContent = (metrics.rate_limited || 0).toLocaleString();
}

function formatTime(isoTime) {
  const date = new Date(isoTime);
  return Number.isNaN(date.getTime()) ? "--:--:--" : date.toLocaleTimeString([], { hour12: false });
}

function renderLogs(logs) {
  const logKey = logs.map((entry) => entry.id).join(",");
  if (logKey === previousLogKey) return;
  previousLogKey = logKey;
  elements.activityList.replaceChildren();
  elements.logCount.textContent = `${logs.length} event${logs.length === 1 ? "" : "s"}`;
  if (!logs.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "Waiting for scanner activity.";
    elements.activityList.append(empty);
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const entry of logs) {
    const row = document.createElement("div");
    row.className = `activity-row outcome-${entry.outcome} level-${entry.level}`;
    const time = document.createElement("time");
    time.className = "activity-time";
    time.dateTime = entry.time;
    time.textContent = formatTime(entry.time);
    const content = document.createElement("div");
    content.className = "activity-message";
    const mark = document.createElement("span");
    mark.className = "activity-mark";
    mark.setAttribute("aria-hidden", "true");
    content.append(mark, document.createTextNode(entry.message));
    if (entry.group_id) {
      const meta = document.createElement("span");
      meta.className = "activity-meta";
      meta.textContent = `GROUP ${entry.group_id} · ${statusLabels[entry.outcome] || entry.outcome}`;
      content.append(meta);
    }
    row.append(time, content);
    fragment.append(row);
  }
  elements.activityList.append(fragment);
}

function renderGroups() {
  const search = elements.groupSearch.value.trim().toLowerCase();
  const filter = elements.groupFilter.value;
  const filtered = visibleGroups.filter((group) => {
    const matchesStatus = filter === "all" || group.status === filter;
    const owner = group.owner_name || "unowned";
    const query = `${group.name} ${group.id} ${owner} ${group.owner_id || ""}`.toLowerCase();
    return matchesStatus && query.includes(search);
  });

  elements.groupsBody.replaceChildren();
  elements.groupCount.textContent = `${filtered.length} group${filtered.length === 1 ? "" : "s"}`;
  if (!filtered.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.className = "table-empty";
    cell.colSpan = 6;
    cell.textContent = visibleGroups.length ? "No groups match these filters." : "Group details will appear as the API responds.";
    row.append(cell);
    elements.groupsBody.append(row);
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const group of filtered) {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    const detailButton = document.createElement("button");
    detailButton.className = "group-detail-trigger";
    detailButton.type = "button";
    detailButton.setAttribute("aria-label", `View details for ${group.name || `group ${group.id}`}`);
    detailButton.addEventListener("click", () => showGroupDetails(group));
    const thumbnail = document.createElement("span");
    thumbnail.className = "group-thumbnail";
    const thumbnailUrl = thumbnailCache.get(String(group.id));
    if (thumbnailUrl) {
      const image = document.createElement("img");
      image.src = thumbnailUrl;
      image.alt = "";
      image.loading = "lazy";
      image.addEventListener("error", () => {
        thumbnailCache.set(String(group.id), "");
        thumbnail.replaceChildren(makeInitial(group.name));
      }, { once: true });
      thumbnail.append(image);
    } else {
      thumbnail.append(makeInitial(group.name));
    }
    const identity = document.createElement("span");
    identity.className = "group-identity";
    const name = document.createElement("span");
    name.className = "group-name";
    name.textContent = group.name || `Group ${group.id}`;
    const id = document.createElement("span");
    id.className = "group-id";
    id.textContent = `ID ${group.id}`;
    identity.append(name, id);
    detailButton.append(thumbnail, identity);
    nameCell.append(detailButton);

    const ownerCell = document.createElement("td");
    ownerCell.textContent = group.owner_name || "No owner";
    if (group.owner_id) {
      const ownerId = document.createElement("span");
      ownerId.className = "owner-id";
      ownerId.textContent = `#${group.owner_id}`;
      ownerCell.append(ownerId);
    }

    const memberCell = document.createElement("td");
    memberCell.textContent = Number.isFinite(Number(group.member_count))
      ? Number(group.member_count).toLocaleString()
      : "—";

    const entryCell = document.createElement("td");
    entryCell.className = group.public_entry_allowed ? "entry-yes" : "entry-no";
    entryCell.textContent = group.public_entry_allowed ? "Open" : "Closed";

    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `status-badge badge-${group.status}`;
    badge.textContent = statusLabels[group.status] || group.status;
    statusCell.append(badge);

    const linkCell = document.createElement("td");
    const link = document.createElement("a");
    link.className = "group-link";
    link.href = `https://www.roblox.com/groups/group.aspx?gid=${encodeURIComponent(group.id)}`;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "Open ↗";
    link.setAttribute("aria-label", `Open ${group.name} on Roblox`);
    linkCell.append(link);

    row.append(nameCell, ownerCell, memberCell, entryCell, statusCell, linkCell);
    fragment.append(row);
  }
  elements.groupsBody.append(fragment);
}

function makeInitial(name) {
  const initial = document.createElement("span");
  initial.className = "thumbnail-initial";
  initial.textContent = (name || "R").trim().charAt(0).toUpperCase() || "R";
  return initial;
}

async function loadThumbnails(groups) {
  const groupIds = groups
    .map((group) => String(group.id))
    .filter((id) => !thumbnailCache.has(id));
  if (!groupIds.length) return;
  groupIds.forEach((id) => thumbnailCache.set(id, ""));
  try {
    const query = new URLSearchParams({ group_ids: groupIds.join(",") });
    const response = await fetch(`/api/thumbnails?${query}`, { cache: "no-store" });
    if (!response.ok) return;
    const thumbnails = await response.json();
    for (const id of groupIds) thumbnailCache.set(id, thumbnails[id] || "");
    renderGroups();
  } catch {
    // Keep the initials fallback when the thumbnail service is unavailable.
  }
}

function showGroupDetails(group) {
  const status = statusLabels[group.status] || group.status || "Unknown";
  const statusBadge = document.querySelector("#detail-status");
  statusBadge.className = `status-badge badge-${group.status || "unknown"}`;
  statusBadge.textContent = status;
  document.querySelector("#detail-name").textContent = group.name || `Group ${group.id}`;
  document.querySelector("#detail-id").textContent = `Group ID ${group.id}`;
  const groupUrl = `https://www.roblox.com/groups/group.aspx?gid=${encodeURIComponent(group.id)}`;
  const link = document.querySelector("#detail-link");
  link.href = groupUrl;
  link.setAttribute("aria-label", `View ${group.name || `group ${group.id}`} on Roblox`);
  document.querySelector("#detail-description").textContent = group.description || "No group description.";
  document.querySelector("#detail-owner").textContent = group.owner_name
    ? `${group.owner_name}${group.owner_id ? ` (#${group.owner_id})` : ""}`
    : "No owner";
  document.querySelector("#detail-members").textContent = Number.isFinite(Number(group.member_count))
    ? Number(group.member_count).toLocaleString()
    : "Not provided";
  document.querySelector("#detail-entry").textContent = group.public_entry_allowed ? "Open to the public" : "Public entry disabled";
  document.querySelector("#detail-lock").textContent = group.is_locked ? "Locked" : "Not locked";
  document.querySelector("#detail-created").textContent = group.created_at
    ? new Date(group.created_at).toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" })
    : "Not provided";
  document.querySelector("#detail-verified").textContent = group.is_verified === null || group.is_verified === undefined
    ? "Not provided"
    : group.is_verified ? "Verified" : "Not verified";
  document.querySelector("#detail-shout").textContent = group.shout || "No current shout.";

  const thumbnailWrap = document.querySelector("#detail-thumbnail-wrap");
  thumbnailWrap.replaceChildren();
  const thumbnailUrl = thumbnailCache.get(String(group.id));
  if (thumbnailUrl) {
    const image = document.createElement("img");
    image.src = thumbnailUrl;
    image.alt = `${group.name || "Group"} icon`;
    image.addEventListener("error", () => thumbnailWrap.replaceChildren(makeInitial(group.name)), { once: true });
    thumbnailWrap.append(image);
  } else {
    thumbnailWrap.append(makeInitial(group.name));
  }
  elements.groupDialog.showModal();
}

function loadSettings(settings) {
  document.querySelector("#worker-count").value = settings.worker_count;
  document.querySelector("#request-interval").value = settings.request_interval;
  document.querySelector("#request-timeout").value = settings.request_timeout;
  document.querySelector("#group-id-min").value = settings.group_id_min;
  document.querySelector("#group-id-max").value = settings.group_id_max;
  document.querySelector("#console-progress").checked = settings.console_progress;
  elements.webhookState.textContent = settings.webhook_configured ? "Webhook configured" : "Webhook not set";
  elements.webhookState.classList.toggle("configured", settings.webhook_configured);
}

async function refreshState() {
  try {
    const response = await fetch(stateUrl, { cache: "no-store" });
    if (!response.ok) throw new Error("Dashboard state unavailable.");
    const data = await response.json();
    setScannerState(data.scanner);
    updateMetrics(data.metrics);
    renderLogs(data.logs);
    const groupKey = JSON.stringify(data.groups);
    if (groupKey !== previousGroupKey) {
      visibleGroups = data.groups;
      previousGroupKey = groupKey;
      renderGroups();
      loadThumbnails(data.groups);
    }
    if (!settingsDirty) loadSettings(data.settings);
    elements.lastUpdated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function runControl(action) {
  elements.startButton.disabled = true;
  elements.stopButton.disabled = true;
  try {
    const result = await postJson(`/api/control/${action}`);
    showNotice(result.message);
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    await refreshState();
  }
}

elements.startButton.addEventListener("click", () => runControl("start"));
elements.stopButton.addEventListener("click", () => runControl("stop"));
elements.groupSearch.addEventListener("input", renderGroups);
elements.groupFilter.addEventListener("change", renderGroups);
elements.detailClose.addEventListener("click", () => elements.groupDialog.close());
elements.groupDialog.addEventListener("click", (event) => {
  if (event.target === elements.groupDialog) elements.groupDialog.close();
});
elements.settingsForm.addEventListener("input", () => { settingsDirty = true; });

elements.settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const saveButton = elements.settingsForm.querySelector("button[type='submit']");
  saveButton.disabled = true;
  const settings = {
    worker_count: Number(document.querySelector("#worker-count").value),
    request_interval: Number(document.querySelector("#request-interval").value),
    request_timeout: Number(document.querySelector("#request-timeout").value),
    group_id_min: Number(document.querySelector("#group-id-min").value),
    group_id_max: Number(document.querySelector("#group-id-max").value),
    console_progress: document.querySelector("#console-progress").checked,
    webhook: document.querySelector("#webhook").value.trim(),
  };
  try {
    const result = await postJson("/api/settings", settings);
    settingsDirty = false;
    document.querySelector("#webhook").value = "";
    loadSettings(result.settings);
    showNotice("Settings saved. Scan parameters apply next start; CMD scan logs apply immediately.");
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    saveButton.disabled = false;
    await refreshState();
  }
});

refreshState();
setInterval(refreshState, refreshRateMs);
