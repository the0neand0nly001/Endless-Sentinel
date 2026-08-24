(() => {
  "use strict";

  const dashboard = document.querySelector("[data-dashboard]");
  if (!dashboard) return;

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const statusClasses = ["status-healthy", "status-warning", "status-critical", "status-disabled", "status-unknown", "status-initializing"];
  const severityOrder = { critical: 0, warning: 1, healthy: 2 };
  const refreshSeconds = Math.max(5, Number($("meta[name='endless-sentinel-refresh']")?.content || 10));
  let refreshTimer = null;

  function setText(selector, value, root = document) {
    const element = $(selector, root);
    if (element) element.textContent = String(value);
  }

  function clamp(value) {
    return Math.max(0, Math.min(100, Number(value) || 0));
  }

  function percent(value) {
    return `${(Number(value) || 0).toFixed(1)}%`;
  }

  function bytes(value) {
    let size = Number(value) || 0;
    const units = ["B", "KB", "MB", "GB", "TB"];
    let index = 0;
    while (Math.abs(size) >= 1024 && index < units.length - 1) {
      size /= 1024;
      index += 1;
    }
    return index === 0 ? `${Math.round(size)} B` : `${size.toFixed(1)} ${units[index]}`;
  }

  function formatTimestamp(value, relative = false) {
    if (!value) return "Waiting for first cycle";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    if (relative) {
      const seconds = Math.round((date.getTime() - Date.now()) / 1000);
      const absolute = Math.abs(seconds);
      const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
      if (absolute < 60) return formatter.format(seconds, "second");
      if (absolute < 3600) return formatter.format(Math.round(seconds / 60), "minute");
      if (absolute < 86400) return formatter.format(Math.round(seconds / 3600), "hour");
      return formatter.format(Math.round(seconds / 86400), "day");
    }
    return new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date).replace(",", " ·");
  }

  function applyStatus(element, status) {
    if (!element) return;
    element.classList.remove(...statusClasses);
    element.classList.add(`status-${status || "unknown"}`);
  }

  function updateFavicon(status) {
    const colors = { healthy: "#00b37e", warning: "#f4b860", critical: "#ff7a7a", initializing: "#54d6ad", unknown: "#829296" };
    const color = colors[status] || colors.unknown;
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="16" fill="#171719"/><rect x="7" y="7" width="50" height="50" rx="12" fill="none" stroke="${color}" stroke-opacity=".36"/><g fill="${color}"><rect x="18" y="34" width="7" height="13" rx="3.5" opacity=".58"/><rect x="29" y="16" width="7" height="31" rx="3.5"/><rect x="40" y="26" width="7" height="21" rx="3.5" opacity=".78"/></g></svg>`;
    const favicon = $("#dynamic-favicon");
    if (favicon) favicon.href = `data:image/svg+xml,${encodeURIComponent(svg)}`;
  }

  function showToast(message, type = "success") {
    const stack = $("[data-toast-stack]");
    if (!stack) return;
    const toast = document.createElement("div");
    toast.className = `toast-message flash-${type}`;
    toast.setAttribute("role", type === "error" ? "alert" : "status");
    const icon = document.createElement("span");
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = type === "success" ? "✓" : type === "warning" ? "!" : "×";
    const copy = document.createElement("p");
    copy.textContent = message;
    const close = document.createElement("button");
    close.type = "button";
    close.setAttribute("aria-label", "Dismiss notification");
    close.textContent = "×";
    close.addEventListener("click", () => toast.remove());
    toast.append(icon, copy, close);
    stack.append(toast);
    window.setTimeout(() => toast.remove(), 5000);
  }

  function resourceRow(text, tone = "healthy") {
    const row = document.createElement("span");
    const dot = document.createElement("i");
    dot.className = tone === "critical" ? "critical-dot" : tone === "warning" ? "warn-dot" : "good-dot";
    row.append(dot, document.createTextNode(text));
    return row;
  }

  function setResourceRows(card, rows, emptyMessage) {
    const list = $("[data-resource-list]", card);
    if (!list) return;
    list.replaceChildren();
    if (!rows.length) {
      const empty = document.createElement("span");
      empty.textContent = emptyMessage;
      list.append(empty);
      return;
    }
    rows.slice(0, 4).forEach((row) => list.append(resourceRow(row.text, row.tone)));
  }

  function updateProxmox(service) {
    const card = $("[data-platform='proxmox']");
    if (!card) return;
    const summary = service.summary || {};
    const status = service.status || "unknown";
    applyStatus($("[data-platform-status]", card), status);
    setText("[data-platform-status] span", status.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase()), card);
    setText("[data-platform-summary]", service.enabled ? `${summary.online_nodes || 0} of ${summary.total_nodes || 0} nodes online` : "Collector disabled", card);
    setText("[data-metric='cpu']", percent(summary.avg_cpu), card);
    setText("[data-metric='memory']", percent(summary.avg_memory), card);
    $("[data-progress='cpu']", card).style.width = `${clamp(summary.avg_cpu)}%`;
    $("[data-progress='memory']", card).style.width = `${clamp(summary.avg_memory)}%`;
    const rows = (service.nodes || []).map((node) => ({ text: `${node.name} · ${percent(node.cpu_percent)} CPU · ${percent(node.memory_percent)} RAM`, tone: node.online ? "healthy" : "critical" }));
    setResourceRows(card, rows, "No Proxmox node telemetry received.");
    updatePlatformError(card, service.error);
  }

  function updateK3s(service) {
    const card = $("[data-platform='k3s']");
    if (!card) return;
    const summary = service.summary || {};
    const status = service.status || "unknown";
    const nodePercent = summary.total_nodes ? (summary.ready_nodes / summary.total_nodes) * 100 : 0;
    const podPercent = summary.total_pods ? (summary.healthy_pods / summary.total_pods) * 100 : 0;
    applyStatus($("[data-platform-status]", card), status);
    setText("[data-platform-status] span", status.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase()), card);
    setText("[data-platform-summary]", service.enabled ? `${summary.healthy_pods || 0} of ${summary.total_pods || 0} pods healthy` : "Collector disabled", card);
    setText("[data-metric='nodes']", `${summary.ready_nodes || 0} / ${summary.total_nodes || 0}`, card);
    setText("[data-metric='pods']", percent(podPercent), card);
    $("[data-progress='nodes']", card).style.width = `${clamp(nodePercent)}%`;
    $("[data-progress='pods']", card).style.width = `${clamp(podPercent)}%`;
    const unhealthy = (service.pods || []).filter((pod) => pod.severity !== "healthy");
    const rows = unhealthy.map((pod) => ({ text: `${pod.namespace}/${pod.name} · ${pod.reason || pod.phase}`, tone: pod.severity }));
    const empty = summary.total_pods ? "All observed k3s pods are healthy." : "No k3s workload telemetry received.";
    setResourceRows(card, rows, empty);
    updatePlatformError(card, service.error);
  }

  function updateDocker(service) {
    const card = $("[data-platform='docker']");
    if (!card) return;
    const summary = service.summary || {};
    const status = service.status || "unknown";
    applyStatus($("[data-platform-status]", card), status);
    setText("[data-platform-status] span", status.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase()), card);
    setText("[data-platform-summary]", service.enabled ? `${summary.running_containers || 0} of ${summary.total_containers || 0} containers running` : "Collector disabled", card);
    setText("[data-metric='memory']", bytes(summary.memory_bytes), card);
    setText("[data-metric='restarts']", summary.total_restarts || 0, card);
    $("[data-progress='memory']", card).style.width = `${clamp(summary.memory_percent)}%`;
    $("[data-progress='restarts']", card).style.width = `${clamp((summary.total_restarts || 0) * 5)}%`;
    const rows = (service.containers || []).map((container) => ({ text: `${container.name} · ${container.status}${container.health ? ` · ${container.health}` : ""}`, tone: container.healthy ? "healthy" : "critical" }));
    setResourceRows(card, rows, "No Docker container telemetry received.");
    updatePlatformError(card, service.error);
  }

  function updatePlatformError(card, error) {
    let element = $("[data-platform-error]", card);
    if (!error) {
      if (element) element.remove();
      return;
    }
    if (!element) {
      element = document.createElement("p");
      element.className = "inline-error";
      element.dataset.platformError = "";
      card.append(element);
    }
    element.textContent = error;
  }

  function drawChart(history) {
    const points = (history || []).slice(-24);
    const cpuLine = $("[data-cpu-line]");
    const cpuFill = $("[data-cpu-fill]");
    const ramLine = $("[data-ram-line]");
    const ramFill = $("[data-ram-fill]");
    const axis = $("[data-chart-axis]");
    if (!cpuLine || !cpuFill || !ramLine || !ramFill || !axis) return;
    const usable = points.length === 1 ? [points[0], points[0]] : points;
    const pathFor = (key) => usable.map((point, index) => {
      const x = usable.length > 1 ? (index / (usable.length - 1)) * 620 : 0;
      const y = 172 - clamp(point[key]) * 1.55;
      return `${index ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`;
    }).join(" ");
    const cpuPath = usable.length ? pathFor("proxmox_cpu") : "M0 172 L620 172";
    const ramPath = usable.length ? pathFor("proxmox_memory") : "M0 172 L620 172";
    cpuLine.setAttribute("d", cpuPath);
    ramLine.setAttribute("d", ramPath);
    cpuFill.setAttribute("d", `${cpuPath} L620 180 L0 180 Z`);
    ramFill.setAttribute("d", `${ramPath} L620 180 L0 180 Z`);
    axis.replaceChildren();
    const labels = points.length > 1 ? [points[0], points[Math.floor((points.length - 1) / 2)], points[points.length - 1]] : [];
    if (!labels.length) {
      ["Awaiting history", "Current cycle"].forEach((label) => { const span = document.createElement("span"); span.textContent = label; axis.append(span); });
    } else {
      labels.forEach((point) => { const span = document.createElement("span"); span.textContent = formatTimestamp(point.timestamp).split(" · ").pop(); axis.append(span); });
    }
  }

  function renderAlerts(alerts) {
    const list = $("[data-alert-list]");
    if (!list) return;
    const ordered = [...(alerts || [])].sort((a, b) => (severityOrder[a.severity] ?? 9) - (severityOrder[b.severity] ?? 9));
    list.replaceChildren();
    if (!ordered.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      const icon = document.createElement("span");
      icon.className = "empty-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = "✓";
      const copy = document.createElement("div");
      const title = document.createElement("h3");
      title.textContent = "No open incidents";
      const text = document.createElement("p");
      text.textContent = "New threshold, availability, health, and restart alerts will appear here.";
      copy.append(title, text);
      empty.append(icon, copy);
      list.append(empty);
      return;
    }
    ordered.forEach((alert) => {
      const row = document.createElement("article");
      row.className = "incident-row";
      const level = document.createElement("span");
      level.className = `incident-level level-${alert.severity}`;
      level.textContent = String(alert.severity || "warning").charAt(0).toUpperCase();
      const copy = document.createElement("div");
      copy.className = "incident-copy";
      const meta = document.createElement("div");
      const pill = document.createElement("span");
      pill.className = `status-pill status-${alert.severity}`;
      const dot = document.createElement("i");
      pill.append(dot, document.createTextNode(alert.source || "Endless Sentinel"));
      const time = document.createElement("time");
      time.dateTime = alert.last_seen || "";
      time.textContent = formatTimestamp(alert.last_seen, true);
      meta.append(pill, time);
      const title = document.createElement("h3");
      title.textContent = alert.title || "Monitored resource changed state";
      const message = document.createElement("p");
      message.textContent = alert.message || "Review this resource.";
      copy.append(meta, title, message);
      const review = document.createElement("a");
      review.href = "#infrastructure";
      review.append(document.createTextNode("Review "), Object.assign(document.createElement("span"), { textContent: "→" }));
      row.append(level, copy, review);
      list.append(row);
    });
  }

  function renderState(state) {
    if (!state?.services) return;
    const status = state.overall_status || "unknown";
    dashboard.dataset.initialStatus = status;
    updateFavicon(status);
    const overallPill = $("[data-overall-pill]");
    applyStatus(overallPill, status);
    setText("[data-overall-label]", status.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase()));
    setText("[data-attention-count]", state.summary.active_alerts || 0);
    setText("[data-attention-label]", state.summary.active_alerts === 1 ? "active incident" : "active incidents");
    const overallCopy = status === "healthy" ? "All configured infrastructure layers are reporting healthy." : status === "warning" || status === "critical" ? "Review the active alerts below for resources that need attention." : status === "initializing" ? "Collectors are starting and the first telemetry cycle is in progress." : "Enable at least one collector to begin receiving homelab telemetry.";
    setText("[data-overall-copy]", overallCopy);
    setText("[data-updated-at]", formatTimestamp(state.updated_at));
    setText("[data-nodes-ready]", state.summary.nodes_ready || 0);
    setText("[data-nodes-total]", state.summary.nodes_total || 0);
    setText("[data-nodes-copy]", state.summary.nodes_total ? `${state.summary.nodes_total - state.summary.nodes_ready} unavailable` : "No node data yet");
    setText("[data-workloads-healthy]", state.summary.workloads_healthy || 0);
    setText("[data-workloads-total]", state.summary.workloads_total || 0);
    setText("[data-workloads-copy]", state.summary.workloads_total ? `${state.summary.workloads_total - state.summary.workloads_healthy} degraded` : "No workload data yet");
    setText("[data-alert-count]", state.summary.active_alerts || 0);
    setText("[data-alert-copy]", state.summary.active_alerts ? "Sorted by severity below" : "No open incidents");
    setText("[data-chart-cpu]", percent(state.services.proxmox.summary?.avg_cpu));
    setText("[data-chart-memory]", percent(state.services.proxmox.summary?.avg_memory));
    const collectorLight = $("[data-collector-light]");
    if (collectorLight) collectorLight.className = `pulse status-light-${status}`;
    setText("[data-collector-label]", state.polling ? "Collector running" : state.updated_at ? "Collector online" : "Collector starting");

    const notifier = state.notifier || {};
    const notifierTone = notifier.status === "ready" ? "healthy" : notifier.status === "error" ? "critical" : "disabled";
    applyStatus($("[data-notifier-pill]"), notifierTone);
    setText("[data-notifier-label]", String(notifier.status || "disabled").replace(/^./, (letter) => letter.toUpperCase()));
    setText("[data-notifier-copy]", notifier.configured ? "Threshold alerts are grouped, deduplicated, and dispatched through your configured webhook." : "Add DISCORD_WEBHOOK_URL to enable threshold and recovery notifications.");
    setText("[data-last-delivery]", notifier.last_delivery_at ? formatTimestamp(notifier.last_delivery_at, true) : "No deliveries yet");

    updateProxmox(state.services.proxmox);
    updateK3s(state.services.k3s);
    updateDocker(state.services.docker);
    drawChart(state.history);
    renderAlerts(state.alerts);
  }

  async function refreshState({ quiet = true } = {}) {
    try {
      const response = await fetch("/api/status", { headers: { Accept: "application/json" }, cache: "no-store" });
      if (!response.ok) throw new Error(`Status request failed (${response.status})`);
      renderState(await response.json());
    } catch (error) {
      if (!quiet) showToast(error.message || "Could not refresh dashboard telemetry.", "error");
    }
  }

  async function submitAction(form) {
    const action = form.dataset.actionForm;
    const button = $("button", form);
    const pollLabel = $("[data-poll-label]", form);
    const icon = $(".scan-icon", form);
    if (button) button.disabled = true;
    if (action === "poll") {
      if (pollLabel) pollLabel.textContent = "Scanning…";
      if (icon) icon.classList.add("spinning");
    }
    try {
      const response = await fetch(form.action, { method: "POST", headers: { Accept: "application/json", "X-Requested-With": "fetch" } });
      const payload = await response.json().catch(() => ({ ok: false, message: "The action returned an invalid response." }));
      if (!response.ok || !payload.ok) throw new Error(payload.message || `Action failed (${response.status})`);
      if (payload.state) renderState(payload.state);
      showToast(payload.message || "Action completed successfully.", "success");
    } catch (error) {
      showToast(error.message || "The action could not be completed.", "error");
    } finally {
      if (button) button.disabled = false;
      if (pollLabel) pollLabel.textContent = "Run scan";
      if (icon) icon.classList.remove("spinning");
    }
  }

  const menuToggle = $("[data-menu-toggle]");
  const sidebar = $("[data-sidebar]");
  const scrim = $("[data-menu-scrim]");
  const setMenu = (open) => {
    sidebar?.classList.toggle("sidebar-open", open);
    if (menuToggle) menuToggle.setAttribute("aria-expanded", String(open));
    if (scrim) scrim.hidden = !open;
    document.body.classList.toggle("menu-open", open);
  };
  menuToggle?.addEventListener("click", () => setMenu(menuToggle.getAttribute("aria-expanded") !== "true"));
  scrim?.addEventListener("click", () => setMenu(false));
  $$(".sidebar nav a").forEach((link) => link.addEventListener("click", () => setMenu(false)));
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") setMenu(false); });

  $$('[data-action-form]').forEach((form) => form.addEventListener("submit", (event) => { event.preventDefault(); submitAction(form); }));
  $$('[data-dismiss-flash]').forEach((button) => button.addEventListener("click", () => button.closest(".flash")?.remove()));

  const sections = ["overview", "infrastructure", "alerts"].map((id) => document.getElementById(id)).filter(Boolean);
  if ("IntersectionObserver" in window) {
    const observer = new IntersectionObserver((entries) => {
      const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (!visible) return;
      $$(".sidebar nav a").forEach((link) => link.classList.toggle("active", link.getAttribute("href") === `#${visible.target.id}`));
    }, { rootMargin: "-20% 0px -68%", threshold: [0, 0.15, 0.5] });
    sections.forEach((section) => observer.observe(section));
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshState({ quiet: true });
  });
  updateFavicon(dashboard.dataset.initialStatus);
  refreshState({ quiet: true });
  refreshTimer = window.setInterval(() => { if (!document.hidden) refreshState({ quiet: true }); }, refreshSeconds * 1000);
  window.addEventListener("beforeunload", () => window.clearInterval(refreshTimer), { once: true });
})();
