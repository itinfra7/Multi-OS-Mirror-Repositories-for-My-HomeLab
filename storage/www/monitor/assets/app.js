// Credits: itinfra7 from GitHub
const $ = (selector, root = document) => root.querySelector(selector);
const template = $("#repo-template");
const grid = $("#repo-grid");

const DEFAULT_REPOS = [
  { id: "alpine", name: "Alpine Linux", repoUrl: "/alpine/", apiUrl: "/api/alpine/status.json" },
  { id: "opencsw", name: "Solaris 10 OpenCSW", repoUrl: "/opencsw/", apiUrl: "/api/opencsw/status.json" },
  { id: "openbsd", name: "OpenBSD amd64", repoUrl: "/openbsd/", apiUrl: "/api/openbsd/status.json" },
  { id: "omnios", name: "OmniOS LTS", repoUrl: "/omnios/", apiUrl: "/api/omnios/status.json" },
  { id: "openindiana", name: "OpenIndiana Hipster", repoUrl: "/openindiana/", apiUrl: "/api/openindiana/status.json" },
];

let repoConfig = DEFAULT_REPOS;
let trafficState = null;
let trafficRefreshInFlight = false;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function setText(node, value) {
  if (!node) return;
  const next = String(value ?? "-");
  if (node.textContent !== next) node.textContent = next;
}

function fmtBytes(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let n = Number(value);
  let idx = 0;
  while (n >= 1024 && idx < units.length - 1) {
    n /= 1024;
    idx += 1;
  }
  return `${n.toFixed(1)} ${units[idx]}`;
}

function fmtRate(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const n = Number(value);
  if (n >= 1000 * 1000) return `${(n / 1000 / 1000).toFixed(2)} MB/s`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)} KB/s`;
  return `${n.toFixed(0)} B/s`;
}

function formatElapsedSeconds(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const secs = value % 60;
  return [hours, minutes, secs].map((item) => String(item).padStart(2, "0")).join(":");
}

function parseElapsedSeconds(value) {
  const text = String(value || "").trim();
  if (!text || text === "-") return null;
  const compact = text.match(/^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+))?$/);
  if (compact && (compact[1] || compact[2])) {
    return (Number(compact[1] || 0) * 86400) + (Number(compact[2] || 0) * 3600) + (Number(compact[3] || 0) * 60);
  }
  let days = 0;
  let rest = text;
  if (rest.includes("-")) {
    const parts = rest.split("-", 2);
    days = Number(parts[0]) || 0;
    rest = parts[1];
  }
  const pieces = rest.split(":").map((part) => Number(part));
  if (pieces.some((part) => !Number.isFinite(part))) return null;
  if (pieces.length === 3) return days * 86400 + pieces[0] * 3600 + pieces[1] * 60 + pieces[2];
  if (pieces.length === 2) return days * 86400 + pieces[0] * 60 + pieces[1];
  if (pieces.length === 1) return days * 86400 + pieces[0] * 60;
  return null;
}

function eventTimestamp(line) {
  const match = String(line || "").match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\|/);
  if (!match) return null;
  const ms = Date.parse(match[1]);
  return Number.isFinite(ms) ? ms : null;
}

function currentTaskStartMs(data) {
  if (!data?.sync?.process?.running) return null;
  const progress = data?.sync?.progress || {};
  const events = Array.isArray(data?.sync?.events) ? data.sync.events : [];
  const release = String(progress.release || "");
  const repo = String(progress.repo || "");
  const current = String(progress.current || "");

  const patterns = [];
  if (release && repo) patterns.push(`|repo-start|${release}|${repo}`);
  if (repo) patterns.push(`|repo-start|${repo}`);
  if (current) patterns.push(`|sync-start|${current}|`);

  for (let index = events.length - 1; index >= 0; index -= 1) {
    const line = String(events[index] || "");
    if (patterns.some((pattern) => line.includes(pattern))) {
      return eventTimestamp(line);
    }
  }

  const raw = String(progress.raw || "");
  const rawMatch = raw.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\|/);
  if (rawMatch) {
    const ms = Date.parse(rawMatch[1]);
    if (Number.isFinite(ms)) return ms;
  }

  return null;
}

function rowIsReady(row) {
  return ["ready", "complete", "ok", "online"].includes(String(row.status || "").toLowerCase());
}

function rowIsRequired(row) {
  return !["optional"].includes(String(row.status || "").toLowerCase());
}

function incompleteRequiredRows(data) {
  return coverageRows(data).filter((row) => rowIsRequired(row) && !rowIsReady(row));
}

function currentEtaSeconds(data) {
  const sync = data?.sync || {};
  const progress = sync.progress || {};
  const currentPercent = currentSyncItemPercent(data);
  const taskStart = currentTaskStartMs(data);
  const taskElapsed = taskStart === null ? null : Math.max(0, Math.floor((Date.now() - taskStart) / 1000));
  const speed = finiteNumber(sync.upstream_speed?.bytes_per_second)
    ?? finiteNumber(progress.upstream_speed_bps)
    ?? finiteNumber(progress.payload_speed_bps);
  const candidates = [];

  const estimateEta = parseElapsedSeconds(sync.estimate?.eta);
  if (estimateEta !== null && estimateEta > 0) candidates.push(estimateEta);

  const rsyncEta = parseElapsedSeconds(sync.rsync?.eta);
  if (rsyncEta !== null && rsyncEta > 0) candidates.push(rsyncEta);

  const payloadDone = finiteNumber(progress.payload_bytes_done);
  const payloadTotal = finiteNumber(progress.payload_bytes_total);
  if (speed !== null && speed > 0 && payloadDone !== null && payloadTotal !== null && payloadTotal > payloadDone) {
    candidates.push((payloadTotal - payloadDone) / speed);
  }

  if (taskElapsed !== null && currentPercent !== null && currentPercent > 0 && currentPercent < 100) {
    candidates.push(taskElapsed * ((100 - currentPercent) / currentPercent));
  }

  if (!candidates.length) return null;
  return Math.max(...candidates.map((value) => Math.max(0, Math.floor(value))));
}

function repoEtaLine(data, percent) {
  const sync = data?.sync || {};
  const process = sync.process || {};
  const running = syncIsRunning(data);
  const incomplete = incompleteRequiredRows(data);
  const pidText = process?.pid ? `PID ${process.pid}` : "no process";

  if (!running) {
    if (incomplete.length) {
      return { value: "unknown", detail: `idle / ${incomplete.length} required targets incomplete` };
    }
    return { value: "00:00:00", detail: "whole repo complete" };
  }

  const currentEta = currentEtaSeconds(data);
  let wholeEta = null;
  const pct = clampPercent(percent);
  const currentTargets = currentSyncTargets(data);
  const onlyCurrentRequired = incomplete.length === 1 && rowIsCurrent(incomplete[0], currentTargets);
  if (onlyCurrentRequired) wholeEta = currentEta;

  if (wholeEta === null) {
    const currentText = currentEta === null ? "" : ` / current target ${formatElapsedSeconds(currentEta)}`;
    const leftText = incomplete.length ? ` / ${incomplete.length} targets left` : "";
    return { value: "unknown", detail: `whole repo ETA unavailable${currentText}${leftText} / ${pidText}` };
  }

  const pctTextValue = pct === null ? "-" : `${pct.toFixed(1)}%`;
  const leftText = incomplete.length ? `${incomplete.length} targets left` : "finishing";
  return {
    value: formatElapsedSeconds(wholeEta),
    detail: `whole repo remaining / ${pctTextValue} / ${leftText} / ${pidText}`,
  };
}

function fmtDate(value) {
  if (!value) return "-";
  const date = /^\d+(\.\d+)?$/.test(String(value)) ? new Date(Number(value) * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date).replace(",", "") + " KST";
}

function statusClass(value) {
  const text = String(value || "idle").toLowerCase();
  if (["online", "ready", "complete", "ok", "healthy"].includes(text)) return "ok";
  if (["running", "syncing", "partial"].includes(text)) return "running";
  if (["queued", "pending", "idle", "optional", "unknown"].includes(text)) return "idle";
  return "offline";
}

function healthLabel(summary) {
  if (summary.fetchError) return "error";
  if (summary.badServices > 0 || summary.errors > 0) return "issue";
  if (summary.warnings > 0) return "warn";
  if (summary.syncStatus === "running") return "running";
  return "healthy";
}

function healthClass(label) {
  if (["healthy"].includes(label)) return "ok";
  if (["running"].includes(label)) return "running";
  if (["warn"].includes(label)) return "idle";
  return "offline";
}

async function getJson(url, timeoutMs = 5000) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${url}${url.includes("?") ? "&" : "?"}ts=${Date.now()}`, {
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    return response.json();
  } catch (error) {
    if (error?.name === "AbortError") throw new Error(`timeout after ${timeoutMs}ms`);
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

async function loadConfig() {
  try {
    const config = await getJson("config.json");
    if (Array.isArray(config.repositories) && config.repositories.length) {
      repoConfig = config.repositories.map((repo) => ({
        id: repo.id,
        name: repo.name || repo.id,
        repoUrl: repo.repoUrl || `/${repo.id}/`,
        apiUrl: repo.apiUrl || `/api/${repo.id}/status.json`,
      }));
    }
  } catch (error) {
    repoConfig = DEFAULT_REPOS;
  }
}

function limitMbps(limit) {
  const bps = finiteNumber(limit?.rate_bytes_per_second);
  if (bps !== null) return bps / 1000 / 1000;
  const match = String(limit?.rate || "").trim().toLowerCase().match(/^([0-9.]+)\s*mbit$/);
  return match ? Number(match[1]) / 8 : null;
}

function formatLimitInput(value) {
  if (!Number.isFinite(value)) return "";
  return String(Math.round(value * 100) / 100);
}

function readLimitInput() {
  const input = $("#limit-rate-mbps");
  if (!input) return null;
  const value = Number(input.value);
  return Number.isFinite(value) && value > 0 ? value : null;
}

function limitInputChanged() {
  const configured = limitMbps(trafficState?.limit);
  const entered = readLimitInput();
  return configured !== null && entered !== null && Math.abs(configured - entered) > 0.005;
}

function updateLimitButton() {
  const toggle = $("#limit-toggle");
  if (!toggle) return;
  const enabled = Boolean(trafficState?.limit?.enabled);
  const changed = enabled && limitInputChanged();
  toggle.disabled = false;
  toggle.textContent = enabled ? (changed ? "apply limit" : "disable limit") : "enable limit";
  toggle.title = enabled
    ? (changed ? "Apply aggregate upstream download limit" : "Disable aggregate upstream download limit")
    : "Enable aggregate upstream download limit";
}

function renderTraffic(data) {
  trafficState = data;
  const limitNode = $("#sync-limit");
  const toggle = $("#limit-toggle");
  const input = $("#limit-rate-mbps");
  const limit = data?.limit || {};
  const enabled = Boolean(limit.enabled);
  const mbps = limitMbps(limit);
  if (input) {
    input.disabled = false;
    if (document.activeElement !== input && mbps !== null) input.value = formatLimitInput(mbps);
  }
  setText(limitNode, enabled ? `configured ${limit.rate_human || limit.rate || "-"}` : "aggregate limit OFF");
  if (toggle) {
    updateLimitButton();
  }
}

async function refreshTraffic() {
  if (trafficRefreshInFlight) return;
  trafficRefreshInFlight = true;
  try {
    renderTraffic(await getJson("/api/mirrors/traffic.json", 4000));
  } catch (error) {
    setText($("#sync-limit"), `traffic api error: ${error.message}`);
    const toggle = $("#limit-toggle");
    if (toggle) {
      toggle.disabled = true;
      toggle.textContent = "error";
    }
    const input = $("#limit-rate-mbps");
    if (input) input.disabled = true;
  } finally {
    trafficRefreshInFlight = false;
  }
}

async function postLimitRate(mbps) {
  const response = await fetch(`/api/mirrors/limit/set-rate?ts=${Date.now()}`, {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mbps, enabled: true }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data?.set_rate?.error || data?.set_rate?.stderr || `status ${response.status}`);
  return data;
}

async function toggleLimit() {
  const toggle = $("#limit-toggle");
  const input = $("#limit-rate-mbps");
  const enabled = Boolean(trafficState?.limit?.enabled);
  const changed = enabled && limitInputChanged();
  const mbps = readLimitInput();
  if (toggle) {
    toggle.disabled = true;
    toggle.textContent = "wait";
  }
  if (input) input.disabled = true;
  try {
    let data;
    if (!enabled || changed) {
      if (mbps === null) throw new Error("enter MB/s");
      data = await postLimitRate(mbps);
    } else {
      const response = await fetch(`/api/mirrors/limit/toggle?ts=${Date.now()}`, {
        method: "POST",
        cache: "no-store",
      });
      data = await response.json();
      if (!response.ok) throw new Error(data?.toggle?.stderr || `status ${response.status}`);
    }
    renderTraffic(data);
  } catch (error) {
    setText($("#sync-limit"), `toggle failed: ${error.message}`);
    if (input) input.disabled = false;
    if (toggle) {
      updateLimitButton();
    }
  }
}

function serviceStats(services = {}) {
  const entries = Object.entries(services || {});
  const bad = entries.filter(([, state]) => String(state).toLowerCase() !== "online").length;
  return { entries, total: entries.length, bad };
}

function listWarnings(data) {
  const errors = Array.isArray(data?.errors) ? data.errors.length : 0;
  const alerts = Array.isArray(data?.alerts) ? data.alerts.length : 0;
  return { errors, alerts };
}

function ratioPercent(done, total) {
  const doneNumber = Number(done);
  const totalNumber = Number(total);
  if (!Number.isFinite(doneNumber) || !Number.isFinite(totalNumber) || totalNumber <= 0) return null;
  return (doneNumber / totalNumber) * 100;
}

function finiteNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function normTarget(value) {
  return String(value || "")
    .toLowerCase()
    .replaceAll("_", "-")
    .replace(/\/(x86_64|x86-64|amd64|i386|sparc)\/?$/g, "")
    .replace(/\/+/g, "/")
    .replace(/^\/|\/$/g, "");
}

function clampPercent(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  return Math.max(0, Math.min(number, 100));
}

function currentSyncItemPercent(data) {
  const estimate = data?.sync?.estimate || {};
  const progress = data?.sync?.progress || {};
  const rsync = data?.sync?.rsync || {};
  const phase = String(progress.phase || "").toLowerCase();
  const current = String(progress.current || "").toLowerCase();
  const candidates = [];

  candidates.push(finiteNumber(estimate.current_percent));
  if ((phase.includes("manifest") || (phase === "syncing" && current.startsWith("manifest:")) || (phase === "syncing" && current.startsWith("file:"))) && progress.manifests_total) {
    candidates.push(ratioPercent(progress.manifests_done, progress.manifests_total));
  }
  if (progress.payload_bytes_total) {
    candidates.push(ratioPercent(progress.payload_bytes_done, progress.payload_bytes_total));
  }
  if (progress.payloads_known) {
    candidates.push(ratioPercent(progress.payloads_done, progress.payloads_known));
  }
  if (progress.payload_batch_bytes_total && !progress.manifests_total) {
    candidates.push(ratioPercent(progress.payload_batch_bytes_done, progress.payload_batch_bytes_total));
  }
  if (progress.payload_batch_total && !progress.manifests_total) {
    candidates.push(ratioPercent(progress.payload_batch_done, progress.payload_batch_total));
  }
  if (!phase.includes("manifest") && progress.manifests_total) {
    candidates.push(ratioPercent(progress.manifests_done, progress.manifests_total));
  }
  if (phase.includes("rsync") || rsync.percent !== undefined) {
    candidates.push(finiteNumber(rsync.percent));
  }

  for (const candidate of candidates) {
    const value = clampPercent(candidate);
    if (value !== null) return value;
  }
  return null;
}

function currentSyncTargets(data) {
  const progress = data?.sync?.progress || {};
  const values = [
    [progress.release, progress.repo].filter(Boolean).join("/"),
    progress.repo,
    progress.current,
    progress.current_job,
    progress.message,
    data?.sync?.upstream_speed?.target,
  ].map(normTarget).filter(Boolean);
  return [...new Set(values)];
}

function rowIsCurrent(row, currentTargets) {
  const rowName = normTarget(row.name);
  if (!rowName) return false;
  return currentTargets.some((target) => rowName.includes(target) || target.includes(rowName));
}

function coverageCompletionPercent(data) {
  const rows = coverageRows(data).filter((row) => !["optional"].includes(String(row.status).toLowerCase()));
  if (!rows.length) return null;

  const currentTargets = currentSyncTargets(data);
  const currentPercent = currentSyncItemPercent(data);

  let knownRows = 0;
  let score = 0;
  let hasIncomplete = false;
  for (const row of rows) {
    const status = String(row.status || "").toLowerCase();
    if (["ready", "complete", "ok", "online"].includes(status)) {
      score += 1;
      knownRows += 1;
    } else if (["syncing", "running"].includes(status)) {
      const isCurrent = rowIsCurrent(row, currentTargets);
      score += isCurrent && currentPercent !== null ? currentPercent / 100 : 0;
      knownRows += 1;
      hasIncomplete = true;
    } else if (status === "partial") {
      const isCurrent = rowIsCurrent(row, currentTargets);
      score += isCurrent && currentPercent !== null ? currentPercent / 100 : 0;
      knownRows += 1;
      hasIncomplete = true;
    } else if (["queued", "pending", "missing", "offline", "error", "fail"].includes(status)) {
      knownRows += 1;
      hasIncomplete = true;
    }
  }

  if (!knownRows) return null;
  const percent = (score / knownRows) * 100;
  if (!hasIncomplete) return 100;
  return Math.max(0, Math.min(percent, 99.9));
}

function coverageHasIncomplete(data) {
  const rows = coverageRows(data).filter((row) => !["optional"].includes(String(row.status).toLowerCase()));
  return rows.some((row) => !["ready", "complete", "ok", "online"].includes(String(row.status || "").toLowerCase()));
}

function syncIsRunning(data) {
  return Boolean(data?.sync?.process?.running) || String(data?.sync?.status || "").toLowerCase() === "running";
}

function syncOperationPercent(data) {
  const estimate = data?.sync?.estimate || {};
  const progress = data?.sync?.progress || {};
  const candidates = [
    ratioPercent(progress.done, progress.total),
    ratioPercent(progress.job_done, progress.job_total),
    ratioPercent(progress.payload_bytes_done, progress.payload_bytes_total),
    ratioPercent(progress.payloads_done, progress.payloads_known),
    ratioPercent(progress.manifests_done, progress.manifests_total),
    finiteNumber(estimate.overall_percent),
    finiteNumber(estimate.percent),
    finiteNumber(estimate.current_percent),
  ];

  for (const candidate of candidates) {
    const value = clampPercent(candidate);
    if (value !== null) return value;
  }
  return null;
}

function applyCoverageGuard(data, percent) {
  const coveragePercent = coverageCompletionPercent(data);
  if (coveragePercent === null) return percent;
  return Math.min(percent, coveragePercent);
}

function progressPercent(data) {
  const coveragePercent = coverageCompletionPercent(data);
  if (coveragePercent !== null && (!syncIsRunning(data) || coverageHasIncomplete(data) || coveragePercent < 100)) {
    return coveragePercent;
  }

  const operationPercent = syncOperationPercent(data);
  if (operationPercent !== null) return operationPercent;
  if (coveragePercent !== null) return coveragePercent;
  if (String(data?.sync?.status).toLowerCase() === "complete") return 100;
  if (String(data?.sync?.status).toLowerCase() === "running") return null;
  return 0;
}

function processRateLimit(data) {
  const process = data?.sync?.process || {};
  const commands = [
    process.command,
    ...(Array.isArray(process.children) ? process.children.map((child) => child.command) : []),
  ].filter(Boolean).map(String);

  for (const command of commands) {
    const rsync = command.match(/--bwlimit(?:=|\s+)([0-9.]+)([A-Za-z]*)/);
    if (rsync) {
      const value = Number(rsync[1]);
      const unit = rsync[2] || "KiB/s";
      if (Number.isFinite(value) && value >= 1024 && !rsync[2]) return `rsync cap ${(value / 1024).toFixed(1)} MiB/s`;
      return `rsync cap ${rsync[1]}${unit}`;
    }
    const curl = command.match(/--limit-rate\s+([0-9.]+[A-Za-z]*)/);
    if (curl) return `curl cap ${curl[1]}`;
  }

  return "";
}

function syncDetail(data) {
  const sync = data?.sync || {};
  const progress = sync.progress || {};
  const estimate = sync.estimate || {};
  const rsync = sync.rsync || {};
  const target = [progress.release, progress.repo].filter(Boolean).join("/");
  const current = progress.current_job || [target, progress.current || progress.phase || progress.message].filter(Boolean).join(" ") || "-";
  const counters = [
    progress.payloads_known ? `payloads ${progress.payloads_done || 0}/${progress.payloads_known}` : "",
    progress.manifests_total ? `manifests ${progress.manifests_done || 0}/${progress.manifests_total}` : "",
  ].filter(Boolean).join(" / ");
  const rawSpeed = estimate.speed || estimate.speed_human || rsync.speed || "-";
  const cap = processRateLimit(data);
  const speed = cap ? (rawSpeed !== "-" ? `${rawSpeed} (${cap})` : cap) : rawSpeed;
  const eta = estimate.eta || rsync.eta || "-";
  return { current, counters, speed, eta };
}

function upstreamSpeed(data) {
  const sync = data?.sync || {};
  const upstream = sync.upstream_speed || {};
  const progress = sync.progress || {};
  const estimate = sync.estimate || {};
  const rsync = sync.rsync || {};
  const running = Boolean(sync.process?.running) || String(sync.status || "").toLowerCase() === "running";
  const bps = upstream.bytes_per_second;
  const human = upstream.human || fmtRate(bps);
  const speed = human && human !== "-" ? human : (running ? (estimate.speed || estimate.speed_human || rsync.speed || "-") : "0 B/s");
  const target = upstream.target || progress.current_job || [progress.release, progress.repo, progress.current].filter(Boolean).join(" ") || "";
  const source = upstream.source || (running ? "status-json" : "idle");
  const normalizedSource = String(source).toLowerCase();
  const sourceLabel = normalizedSource === "sync-progress" ? "sync-progress avg" : source;
  if (normalizedSource === "idle") {
    return {
      speed: "0 B/s",
      detail: target ? `idle / last target ${target}` : "idle",
    };
  }
  return {
    speed,
    detail: [sourceLabel, target].filter(Boolean).join(" / ") || "-",
  };
}

function syncDetailText(detail) {
  const parts = [];
  if (detail.current && detail.current !== "-") parts.push(`current ${detail.current}`);
  if (detail.counters) parts.push(detail.counters);
  if (detail.speed && detail.speed !== "-") parts.push(`speed ${detail.speed}`);
  return parts.join(" / ") || "-";
}

function storageLines(config, data) {
  const repoStorage = trafficState?.repo_storage?.[config.id];
  if (repoStorage?.exists && Number.isFinite(Number(repoStorage.used_bytes))) {
    const scanned = repoStorage.scanned_at ? ` / scanned ${fmtDate(repoStorage.scanned_at)}` : "";
    return {
      used: repoStorage.used_human || fmtBytes(repoStorage.used_bytes),
      detail: `${repoStorage.path || config.repoUrl} repo data${scanned}`,
    };
  }
  if (!trafficState) {
    return {
      used: "-",
      detail: "repo storage loading",
    };
  }
  if (repoStorage?.scanning) {
    return {
      used: "-",
      detail: "repo storage scan in progress",
    };
  }
  if (repoStorage?.error) {
    return {
      used: "-",
      detail: `repo storage error: ${repoStorage.error}`,
    };
  }

  const storage = data.storage || {};
  return {
    used: `${storage.used_human || fmtBytes(storage.used_bytes)} / ${storage.total_human || fmtBytes(storage.total_bytes)}`,
    detail: `filesystem ${storage.used_pct ?? "-"}% used / ${storage.available_human || storage.free_human || fmtBytes(storage.available_bytes ?? storage.free_bytes)} available`,
  };
}

function planLine(config, data) {
  const mirror = data?.mirror || {};
  const state = mirror.state || {};
  const versions = Array.isArray(mirror.versions) ? mirror.versions.join(" / ") : "";
  const latest = state.latest || mirror.log?.current_run_latest || "";
  const previous = state.previous || mirror.log?.current_run_previous || "";
  const arch = state.arch || mirror.arch || "";
  const keep = Array.isArray(state.keep) ? state.keep.join(" / ") : "";

  if (config?.id === "alpine") {
    return [
      "edge",
      latest && `latest stable ${latest}`,
      previous && previous !== "none" ? `previous stable ${previous}` : "",
      arch && `arch ${arch}`,
    ].filter(Boolean).join(" | ") || "edge + latest/previous stable";
  }

  if (config?.id === "opencsw") {
    const releases = mirror.published_releases;
    return [
      "OpenCSW full mirror",
      mirror.current_exists ? "current published" : "current missing",
      Number.isFinite(Number(releases)) ? `${releases} retained snapshots` : "",
    ].filter(Boolean).join(" | ");
  }

  if (config?.id === "openbsd") {
    return [
      latest && `latest ${latest}`,
      previous && `previous ${previous}`,
      arch && `arch ${arch}`,
      "release/packages/syspatch/patches",
    ].filter(Boolean).join(" | ");
  }

  if (config?.id === "omnios") {
    return [
      latest && `latest LTS ${latest}`,
      previous && `previous LTS ${previous}`,
      "aliases latest-lts / previous-lts",
      "SFE localhostomnios",
    ].filter(Boolean).join(" | ");
  }

  if (config?.id === "openindiana") {
    return "rolling Hipster | publishers openindiana.org / hipster-encumbered / localhostoih";
  }

  const line = [
    latest && `latest ${latest}`,
    previous && `previous ${previous}`,
    keep && `keep ${keep}`,
    versions && `versions ${versions}`,
    arch && `arch ${arch}`,
  ].filter(Boolean).join(" | ");
  return line || "published mirror state";
}

function sourceLine(data) {
  const source = data?.sync?.source || data?.mirror?.source || data?.mirror?.state?.source || data?.mirror?.client_url || "-";
  if (source === "https://pkg.openindiana.org + http://sfe.opencsw.org") {
    return "https://pkg.openindiana.org/hipster + https://pkg.openindiana.org/hipster-encumbered + http://sfe.opencsw.org/localhostoih";
  }
  return source;
}

function pageOrigin() {
  if (window.location.origin && window.location.origin !== "null") return window.location.origin;
  return new URL(document.baseURI).origin;
}

function firstHttpUrl(lines) {
  const values = Array.isArray(lines) ? lines : [lines].filter(Boolean);
  for (const value of values) {
    const match = String(value).match(/https?:\/\/[^\s'"]+/);
    if (match) return match[0].replace(/[),.]+$/, "");
  }
  return pageOrigin();
}

function originFromStatus(data) {
  const current = pageOrigin();
  if (current) return current;
  const hintUrl = firstHttpUrl(data?.mirror?.client_hints || data?.mirror?.client_url);
  try {
    const url = new URL(hintUrl);
    return `${url.protocol}//${url.host}`;
  } catch (error) {
    return pageOrigin();
  }
}

function latestReadyOpenBsdVersion(data) {
  const latest = data?.mirror?.state?.latest;
  const releases = Array.isArray(data?.mirror?.releases) ? data.mirror.releases : [];
  const isReady = (release) => {
    const parts = release?.parts || {};
    const releaseReady = parts.release?.status === "ready";
    const packagesReady = parts.packages?.status === "ready";
    return releaseReady && packagesReady;
  };
  const latestRelease = releases.find((release) => release.version === latest);
  if (latestRelease && isReady(latestRelease)) return latest;
  const readyRelease = releases.find(isReady);
  return readyRelease?.version || latest || data?.mirror?.state?.previous || "7.x";
}

function coverageRows(data) {
  const mirror = data?.mirror || {};
  const rows = [];

  if (Array.isArray(mirror.repositories)) {
    for (const item of mirror.repositories) {
      rows.push({
        name: item.target || [item.release, item.repo].filter(Boolean).join("/") || item.path || "repository",
        meta: [
          item.bytes_human,
          item.files !== undefined ? `${item.files} files` : "",
          item.apk_packages !== undefined ? `${item.apk_packages} apk` : "",
          item.payloads !== undefined ? `${item.payloads} payloads` : "",
          item.partial_files ? `${item.partial_files} partial` : "",
        ].filter(Boolean).join(" / "),
        status: item.status || (item.complete ? "ready" : item.exists ? "partial" : "missing"),
      });
    }
  }

  if (!rows.length && Array.isArray(mirror.releases)) {
    for (const release of mirror.releases) {
      if (release.parts) {
        for (const [part, partData] of Object.entries(release.parts)) {
          rows.push({
            name: `${release.version}/${part}`,
            meta: [
              partData.bytes_human,
              partData.files !== undefined ? `${partData.files} files` : "",
              partData.temp_files ? `${partData.temp_files} temp` : "",
            ].filter(Boolean).join(" / "),
            status: partData.status || "unknown",
          });
        }
      } else {
        rows.push({
          name: release.version,
          meta: [
            release.bytes_human,
            release.files !== undefined ? `${release.files} files` : "",
            release.payloads !== undefined ? `${release.payloads} payloads` : "",
          ].filter(Boolean).join(" / "),
          status: release.status || "ready",
        });
      }
    }
  }

  if (!rows.length && mirror.current?.channels) {
    for (const [name, item] of Object.entries(mirror.current.channels)) {
      rows.push({
        name,
        meta: [
          fmtBytes(item.bytes),
          item.files !== undefined ? `${item.files} files` : "",
          item.packages !== undefined ? `${item.packages} pkgs` : "",
        ].filter(Boolean).join(" / "),
        status: "ready",
      });
    }
    rows.sort((a, b) => {
      const an = mirror.current.channels[a.name]?.bytes || 0;
      const bn = mirror.current.channels[b.name]?.bytes || 0;
      return bn - an;
    });
  }

  if (!rows.length && Array.isArray(mirror.branches)) {
    for (const item of mirror.branches) {
      rows.push({
        name: item.name,
        meta: [item.bytes_human, `${item.files} files`, `${item.apk_packages} apk`].join(" / "),
        status: "ready",
      });
    }
  }

  return rows;
}

function clientLines(data) {
  const hints = data?.mirror?.client_hints;
  if (Array.isArray(hints)) return hints.filter((line) => line && !String(line).startsWith("#")).slice(0, 4);
  if (data?.mirror?.client_url) return [data.mirror.client_url];
  return [];
}

function clientApplyRows(config, data) {
  const origin = originFromStatus(data);
  const id = config.id;

  if (id === "alpine") {
    const latest = data?.mirror?.state?.latest || data?.mirror?.state?.latest_link || "latest-stable";
    const stablePath = data?.mirror?.state?.latest_link_exists === false ? latest : "latest-stable";
    return [
      {
        title: "Permanent file: /etc/apk/repositories",
        meta: `moving latest-stable alias, currently ${latest}`,
        code: `${origin}/alpine/${stablePath}/main\n${origin}/alpine/${stablePath}/community`,
      },
      {
        title: "Apply command",
        meta: "overwrite repositories file, then refresh indexes",
        code: `printf '%s\\n' '${origin}/alpine/${stablePath}/main' '${origin}/alpine/${stablePath}/community' > /etc/apk/repositories\napk update`,
      },
    ];
  }

  if (id === "opencsw") {
    return [
      {
        title: "Permanent file: /etc/opt/csw/pkgutil.conf",
        meta: "latest published OpenCSW current catalog",
        code: `mirror=${origin}/opencsw/current/`,
      },
      {
        title: "Apply command",
        meta: "refresh pkgutil catalog after editing the file",
        code: `mkdir -p /etc/opt/csw\nprintf '%s\\n' 'mirror=${origin}/opencsw/current/' > /etc/opt/csw/pkgutil.conf\n/opt/csw/bin/pkgutil -U`,
      },
    ];
  }

  if (id === "openbsd") {
    const latest = data?.mirror?.state?.latest || "7.x";
    const ready = latestReadyOpenBsdVersion(data);
    const arch = data?.mirror?.state?.arch || data?.mirror?.arch || "amd64";
    const host = (() => {
      try { return new URL(origin).host; } catch (error) { return window.location.host || "localhost"; }
    })();
    const readyNote = ready === latest ? `latest stable example: ${latest}/${arch}` : `latest ${latest}/${arch} not ready yet; ready example: ${ready}/${arch}`;
    return [
      {
        title: "Permanent file: /etc/installurl",
        meta: "pkg_add/sysupgrade use the client OS release from this base URL",
        code: `${origin}/openbsd`,
      },
      {
        title: "Apply command",
        meta: readyNote,
        code: `printf '%s\\n' '${origin}/openbsd' > /etc/installurl\npkg_add -u\n# installer prompt: server ${host}, server directory /openbsd\n# explicit package path example: ${origin}/openbsd/${ready}/packages/${arch}/`,
      },
    ];
  }

  if (id === "omnios") {
    const latest = data?.mirror?.state?.latest || data?.mirror?.versions?.[0] || "r1510xx";
    const latestAlias = "latest-lts";
    return [
      {
        title: "Permanent IPS publisher commands",
        meta: `moving latest-lts alias, currently ${latest}`,
        code: `pkg set-publisher -G '*' -g ${origin}/omnios/${latestAlias}/core omnios\npkg set-publisher -G '*' -g ${origin}/omnios/${latestAlias}/extra extra.omnios\npkg set-publisher -G '*' -g ${origin}/omnios/localhostomnios localhostomnios\npkg refresh --full`,
      },
      {
        title: "Verify",
        meta: "publisher configuration is stored in the IPS image",
        code: "pkg publisher",
      },
    ];
  }

  if (id === "openindiana") {
    return [
      {
        title: "Permanent IPS publisher commands",
        meta: "OpenIndiana Hipster is rolling; localhostoih is third-party SFE",
        code: `pkg set-publisher -G '*' -g ${origin}/openindiana/hipster openindiana.org\npkg set-publisher -G '*' -g ${origin}/openindiana/hipster-encumbered hipster-encumbered\npkg set-publisher -G '*' -g ${origin}/openindiana/localhostoih localhostoih\npkg refresh --full`,
      },
      {
        title: "Verify",
        meta: "publisher configuration is stored in the IPS image",
        code: "pkg publisher",
      },
    ];
  }

  const hints = clientLines(data);
  return hints.map((line) => ({ title: "Client line", meta: "repo-specific hint", code: line }));
}

function eventLines(data) {
  const lines = data?.sync?.events || data?.sync?.rsync_log_tail || data?.sync?.service_log_tail || [];
  return Array.isArray(lines) ? lines.slice(-8) : [];
}

function renderList(target, rows, emptyText) {
  const html = rows.length ? rows.join("") : `<li><span class="item-main"><span class="item-name">${escapeHtml(emptyText)}</span></span><span class="pill idle">empty</span></li>`;
  if (target.innerHTML !== html) target.innerHTML = html;
}

function buildSummary(config, data, error) {
  if (error) {
    return {
      config,
      fetchError: error.message,
      syncStatus: "error",
      badServices: 1,
      errors: 1,
      alerts: 0,
      warnings: 0,
      storage: null,
      data: null,
    };
  }

  const services = serviceStats(data.services);
  const notices = listWarnings(data);
  const coverage = coverageRows(data);
  const running = syncIsRunning(data);
  const coverageWarnings = coverage.filter((row) => {
    const status = String(row.status || "").toLowerCase();
    if (["error", "fail", "offline"].includes(status)) return true;
    if (["missing", "partial", "queued", "pending", "unknown"].includes(status)) return !running;
    return false;
  }).length;
  return {
    config,
    fetchError: null,
    syncStatus: String(data?.sync?.status || "unknown").toLowerCase(),
    badServices: services.bad,
    errors: notices.errors,
    alerts: notices.alerts,
    warnings: notices.alerts + coverageWarnings,
    storage: data.storage || null,
    data,
  };
}

function aggregateRepoUpstream(summaries) {
  let total = 0;
  let known = 0;
  for (const summary of summaries) {
    const speed = summary.data?.sync?.upstream_speed;
    if (!speed) continue;
    const value = Number(speed.bytes_per_second);
    if (!Number.isFinite(value)) continue;
    total += Math.max(value, 0);
    known += 1;
  }
  return {
    bytes_per_second: total,
    human: known ? fmtRate(total) : "-",
    known,
  };
}

function pctText(value) {
  const number = finiteNumber(value);
  return number === null ? "-" : `${number.toFixed(1)}%`;
}

function newestHostSystem(summaries) {
  const candidates = summaries
    .map((summary) => summary.data)
    .filter((data) => data?.system && (data.system.cpu || data.system.memory || data.system.swap))
    .sort((a, b) => {
      const left = Date.parse(a?.generated_at || 0) || 0;
      const right = Date.parse(b?.generated_at || 0) || 0;
      return right - left;
    });
  return candidates[0]?.system || null;
}

function cpuSummary(system) {
  const cpu = system?.cpu || {};
  const perCpu = Array.isArray(cpu.per_cpu) ? cpu.per_cpu : [];
  const vcpu = perCpu.length || finiteNumber(cpu.vcpu) || finiteNumber(cpu.count);
  let busy = finiteNumber(cpu.total_busy_pct);
  if (busy === null && perCpu.length) {
    const values = perCpu.map((item) => finiteNumber(item.busy_pct)).filter((value) => value !== null);
    if (values.length) busy = values.reduce((sum, value) => sum + value, 0) / values.length;
  }
  const perCpuLine = perCpu.slice(0, 4).map((item, index) => `${item.name || `cpu${index}`}:${pctText(item.busy_pct)}`).join(" / ");
  const extra = perCpu.length > 4 ? ` / +${perCpu.length - 4}` : "";
  return {
    value: busy === null ? "-" : `${busy.toFixed(1)}%`,
    detail: [`${vcpu || "-"} vCPU`, perCpuLine ? `${perCpuLine}${extra}` : ""].filter(Boolean).join(" / ") || "vcpu -",
  };
}

function ramSummary(system) {
  const memory = system?.memory || {};
  const swap = system?.swap || {};
  const memUsed = finiteNumber(memory.used_bytes);
  const memTotal = finiteNumber(memory.total_bytes);
  const memPct = finiteNumber(memory.used_pct);
  const swapUsed = finiteNumber(swap.used_bytes);
  const swapTotal = finiteNumber(swap.total_bytes);
  const swapPct = finiteNumber(swap.used_pct);
  const swapLine = swapTotal && swapTotal > 0
    ? `swap ${fmtBytes(swapUsed)} / ${fmtBytes(swapTotal)} (${pctText(swapPct)})`
    : "swap off";
  return {
    value: memTotal ? `${fmtBytes(memUsed)} / ${fmtBytes(memTotal)}` : "-",
    detail: [`RAM ${pctText(memPct)}`, swapLine].filter(Boolean).join(" / "),
  };
}

function renderRepo(summary) {
  const config = summary.config;
  let node = grid.querySelector(`[data-repo="${CSS.escape(config.id)}"]`);
  if (!node) {
    node = template.content.firstElementChild.cloneNode(true);
    node.dataset.repo = config.id;
    node.id = `repo-${config.id}`;
    $(".repo-title", node).textContent = config.name;
    $(".repo-id", node).textContent = config.id;
    $(".repo-link", node).href = config.repoUrl;
    grid.appendChild(node);
  }

  const data = summary.data;
  const label = healthLabel(summary);
  const hClass = healthClass(label);
  node.classList.toggle("issue", hClass === "offline");
  node.classList.toggle("warn", hClass === "idle");
  $(".repo-health", node).className = `repo-health pill ${hClass}`;
  setText($(".repo-health", node), label);

  if (!data) {
    setText($(".sync-status", node), "error");
    setText($(".sync-detail", node), summary.fetchError);
    setText($(".process", node), "-");
    setText($(".eta-detail", node), "-");
    setText($(".upstream-speed", node), "-");
    setText($(".upstream-detail", node), "no status data");
    setText($(".storage-used", node), "-");
    setText($(".storage-free", node), "-");
    setText($(".service-score", node), "0/1");
    setText($(".generated-at", node), "API failed");
    $(".progress-bar", node).style.width = "0%";
    setText($(".plan-line", node), "status API unavailable");
    setText($(".source-line", node), config.apiUrl);
    renderList($(".coverage-list", node), [], "no status data");
    renderList($(".service-list", node), [`<li><span class="item-main"><span class="item-name">status api</span><span class="meta">${escapeHtml(summary.fetchError)}</span></span><span class="pill offline">offline</span></li>`], "no services");
    setText($(".events", node), summary.fetchError);
    renderList($(".client-list", node), [], "no client hints");
    return;
  }

  const services = serviceStats(data.services);
  const sync = data.sync || {};
  const detail = syncDetail(data);
  const upstream = upstreamSpeed(data);
  const progress = progressPercent(data);
  const pct = progress === null ? null : Math.max(0, Math.min(progress, 100));
  const coverage = coverageRows(data);
  const clients = clientApplyRows(config, data);
  const storage = storageLines(config, data);
  const eta = repoEtaLine(data, pct);

  setText($(".sync-status", node), String(sync.status || "unknown").toUpperCase());
  setText($(".sync-detail", node), syncDetailText(detail));
  setText($(".process", node), eta.value);
  setText($(".eta-detail", node), eta.detail);
  setText($(".upstream-speed", node), upstream.speed);
  setText($(".upstream-detail", node), upstream.detail);
  setText($(".storage-used", node), storage.used);
  setText($(".storage-free", node), storage.detail);
  setText($(".service-score", node), `${services.total - services.bad}/${services.total}`);
  setText($(".generated-at", node), fmtDate(data.generated_at));
  const progressBar = $(".progress-bar", node);
  const isIndeterminate = pct === null;
  progressBar.classList.toggle("indeterminate", isIndeterminate);
  progressBar.style.width = isIndeterminate ? "100%" : `${pct}%`;
  progressBar.title = isIndeterminate ? "running; exact progress is not available" : `whole repo progress ${pct.toFixed(1)}%`;
  setText($(".plan-line", node), planLine(config, data));
  setText($(".source-line", node), sourceLine(data));
  setText($(".coverage-count", node), `${coverage.length} items`);

  renderList($(".coverage-list", node), coverage.map((item) =>
    `<li><span class="item-main"><span class="item-name">${escapeHtml(item.name)}</span><span class="meta">${escapeHtml(item.meta || "-")}</span></span><span class="pill ${statusClass(item.status)}">${escapeHtml(item.status)}</span></li>`
  ), "no coverage rows");

  renderList($(".service-list", node), services.entries.map(([name, state]) =>
    `<li><span class="item-main"><span class="item-name">${escapeHtml(name)}</span></span><span class="pill ${statusClass(state)}">${escapeHtml(state)}</span></li>`
  ), "no service rows");

  setText($(".events", node), eventLines(data).join("\n") || "no recent events");

  renderList($(".client-list", node), clients.map((item) =>
    `<li class="client-item"><span class="item-main"><span class="item-name">${escapeHtml(item.title)}</span><span class="meta">${escapeHtml(item.meta || "")}</span><code class="client-code">${escapeHtml(item.code)}</code></span></li>`
  ), "no client hints");
}

function renderTop(summaries) {
  const count = summaries.length;
  const running = summaries.filter((item) => item.syncStatus === "running").length;
  const critical = summaries.filter((item) => item.fetchError || item.badServices > 0 || item.errors > 0).length;
  const attention = summaries.filter((item) => item.fetchError || item.badServices > 0 || item.errors > 0 || item.warnings > 0).length;
  const healthy = summaries.filter((item) => !item.fetchError && item.badServices === 0 && item.errors === 0 && item.warnings === 0).length;
  const storage = summaries.find((item) => item.storage)?.storage;
  const upstream = aggregateRepoUpstream(summaries);
  const system = newestHostSystem(summaries);
  const cpu = cpuSummary(system);
  const ram = ramSummary(system);

  setText($("#repo-count"), count);
  setText($("#healthy-count"), healthy);
  setText($("#running-count"), running);
  setText($("#issue-count"), attention);
  setText($("#sync-speed"), upstream.human);
  setText($("#cpu-summary"), cpu.value);
  setText($("#cpu-detail"), cpu.detail);
  setText($("#ram-summary"), ram.value);
  setText($("#ram-detail"), ram.detail);
  setText($("#storage-summary"), storage ? `${storage.used_human || fmtBytes(storage.used_bytes)} / ${storage.total_human || fmtBytes(storage.total_bytes)}` : "-");
  setText($("#clock"), fmtDate(new Date().toISOString()));
  const state = critical ? "critical" : attention ? "attention" : running ? "syncing" : "live";
  $("#refresh-state").className = `pill ${critical ? "offline" : attention ? "idle" : running ? "running" : "ok"}`;
  setText($("#refresh-state"), state);
}

async function refresh() {
  const results = await Promise.all(repoConfig.map(async (config) => {
    try {
      return buildSummary(config, await getJson(config.apiUrl), null);
    } catch (error) {
      return buildSummary(config, null, error);
    }
  }));
  for (const summary of results) renderRepo(summary);
  renderTop(results);
}

async function init() {
  await loadConfig();
  $("#limit-toggle")?.addEventListener("click", toggleLimit);
  $("#limit-rate-mbps")?.addEventListener("input", updateLimitButton);
  $("#limit-rate-mbps")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      toggleLimit();
    }
  });
  await Promise.allSettled([refreshTraffic(), refresh()]);
  setInterval(refreshTraffic, 15000);
  setInterval(refresh, 5000);
}

init().catch((error) => {
  $("#refresh-state").className = "pill offline";
  setText($("#refresh-state"), error.message);
});
