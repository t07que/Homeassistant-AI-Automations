/* global jsyaml, CodeMirror, Diff */

const $ = (id) => document.getElementById(id);

function isAutomation() {
  return state.entityType !== "script";
}

function entityLabel() {
  return isAutomation() ? "automation" : "script";
}

function entityLabelPlural() {
  return isAutomation() ? "automations" : "scripts";
}

function entityTitle() {
  return isAutomation() ? "Automation" : "Script";
}

function entityEndpoint() {
  return isAutomation() ? "/api/automations" : "/api/scripts";
}

const state = {
  q: "",
  list: [],
  entityType: "automation",
  activeId: null,
  active: null,
  originalYaml: "",
  currentDraft: "",
  dirty: false,
  editor: null,
  aiMode: "architect", // or "improve" / "rewrite"
  viewMode: "yaml",
  versions: [],
  selectedVersionId: null,
  selectedVersionYaml: "",
  latestVersionId: null,
  latestVersionYaml: "",
  previousSavedYaml: "",
  compareTarget: "current",
  sidebarCollapsed: false,
  railCollapsed: false,
  aiCollapsed: false,
  capabilitiesView: false,
  capabilitiesSnapshot: null,
  capabilitiesYaml: "",
  capabilitiesCache: null,
  health: null,
  healthLoading: false,
  scenarioResult: null,
  scenarioRunning: false,
  kbSyncRunning: false,
  diffMarks: [],
  lastAiPrompt: "",
  aiHistory: [],
  createHistory: [],
  suppressChange: false,
  tabs: [],
  tabCache: {},
  tabsByType: { automation: [], script: [] },
  tabCacheByType: { automation: {}, script: {} },
  lastActiveByType: { automation: null, script: null },
  aiOutputHome: null,
  promptSyncing: false,
  _sizingEditor: false,
  architectConversationId: null,
  architectContextSent: false,
  createArchitectConversationId: null,
  combineSelectionIds: [],
  usageSnapshot: null,
};

// Temporary safety switches for remote access issues.
const DISABLE_HEALTH_PANEL = true;
const DISABLE_SEMANTIC_DIFF = false;

const SEARCH_DEBOUNCE_MS = 350;
let debouncedLoadList = null;
let listRequestSeq = 0;

const DEFAULT_SETTINGS = {
  viewMode: "yaml",
  compareTarget: "current",
  compactList: false,
  hideDisabled: false,
  autoOpenNew: true,
  filePath: "",
  invertPromptEnter: false,
  tryLocalEditFirst: true,
  allowAiDiff: false,
  helperMinConfidence: 0.55,
  usageCurrency: "GBP",
};
let settings = { ...DEFAULT_SETTINGS };
let collapsedCards = new Set();
let hiddenCards = new Set();
let _enableTimer;
let _confirmResolver;
const AI_COLLAPSE_KEY = "ai_section_collapsed";
const HIDDEN_CARDS_KEY = "hidden_cards_v1";
const VIEW_MENU_CARD_IDS = ["activity", "usage", "versions"];

const LAYOUT_STORAGE_KEY = "ui_layout_v1";
const DEFAULT_LAYOUT = {
  rightRailWidth: 320,
  sizes: {},
  order: {
    aiRow: ["ai_assist", "ai_output"],
    rightRail: ["activity", "usage", "versions"],
  },
  columnSplit: { editorPct: 0.6 },
};
let layout = loadLayout();
let _activeResizeSection = null;
let _resizeDrag = null;
const RUNTIME_MODEL_FIELDS = [
  { role: "builder", key: "builder_model", selectId: "settingsBuilderModel" },
  { role: "architect", key: "architect_model", selectId: "settingsArchitectModel" },
  { role: "editor", key: "editor_model", selectId: "settingsEditorModel" },
  { role: "summary", key: "summary_model", selectId: "settingsSummaryModel" },
  { role: "capability_mapper", key: "capability_mapper_model", selectId: "settingsCapabilityMapperModel" },
  { role: "semantic_diff", key: "semantic_diff_model", selectId: "settingsSemanticDiffModel" },
  { role: "kb_sync_helper", key: "kb_sync_helper_model", selectId: "settingsKbSyncModel" },
  { role: "dumb_builder", key: "dumb_builder_model", selectId: "settingsDumbBuilderModel" },
];

function log(msg) {
  const box = $("logBox");
  const line = `[${new Date().toLocaleTimeString()}] ${msg}\n`;
  box.textContent = (box.textContent + line).slice(-8000);
  box.scrollTop = box.scrollHeight;
}

function toLineArray(text) {
  if (!text) return [];
  const lines = String(text).split(/\r?\n/);
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

function countLines(text) {
  return toLineArray(text).length;
}

function sizeEditorToWrap() {
  if (!state.editor) return;
  if (state._sizingEditor) return;
  state._sizingEditor = true;
  requestAnimationFrame(() => {
    const wrap = document.querySelector(".editor-wrap");
    if (wrap) {
      const h = Math.max(200, wrap.clientHeight);
      state.editor.setSize("100%", `${h}px`);
    }
    state._sizingEditor = false;
  });
}

function setEditorValue(text) {
  if (!state.editor) return;
  state.suppressChange = true;
  state.editor.setValue(text || "");
  state.suppressChange = false;
  sizeEditorToWrap();
}

function setEditorReadOnly(isReadOnly) {
  if (!state.editor) return;
  state.editor.setOption("readOnly", isReadOnly ? "nocursor" : false);
}

function getCurrentDraftText() {
  if (state.compareTarget === "current" && state.editor) return state.editor.getValue();
  if (state.currentDraft !== null && state.currentDraft !== undefined) return state.currentDraft;
  return state.editor ? state.editor.getValue() : "";
}

function toast(msg, ms = 2200) {
  const t = $("toast");
  t.textContent = msg;
  t.style.display = "block";
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.style.display = "none"), ms);
}

function formatAgentStatusList(list) {
  if (!Array.isArray(list) || !list.length) return "";
  return list
    .map((item) => {
      const name = item?.name || item?.agent_id || "agent";
      const status = item?.ok ? "ok" : "failed";
      const detail = item?.detail ? ` (${item.detail})` : "";
      return `${name}: ${status}${detail}`;
    })
    .join(", ");
}

function formatMoney(amount, currency = "GBP") {
  const value = Number(amount);
  if (!Number.isFinite(value)) return `${currency} 0.00000`;
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency,
      minimumFractionDigits: value >= 1 ? 2 : 5,
      maximumFractionDigits: value >= 1 ? 2 : 5,
    }).format(value);
  } catch (err) {
    return `${currency} ${value.toFixed(value >= 1 ? 2 : 5)}`;
  }
}

function formatUsageEvent(ev, fallbackCurrency = "GBP") {
  const name = ev?.name || ev?.agent_id || "agent";
  const model = ev?.model || "model";
  const promptTokens = Number(ev?.prompt_tokens) || 0;
  const completionTokens = Number(ev?.completion_tokens) || 0;
  const totalTokens = Number.isFinite(Number(ev?.total_tokens))
    ? Number(ev?.total_tokens)
    : promptTokens + completionTokens;
  const hasConvertedCost = Number.isFinite(Number(ev?.cost));
  const currency = String(ev?.currency || (hasConvertedCost ? fallbackCurrency : "USD") || "GBP").toUpperCase();
  const amount = hasConvertedCost ? Number(ev?.cost) : Number(ev?.cost_usd) || 0;
  const cost = formatMoney(amount, currency);
  return `${name} (${model}): ${promptTokens} in + ${completionTokens} out = ${totalTokens} tokens (~${cost})`;
}

function appendUsage(out, label = "Usage") {
  const events = Array.isArray(out?.usage_events) ? out.usage_events : [];
  const total = out?.usage_total;
  if (!events.length && !total) return;
  updateUsageSnapshot(events, total, label);
  const fallbackCurrency = String(total?.currency || settings.usageCurrency || "GBP").toUpperCase();
  events.forEach((ev) => log(`${label}: ${formatUsageEvent(ev, fallbackCurrency)}`));
  if (total) {
    const promptTokens = Number(total?.prompt_tokens) || 0;
    const completionTokens = Number(total?.completion_tokens) || 0;
    const totalTokens = Number.isFinite(Number(total?.total_tokens))
      ? Number(total?.total_tokens)
      : promptTokens + completionTokens;
    const hasConvertedCost = Number.isFinite(Number(total?.cost));
    const currency = String(total?.currency || (hasConvertedCost ? fallbackCurrency : "USD")).toUpperCase();
    const amount = hasConvertedCost ? Number(total?.cost) : Number(total?.cost_usd) || 0;
    const cost = currency === "MIXED" ? `${amount.toFixed(5)} (mixed currency)` : formatMoney(amount, currency);
    log(`${label} total: ${promptTokens} in + ${completionTokens} out = ${totalTokens} tokens (~${cost})`);
  }
}

function updateUsageSnapshot(events, total, label = "Usage") {
  const list = Array.isArray(events) ? events : [];
  const mapped = list
    .map((ev) => {
      const name = String(ev?.name || ev?.agent_id || "agent");
      const model = String(ev?.model || "");
      const totalTokens = Number.isFinite(Number(ev?.total_tokens))
        ? Number(ev?.total_tokens)
        : (Number(ev?.prompt_tokens) || 0) + (Number(ev?.completion_tokens) || 0);
      return { name, model, totalTokens: Math.max(0, Math.round(totalTokens)) };
    })
    .filter((ev) => ev.totalTokens > 0)
    .sort((a, b) => b.totalTokens - a.totalTokens);

  const promptTokens = Number(total?.prompt_tokens) || 0;
  const completionTokens = Number(total?.completion_tokens) || 0;
  const totalTokens = Number.isFinite(Number(total?.total_tokens))
    ? Number(total?.total_tokens)
    : promptTokens + completionTokens;
  const hasConvertedCost = Number.isFinite(Number(total?.cost));
  const currency = String(total?.currency || settings.usageCurrency || "GBP").toUpperCase();
  const amount = hasConvertedCost ? Number(total?.cost) : Number(total?.cost_usd) || 0;

  state.usageSnapshot = {
    label: String(label || "Usage"),
    at: Date.now(),
    events: mapped,
    totalTokens: Math.max(0, Math.round(totalTokens)),
    promptTokens: Math.max(0, Math.round(promptTokens)),
    completionTokens: Math.max(0, Math.round(completionTokens)),
    currency,
    amount,
  };
  renderUsageChart();
}

function appendAgentStatus(out, targetAppend, statusId, label = "Helpers") {
  const list = Array.isArray(out?.agent_status) ? out.agent_status : [];
  if (!list.length) return;
  const msg = formatAgentStatusList(list);
  if (!msg) return;
  if (typeof targetAppend === "function") {
    targetAppend(`${label} status: ${msg}`, "system");
  }
  if (statusId) {
    const hasSummary = list.some((item) => item?.name === "summary");
    const hasKb = list.some((item) => item?.name === "kb_sync_helper");
    if (hasSummary) {
      setStatus(statusId, "summary", agentStatusText("summary", "summarizing..."));
    } else if (hasKb) {
      setStatus(statusId, "kb", agentStatusText("kb_sync_helper", "updating..."));
    } else {
      setStatus(statusId, "architect", `${label} status: ${msg}`);
    }
  }
}

function handleCapabilitiesUpdated(out) {
  if (!out) return;
  const context = Array.isArray(out.saved_context) ? out.saved_context.filter(Boolean) : [];
  const entities = Array.isArray(out.saved_entities) ? out.saved_entities.filter(Boolean) : [];
  const scripts = Array.isArray(out.saved_scripts) ? out.saved_scripts.filter(Boolean) : [];
  const notes = Array.isArray(out.saved_notes) ? out.saved_notes.filter(Boolean) : [];
  if (!context.length && !entities.length && !scripts.length && !notes.length) return;
  const parts = [];
  if (context.length) parts.push(context.join("/"));
  if (entities.length) parts.push(`${entities.length} entities`);
  if (scripts.length) parts.push(`${scripts.length} scripts`);
  if (notes.length) parts.push(`${notes.length} notes`);
  toast(`Learned: ${parts.join(" | ")}`, 3200);
  const detail = [];
  if (context.length) detail.push(`context=[${context.join(", ")}]`);
  if (entities.length) detail.push(`entities=[${entities.join(", ")}]`);
  if (scripts.length) detail.push(`scripts=[${scripts.join(", ")}]`);
  if (notes.length) detail.push(`notes=[${notes.join(", ")}]`);
  log(`Capabilities updated: ${detail.join(" ")}.`);
}

function resolveConfirm(result) {
  if (typeof _confirmResolver !== "function") return;
  const fn = _confirmResolver;
  _confirmResolver = null;
  fn(result);
}

function openConfirmModal({ title, subtitle, message, confirmText, cancelText, confirmClass, secondaryText, secondaryClass } = {}) {
  const modal = $("confirmModal");
  if (!modal) return Promise.resolve(false);
  const titleEl = $("confirmTitle");
  const subtitleEl = $("confirmSubtitle");
  const messageEl = $("confirmMessage");
  const okBtn = $("confirmOkBtn");
  const cancelBtn = $("confirmCancelBtn");
  const altBtn = $("confirmAltBtn");

  if (titleEl) titleEl.textContent = title || "Confirm";
  if (subtitleEl) subtitleEl.textContent = subtitle || "Please confirm this action.";
  if (messageEl) messageEl.textContent = message || "";
  if (okBtn) okBtn.textContent = confirmText || "Confirm";
  if (cancelBtn) cancelBtn.textContent = cancelText || "Cancel";
  if (altBtn) {
    altBtn.textContent = secondaryText || "Secondary";
    altBtn.hidden = !secondaryText;
    altBtn.classList.remove("danger", "primary");
    if (secondaryClass) {
      altBtn.classList.toggle("danger", secondaryClass === "danger");
      altBtn.classList.toggle("primary", secondaryClass === "primary");
    }
  }
  if (okBtn) {
    okBtn.classList.toggle("danger", confirmClass === "danger");
    okBtn.classList.toggle("primary", confirmClass !== "danger");
  }

  if (_confirmResolver) resolveConfirm(false);
  return new Promise((resolve) => {
    _confirmResolver = resolve;
    if (okBtn) {
      okBtn.onclick = () => {
        resolveConfirm(true);
        closeModal("confirmModal");
      };
    }
    if (cancelBtn) {
      cancelBtn.onclick = () => {
        resolveConfirm(false);
        closeModal("confirmModal");
      };
    }
    if (altBtn) {
      altBtn.onclick = () => {
        resolveConfirm("secondary");
        closeModal("confirmModal");
      };
    }
    openModal("confirmModal");
  });
}

function aiOutputClear() {
  const boxes = [$("aiOutput"), $("architectOutputExpanded")].filter(Boolean);
  boxes.forEach((box) => {
    box.innerHTML = "";
  });
}

function formatInline(text) {
  let safe = escapeHtml(text || "");
  safe = safe.replace(/##(.+?)##/g, "<strong>$1</strong>");
  safe = safe.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  safe = safe.replace(/`([^`]+)`/g, "<code>$1</code>");
  return safe;
}

function formatAiMessage(text) {
  const lines = String(text || "").split(/\r?\n/);
  let html = "";
  let inUl = false;
  let inOl = false;

  const closeLists = () => {
    if (inUl) {
      html += "</ul>";
      inUl = false;
    }
    if (inOl) {
      html += "</ol>";
      inOl = false;
    }
  };

  lines.forEach((raw) => {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) {
      closeLists();
      html += `<div class="ai-spacer"></div>`;
      return;
    }

    const heading = line.match(/^###\s+(.*)$/);
    if (heading) {
      closeLists();
      html += `<div class="ai-h3">${formatInline(heading[1])}</div>`;
      return;
    }

    const ol = line.match(/^\s*(\d+)[.)]\s+(.*)$/);
    if (ol) {
      if (!inOl) {
        closeLists();
        html += `<ol class="ai-list">`;
        inOl = true;
      }
      html += `<li>${formatInline(ol[2])}</li>`;
      return;
    }

    const ul = line.match(/^\s*[-*•]\s+(.*)$/);
    if (ul) {
      if (!inUl) {
        closeLists();
        html += `<ul class="ai-list">`;
        inUl = true;
      }
      html += `<li>${formatInline(ul[1])}</li>`;
      return;
    }

    closeLists();
    html += `<div class="ai-line">${formatInline(line)}</div>`;
  });

  closeLists();
  return html;
}

function aiOutputAppend(text, role = "assistant") {
  const boxes = [$("aiOutput"), $("architectOutputExpanded")].filter(Boolean);
  boxes.forEach((box) => {
    const div = document.createElement("div");
    div.className = `ai-msg ${role}`;
    div.innerHTML = formatAiMessage(text);
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
  });
}

function setAiHistory(history) {
  state.aiHistory = Array.isArray(history) ? history : [];
  aiOutputClear();
  state.aiHistory.forEach((msg) => {
    if (!msg || !msg.text) return;
    const role = msg.role === "user" ? "user" : msg.role === "system" ? "system" : "assistant";
    aiOutputAppend(msg.text, role);
  });
}

function pushAiHistory(role, text) {
  if (!text) return;
  if (!Array.isArray(state.aiHistory)) state.aiHistory = [];
  state.aiHistory.push({ role, text });
  if (state.aiHistory.length > 120) {
    state.aiHistory = state.aiHistory.slice(-120);
  }
}

async function resetAiHistory() {
  if (!state.activeId) return toast(`Select a ${entityLabel()} first.`);
  const ok = await openConfirmModal({
    title: "Reset conversation",
    subtitle: "Clear the Architect history for this item.",
    message: `Reset Architect history for this ${entityLabel()}? This cannot be undone.`,
    confirmText: "Reset history",
    confirmClass: "danger",
  });
  if (!ok) return;
  try {
    await api(`${entityEndpoint()}/${encodeURIComponent(state.activeId)}/conversation`, {
      method: "DELETE",
    });
    state.architectConversationId = null;
    state.architectContextSent = false;
    state.aiHistory = [];
    aiOutputClear();
    updateArchitectActionState();
    toast("History cleared.");
  } catch (e) {
    toast("History reset failed - check log", 3500);
    log(`History reset failed: ${e.message || e}`);
  }
}

function setCreateHistory(history) {
  state.createHistory = Array.isArray(history) ? history : [];
}

function pushCreateHistory(role, text) {
  if (!text) return;
  if (role !== "user" && role !== "assistant") return;
  if (!Array.isArray(state.createHistory)) state.createHistory = [];
  state.createHistory.push({ role, text });
  if (state.createHistory.length > 120) {
    state.createHistory = state.createHistory.slice(-120);
  }
}

function createOutputClear() {
  const box = $("createOutput");
  if (!box) return;
  box.innerHTML = "";
  setCreateHistory([]);
}

function createOutputAppend(text, role = "assistant") {
  const box = $("createOutput");
  if (!box) return;
  const div = document.createElement("div");
  div.className = `ai-msg ${role}`;
  div.innerHTML = formatAiMessage(text);
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  pushCreateHistory(role, text);
}

function showBuilderPrompt(target, prompt, options = {}) {
  if (!prompt) {
    target("Builder prompt not returned by Architect.", "error");
    return;
  }
  target("### Builder plan", "assistant");
  target(prompt, "builder");
  const trackHistory = options.trackHistory ?? (target === aiOutputAppend);
  if (trackHistory) {
    pushAiHistory("assistant", "### Builder plan");
    pushAiHistory("assistant", prompt);
  }
  log(`Builder prompt ready (${prompt.length} chars).`);
}

async function attachConversationHistory(entityId, entityType, conversationId, history) {
  if (!entityId) return;
  const messages = Array.isArray(history) ? history : [];
  if (!conversationId && !messages.length) return;
  const endpoint = entityType === "script" ? "/api/scripts" : "/api/automations";
  try {
    await api(`${endpoint}/${encodeURIComponent(entityId)}/conversation`, {
      method: "POST",
      body: JSON.stringify({
        conversation_id: conversationId,
        messages,
        replace: true,
      }),
    });
  } catch (e) {
    log(`Conversation attach failed: ${e.message || e}`);
  }
}

async function appendConversationHistory(entityId, entityType, conversationId, messages) {
  if (!entityId) return;
  const items = Array.isArray(messages) ? messages : [];
  if (!conversationId && !items.length) return;
  const endpoint = entityType === "script" ? "/api/scripts" : "/api/automations";
  try {
    await api(`${endpoint}/${encodeURIComponent(entityId)}/conversation`, {
      method: "POST",
      body: JSON.stringify({
        conversation_id: conversationId,
        messages: items,
      }),
    });
  } catch (e) {
    log(`Conversation append failed: ${e.message || e}`);
  }
}

const STATUS_CLASS_LIST = ["architect", "builder", "handoff", "kb", "summary"];
const ACTIVE_REQUESTS = new Map();
let ACTIVE_REQUEST_SEQ = 0;
const STATUS_CYCLES = new Map();

function isRequestCancelledError(err) {
  if (!err) return false;
  if (err.cancelled) return true;
  if (err.name === "AbortError") return true;
  const msg = String(err.message || err).toLowerCase();
  return msg.includes("request cancelled") || msg.includes("abort");
}

function updateStopRequestsButton() {
  const count = ACTIVE_REQUESTS.size;
  const label = count > 0 ? `Stop (${count})` : "Stop";
  ["stopRequestsBtn", "aiStopBtn", "aiStopBtnExpanded"].forEach((id) => {
    const btn = $(id);
    if (!btn) return;
    btn.disabled = count === 0;
    btn.textContent = label;
  });
}

function abortAllActiveRequests() {
  const count = ACTIVE_REQUESTS.size;
  if (!count) {
    toast("No active requests.");
    return;
  }
  for (const controller of ACTIVE_REQUESTS.values()) {
    try {
      controller.abort();
    } catch (err) {
      // Ignore abort races.
    }
  }
  clearStatus("aiStatus");
  clearStatus("createStatus");
  toast(`Stopped ${count} request${count === 1 ? "" : "s"}.`);
  log(`Stopped ${count} active request${count === 1 ? "" : "s"}.`);
}

function statusTargetIds(targetId) {
  const ids = [targetId];
  if (targetId === "aiStatus" && $("aiStatusExpanded")) ids.push("aiStatusExpanded");
  if (targetId === "aiStatusExpanded" && $("aiStatus")) ids.push("aiStatus");
  return ids;
}

function setStatusCycling(targetId, isCycling) {
  statusTargetIds(targetId).forEach((id) => {
    const box = $(id);
    if (!box) return;
    box.classList.toggle("cycling", Boolean(isCycling));
  });
}

function stopStatusCycle(targetId) {
  statusTargetIds(targetId).forEach((id) => {
    const timer = STATUS_CYCLES.get(id);
    if (timer) {
      clearInterval(timer);
      STATUS_CYCLES.delete(id);
    }
  });
  setStatusCycling(targetId, false);
}

function startStatusCycle(targetId, steps, intervalMs = 1100) {
  if (!Array.isArray(steps) || !steps.length) return () => {};
  stopStatusCycle(targetId);
  let idx = 0;
  const tick = () => {
    const step = steps[idx % steps.length] || {};
    setStatus(targetId, step.type || "architect", step.text || "");
    idx += 1;
  };
  tick();
  const timer = setInterval(tick, Math.max(500, intervalMs || 1100));
  statusTargetIds(targetId).forEach((id) => STATUS_CYCLES.set(id, timer));
  setStatusCycling(targetId, true);
  return () => stopStatusCycle(targetId);
}

function setStatus(targetId, type, text) {
  const ids = statusTargetIds(targetId);
  ids.forEach((id) => {
    const box = $(id);
    if (!box) return;
    box.classList.add("active");
    STATUS_CLASS_LIST.forEach((cls) => box.classList.remove(cls));
    if (type) box.classList.add(type);
    const icon = box.querySelector(".ai-status-icon");
    if (icon) {
      icon.className = "ai-status-icon";
    }
    const label = box.querySelector(".ai-status-text");
    if (label) label.textContent = text || "";

  });
}

function clearStatus(targetId) {
  stopStatusCycle(targetId);
  const ids = statusTargetIds(targetId);
  ids.forEach((id) => {
    const box = $(id);
    if (!box) return;
    box.classList.remove("active");
    STATUS_CLASS_LIST.forEach((cls) => box.classList.remove(cls));
    const label = box.querySelector(".ai-status-text");
    if (label) label.textContent = "";
  });
}

function isFormData(v) {
  return typeof FormData !== "undefined" && v instanceof FormData;
}

function getAgentId(key) {
  const agents = window.__AGENTS__ || {};
  return agents[key] || "";
}

function agentStatusText(key, actionText, fallbackLabel) {
  const labelMap = {
    architect: "Architect",
    builder: "Builder",
    kb_sync_helper: "Knowledgebase",
    summary: "Editor",
  };
  const label = fallbackLabel || labelMap[key] || "Agent";
  return `${label} ${actionText}`;
}

function startBuilderHandoff(statusId) {
  return startStatusCycle(statusId, [
    { type: "handoff", text: agentStatusText("architect", "preparing handoff...") },
    { type: "builder", text: agentStatusText("builder", "building output...") },
    { type: "summary", text: agentStatusText("summary", "reviewing output...") },
  ], 980);
}

function normalizeAgentList(out) {
  const list = Array.isArray(out)
    ? out
    : Array.isArray(out?.agents)
      ? out.agents
      : Array.isArray(out?.data)
        ? out.data
        : [];
  return list
    .map((agent) => {
      const id = agent?.id || agent?.agent_id || agent?.entity_id || "";
      const name = agent?.name || agent?.title || id || "Agent";
      return { id, name };
    })
    .filter((agent) => Boolean(agent.id));
}

async function loadConversationAgents() {
  try {
    const out = await api("/api/admin/conversation-agents");
    return normalizeAgentList(out);
  } catch (e) {
    return [];
  }
}

function populateAgentSelect(selectId, agents, currentValue) {
  const select = $(selectId);
  if (!select) return;
  select.innerHTML = "";
  const addOption = (value, label) => {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = label;
    select.appendChild(opt);
  };
  addOption("", "Use server default");
  const exists = agents.some((agent) => agent.id === currentValue);
  if (currentValue && !exists) {
    addOption(currentValue, `${currentValue} (custom)`);
  }
  agents.forEach((agent) => addOption(agent.id, `${agent.name} (${agent.id})`));
  select.value = currentValue || "";
}

function readAgentSelect(selectId) {
  const select = $(selectId);
  if (!select) return "";
  return (select.value || "").trim();
}

function populateValueSelect(selectId, values, currentValue, defaultLabel = "Use server default") {
  const select = $(selectId);
  if (!select) return;
  select.innerHTML = "";
  const addOption = (value, label) => {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = label;
    select.appendChild(opt);
  };
  if (defaultLabel) addOption("", defaultLabel);
  const normalized = Array.isArray(values)
    ? values.map((val) => String(val || "").trim()).filter(Boolean)
    : [];
  const uniqValues = [...new Set(normalized)];
  const current = String(currentValue || "").trim();
  if (current && !uniqValues.includes(current)) {
    addOption(current, `${current} (custom)`);
  }
  uniqValues.forEach((val) => addOption(val, val));
  select.value = current;
}

const APP_BASE_PATH = (() => {
  const baseEl = document.querySelector("base");
  let basePath = baseEl?.getAttribute("href") || window.location.pathname || "/";
  try {
    basePath = new URL(basePath, window.location.origin).pathname;
  } catch (err) {
    // Leave basePath as-is if parsing fails.
  }
  if (!basePath.startsWith("/")) basePath = `/${basePath}`;
  return basePath.replace(/\/$/, "");
})();

function withBasePath(path) {
  if (!path || !path.startsWith("/")) return path;
  if (!APP_BASE_PATH || APP_BASE_PATH === "/") return path;
  return `${APP_BASE_PATH}${path}`;
}

function getAgentSecret() {
  const stored = localStorage.getItem("agent_secret");
  if (stored) return stored;
  const injected = window.__AGENT_SECRET__ || "";
  if (injected) {
    localStorage.setItem("agent_secret", injected);
  }
  return injected;
}

async function api(path, opts = {}) {
  const req = { ...opts, headers: { ...(opts.headers || {}) } };
  // FastAPI param x_ha_agent_secret accepts header "X-HA-AGENT-SECRET"
  req.headers["X-HA-AGENT-SECRET"] = getAgentSecret();

  // If we're sending JSON, set content-type automatically.
  // (Don't set it for FormData; the browser will add boundaries.)
  if (req.body && typeof req.body === "string" && !req.headers["Content-Type"]) {
    req.headers["Content-Type"] = "application/json";
  }
  if (req.body && isFormData(req.body)) {
    // Ensure we do NOT accidentally set JSON content-type
    if (req.headers["Content-Type"]) delete req.headers["Content-Type"];
  }

  const controller = new AbortController();
  if (opts.signal) {
    if (opts.signal.aborted) {
      controller.abort();
    } else {
      opts.signal.addEventListener("abort", () => controller.abort(), { once: true });
    }
  }
  req.signal = controller.signal;
  const reqId = ++ACTIVE_REQUEST_SEQ;
  ACTIVE_REQUESTS.set(reqId, controller);
  updateStopRequestsButton();

  try {
    const res = await fetch(withBasePath(path), req);
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`${res.status} ${res.statusText}${text ? ` - ${text}` : ""}`);
    }
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) {
      const data = await res.json();
      appendUsage(data);
      return data;
    }
    return await res.text();
  } catch (err) {
    if (err?.name === "AbortError") {
      const cancelled = new Error("Request cancelled.");
      cancelled.cancelled = true;
      throw cancelled;
    }
    throw err;
  } finally {
    ACTIVE_REQUESTS.delete(reqId);
    updateStopRequestsButton();
  }
}

function setConn(ok, label) {
  const dot = $("connDot");
  const txt = $("connText");
  dot.style.background = ok ? "var(--ok)" : "var(--danger)";
  dot.style.boxShadow = ok
    ? "0 0 0 3px rgba(55,214,122,.12)"
    : "0 0 0 3px rgba(255,90,122,.12)";
  txt.textContent = label;
}

function normalizeListPayload(data) {
  if (Array.isArray(data)) return data;
  if (data && Array.isArray(data.items)) return data.items;
  if (data && Array.isArray(data.automations)) return data.automations;
  if (data && Array.isArray(data.scripts)) return data.scripts;
  return [];
}

function getCombineSelectionIds() {
  const vals = Array.isArray(state.combineSelectionIds) ? state.combineSelectionIds : [];
  return [...new Set(vals.map((id) => String(id || "").trim()).filter(Boolean))];
}

function isCombineModeActive() {
  return isAutomation() && getCombineSelectionIds().length >= 2;
}

function getCombineSelectionSummary() {
  const ids = getCombineSelectionIds();
  if (!ids.length) return "";
  const namesById = new Map(
    (state.list || []).map((item) => [String(item?.id || "").trim(), String(item?.alias || item?.name || item?.id || "").trim()])
  );
  const names = ids.map((id) => namesById.get(id) || id).filter(Boolean);
  if (names.length <= 3) return names.join(", ");
  return `${names.slice(0, 3).join(", ")}, +${names.length - 3} more`;
}

function updateArchitectAssistCopy() {
  const heading = $("aiAssistHeading");
  const hint = $("aiAssistHint");
  const prompt = $("aiPrompt");
  const promptExpanded = $("aiPromptExpanded");
  const count = getCombineSelectionIds().length;

  if (isCombineModeActive()) {
    const selectedText = getCombineSelectionSummary();
    const combinePlaceholder = `Add notes here to add or remove features from the combined ${entityLabel()}. Currently we are combining: ${selectedText}.`;
    if (heading) heading.textContent = "Combine these with the architect's assistance";
    if (hint) {
      hint.textContent = `Selected ${count} automations. Type notes here, then click Finalize and build to combine into one and disable redundant originals automatically.`;
    }
    if (prompt) prompt.placeholder = combinePlaceholder;
    if (promptExpanded) promptExpanded.placeholder = combinePlaceholder;
    return;
  }

  if (heading) heading.textContent = "Consult with the architect...";
  if (hint) {
    hint.textContent = "Plan changes with the Architect, then finalize to build an automation or script. The editor updates after build.";
  }
  if (prompt) {
    prompt.placeholder = isAutomation()
      ? "e.g. Add a 30 min bedtime window, and do not announce if I am already in bed..."
      : "e.g. Create a script that powers down the living room and turns off the TV...";
  }
  if (promptExpanded) {
    promptExpanded.placeholder = isAutomation()
      ? "Add your next automation change request..."
      : "Add your next script change request...";
  }
}

function setCombineSelection(ids) {
  state.combineSelectionIds = [...new Set((ids || []).map((id) => String(id || "").trim()).filter(Boolean))];
  syncCombineButton();
  updateArchitectAssistCopy();
}

function pruneCombineSelection() {
  const known = new Set((state.list || []).map((it) => String(it?.id || "").trim()).filter(Boolean));
  const next = getCombineSelectionIds().filter((id) => known.has(id));
  if (next.length !== getCombineSelectionIds().length) {
    setCombineSelection(next);
  } else {
    syncCombineButton();
  }
}

function toggleCombineSelection(id, selected) {
  const aid = String(id || "").trim();
  if (!aid) return;
  const current = getCombineSelectionIds();
  const set = new Set(current);
  if (selected) set.add(aid);
  else set.delete(aid);
  setCombineSelection([...set]);
}

function clearCombineSelection() {
  if (!getCombineSelectionIds().length) return;
  setCombineSelection([]);
}

function syncCombineButton() {
  const btn = $("combineBtn");
  if (!btn) return;
  const count = getCombineSelectionIds().length;
  const visible = isAutomation();
  btn.hidden = !visible;
  btn.disabled = !visible || state.capabilitiesView || count < 2;
  btn.textContent = count > 0 ? `Combine (${count})` : "Combine";
}

function renderList() {
  const el = $("automationList");
  pruneCombineSelection();
  const selectedForCombine = new Set(getCombineSelectionIds());
  let items = state.list;
  if (settings.hideDisabled && isAutomation()) {
    items = items.filter((it) => !isAutomationDisabled(it) || it.id === state.activeId);
  }

  if (!items.length) {
    el.innerHTML = `<div class="empty">No matches.</div>`;
    syncCombineButton();
    updateArchitectAssistCopy();
    return;
  }

  el.innerHTML = "";
  for (const it of items) {
    const isDisabled = isAutomation() && isAutomationDisabled(it);
    const isSelected = selectedForCombine.has(String(it.id || ""));
    const stateLabel = isAutomation() ? (isDisabled ? "Disabled" : "Enabled") : "Script";
    const stateClass = isAutomation()
      ? (isDisabled ? "badge-disabled" : "badge-enabled")
      : "";
    const div = document.createElement("div");
    div.className = "item"
      + (it.id === state.activeId ? " active" : "")
      + (isDisabled ? " disabled" : "")
      + (isSelected ? " combine-selected" : "");
    div.dataset.id = it.id;
    div.onclick = (e) => {
      if (e.target?.closest?.(".item-toggle")) return;
      if (e.target?.closest?.(".item-select-wrap")) return;
      openAutomation(it.id);
    };

    div.innerHTML = `
      <div class="item-top">
        <div class="item-title-wrap">
          ${isAutomation() ? `
            <label class="item-select-wrap" title="Select for combine">
              <input class="item-select" type="checkbox" data-combine-id="${escapeHtml(it.id)}" ${isSelected ? "checked" : ""} />
            </label>
          ` : ""}
          <div class="item-action-stack">
            <span class="badge ${stateClass}">${stateLabel}</span>
            <button class="item-toggle" data-action="toggle">Details</button>
          </div>
          <div class="item-title">${escapeHtml(it.alias || it.name || it.id)}</div>
        </div>
      </div>
      <div class="item-details">
        <div class="item-desc">${escapeHtml(it.description || it.summary || "")}</div>
        <div class="item-desc" style="opacity:.75;margin-top:8px;">${escapeHtml(it.id)}</div>
      </div>
    `;
    const check = div.querySelector(".item-select");
    if (check) {
      check.addEventListener("click", (e) => e.stopPropagation());
      check.addEventListener("change", () => toggleCombineSelection(it.id, check.checked));
    }
    el.appendChild(div);
  }
  syncCombineButton();
  updateArchitectAssistCopy();
}

function renderHealthPanel() {
  const box = $("healthPanel");
  if (!box) return;
  if (DISABLE_HEALTH_PANEL) {
    box.innerHTML = `<div class="muted">Health checks temporarily disabled.</div>`;
    return;
  }
  if (state.capabilitiesView) {
    box.innerHTML = `<div class="muted">Knowledgebase view.</div>`;
    return;
  }
  if (!isAutomation()) {
    box.innerHTML = `<div class="muted">Health checks are for automations.</div>`;
    return;
  }
  if (!state.activeId) {
    box.innerHTML = `<div class="muted">Select an automation to view health.</div>`;
    return;
  }
  if (state.healthLoading) {
    box.innerHTML = `<div class="muted">Loading health...</div>`;
    return;
  }
  const health = state.health;
  if (!health) {
    box.innerHTML = `<div class="muted">Health data unavailable.</div>`;
    return;
  }
  const rows = [];
  rows.push(`<div class="health-row"><span class="health-label">Status</span><span>${escapeHtml(health.state || "unknown")}</span></div>`);
  rows.push(`<div class="health-row"><span class="health-label">Last triggered</span><span>${escapeHtml(health.last_triggered || "n/a")}</span></div>`);
  if (health.last_action) {
    rows.push(`<div class="health-row"><span class="health-label">Last action</span><span>${escapeHtml(health.last_action)}</span></div>`);
  }
  if (health.last_error) {
    rows.push(`<div class="health-row"><span class="health-label">Last error</span><span>${escapeHtml(health.last_error)}</span></div>`);
  }
  if (health.missing_entities?.length) {
    rows.push(`<div><div class="health-label">Missing entities</div><ul class="health-list">${health.missing_entities.map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul></div>`);
  }
  if (health.disabled_entities?.length) {
    rows.push(`<div><div class="health-label">Disabled entities</div><ul class="health-list">${health.disabled_entities.map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul></div>`);
  }
  if (health.stale_entities?.length) {
    rows.push(`<div><div class="health-label">Stale device refs</div><ul class="health-list">${health.stale_entities.map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul></div>`);
  }
  if (!health.missing_entities?.length && !health.disabled_entities?.length && !health.stale_entities?.length) {
    rows.push(`<div class="muted">No issues detected.</div>`);
  }
  box.innerHTML = rows.join("");
}

async function loadHealth(id = state.activeId) {
  if (DISABLE_HEALTH_PANEL) {
    state.health = null;
    state.healthLoading = false;
    renderHealthPanel();
    return;
  }
  if (!id || !isAutomation() || state.capabilitiesView) {
    state.health = null;
    renderHealthPanel();
    return;
  }
  state.healthLoading = true;
  renderHealthPanel();
  try {
    const out = await api(`/api/automations/${encodeURIComponent(id)}/health`);
    if (state.activeId === id) {
      state.health = out;
    }
  } catch (e) {
    log(`Health load failed: ${e.message || e}`);
    if (state.activeId === id) state.health = null;
  } finally {
    if (state.activeId === id) {
      state.healthLoading = false;
      renderHealthPanel();
    }
  }
}

function parseScenarioOverrides(raw) {
  const overrides = {};
  const lines = String(raw || "").split(/\r?\n/);
  lines.forEach((line) => {
    const cleaned = line.trim();
    if (!cleaned || cleaned.startsWith("#")) return;
    const idx = cleaned.indexOf("=");
    if (idx === -1) return;
    const key = cleaned.slice(0, idx).trim();
    const value = cleaned.slice(idx + 1).trim();
    if (!key) return;
    overrides[key] = value;
  });
  return overrides;
}

function renderScenarioOutput() {
  const box = $("scenarioOutput");
  if (!box) return;
  if (state.capabilitiesView) {
    box.innerHTML = `<div class="muted">Knowledgebase view.</div>`;
    return;
  }
  if (!isAutomation()) {
    box.innerHTML = `<div class="muted">Scenario tests are for automations.</div>`;
    return;
  }
  if (!state.activeId) {
    box.innerHTML = `<div class="muted">Select an automation to test.</div>`;
    return;
  }
  if (state.scenarioRunning) {
    box.textContent = "Running scenario...";
    return;
  }
  const result = state.scenarioResult;
  if (!result) {
    box.innerHTML = `<div class="muted">No scenario run yet.</div>`;
    return;
  }
  const lines = [];
  const status = result.conditions_unknown ? "unknown" : (result.conditions_passed ? "passed" : "failed");
  lines.push(`Conditions: ${status}`);
  if (Array.isArray(result.actions) && result.actions.length) {
    lines.push("Actions:");
    result.actions.forEach((a) => lines.push(`- ${a}`));
  }
  if (Array.isArray(result.logs) && result.logs.length) {
    lines.push("Logs:");
    result.logs.forEach((l) => lines.push(`- ${l}`));
  }
  box.textContent = lines.join("\n");
}

async function runScenarioTest() {
  if (!state.activeId || !isAutomation()) {
    toast("Select an automation to test.");
    return;
  }
  const time = $("scenarioTime")?.value || "";
  const overrides = parseScenarioOverrides($("scenarioOverrides")?.value || "");
  state.scenarioRunning = true;
  renderScenarioOutput();
  try {
    const out = await api(`/api/automations/${encodeURIComponent(state.activeId)}/test`, {
      method: "POST",
      body: JSON.stringify({
        time,
        overrides,
        yaml: getCurrentDraftText(),
      }),
    });
    state.scenarioResult = out;
  } catch (e) {
    log(`Scenario test failed: ${e.message || e}`);
    state.scenarioResult = { conditions_passed: false, conditions_unknown: true, actions: [], logs: [String(e.message || e)] };
  } finally {
    state.scenarioRunning = false;
    renderScenarioOutput();
  }
}

function escapeHtml(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatStamp(ts) {
  if (!ts) return "";
  const digits = String(ts).replace(/\D/g, "");
  if (digits.length >= 14) {
    const y = digits.slice(0, 4);
    const m = digits.slice(4, 6);
    const d = digits.slice(6, 8);
    const hh = digits.slice(8, 10);
    const mm = digits.slice(10, 12);
    const ss = digits.slice(12, 14);
    return `${y}-${m}-${d} ${hh}:${mm}:${ss}`;
  }
  return String(ts);
}

function renderUsageChart() {
  const box = $("usageChart");
  if (!box) return;
  const snap = state.usageSnapshot;
  if (!snap || !Array.isArray(snap.events) || !snap.events.length) {
    box.innerHTML = `<div class="usage-empty">No token usage yet.</div>`;
    return;
  }
  const maxTokens = Math.max(1, ...snap.events.map((ev) => ev.totalTokens || 0));
  const totalTokens = Number(snap.totalTokens) || 0;
  const cost = formatMoney(Number(snap.amount) || 0, snap.currency || "GBP");
  const stamp = new Date(snap.at || Date.now()).toLocaleTimeString();
  let html = `<div class="usage-meta">${escapeHtml(snap.label)} - ${stamp}<br>Total: ${totalTokens} tokens (~${escapeHtml(cost)})</div>`;
  snap.events.slice(0, 8).forEach((ev) => {
    const pct = Math.max(4, Math.round((ev.totalTokens / maxTokens) * 100));
    const name = ev.model ? `${ev.name} (${ev.model})` : ev.name;
    html += `
      <div class="usage-row">
        <div class="usage-label">
          <span class="usage-label-name">${escapeHtml(name)}</span>
          <span class="usage-label-value">${ev.totalTokens}</span>
        </div>
        <div class="usage-bar"><span class="usage-bar-fill" style="width:${pct}%"></span></div>
      </div>
    `;
  });
  box.innerHTML = html;
}

function showSetupBanner() {
  const el = $("setupBanner");
  if (el) el.hidden = false;
}

function hideSetupBanner() {
  const el = $("setupBanner");
  if (el) el.hidden = true;
}

function loadSettings() {
  try {
    const raw = localStorage.getItem("ui_settings");
    const parsed = raw ? JSON.parse(raw) : {};
    settings = { ...DEFAULT_SETTINGS, ...parsed };
  } catch (e) {
    settings = { ...DEFAULT_SETTINGS };
  }
  settings.tryLocalEditFirst = settings.tryLocalEditFirst !== false;
  settings.allowAiDiff = Boolean(settings.allowAiDiff);
  const conf = parseFloat(settings.helperMinConfidence);
  settings.helperMinConfidence = Number.isFinite(conf) ? Math.max(0, Math.min(1, conf)) : DEFAULT_SETTINGS.helperMinConfidence;
  settings.usageCurrency = String(settings.usageCurrency || DEFAULT_SETTINGS.usageCurrency).toUpperCase();
  if (settings.compareTarget === "latest") settings.compareTarget = "version";
  applySettings();
}

function applySettings() {
  document.body.classList.toggle("compact-list", Boolean(settings.compactList));
  syncHideDisabledToggle();
}

function syncHideDisabledToggle() {
  const btn = $("hideDisabledToggleBtn");
  if (!btn) return;
  const active = Boolean(settings.hideDisabled);
  btn.classList.toggle("toggle-active", active);
  btn.setAttribute("aria-pressed", active ? "true" : "false");
  btn.textContent = active ? "Show disabled" : "Hide disabled";
}

function loadAiCollapsed() {
  try {
    state.aiCollapsed = localStorage.getItem(AI_COLLAPSE_KEY) === "1";
  } catch (e) {
    state.aiCollapsed = false;
  }
}

async function checkHelperAgents() {
  try {
    const out = await api("/api/admin/agent-check");
    const bad = Array.isArray(out?.bad_agents) ? out.bad_agents : [];
    if (bad.length) {
      toast(`Helper agents need attention: ${bad.join(", ")}`, 5200);
      log(`Agent check failed for: ${bad.join(", ")}.`);
    }
  } catch (e) {
    // ignore when admin endpoint is unavailable or auth missing
  }
}

function setAiCollapsed(collapsed, persist = true) {
  state.aiCollapsed = Boolean(collapsed);
  document.body.classList.toggle("ai-collapsed", state.aiCollapsed);
  const btn = $("aiSectionToggleBtn");
  if (btn) {
    btn.textContent = state.aiCollapsed ? "Show AI" : "Hide AI";
    btn.classList.toggle("ai-toggle-active", state.aiCollapsed);
  }
  if (persist) {
    try {
      localStorage.setItem(AI_COLLAPSE_KEY, state.aiCollapsed ? "1" : "0");
    } catch (e) {
      // ignore persistence errors
    }
  }
  applyColumnSplit();
  sizeEditorToWrap();
  syncViewMenuToggleState();
}

function syncCapabilitiesUi() {
  const btn = $("capabilitiesBtn");
  if (btn) {
    btn.classList.toggle("toggle-active", state.capabilitiesView);
    btn.setAttribute("aria-pressed", state.capabilitiesView ? "true" : "false");
    btn.textContent = state.capabilitiesView ? "Back to editor" : "Knowledgebase";
  }
  const refreshBtn = $("capabilitiesRefreshBtn");
  if (refreshBtn) refreshBtn.hidden = !state.capabilitiesView;
  const syncBtn = $("capabilitiesSyncBtn");
  if (syncBtn) {
    const canSync = Boolean(state.activeId);
    syncBtn.hidden = !canSync;
    syncBtn.disabled = !canSync || state.capabilitiesView;
    if (canSync) {
      syncBtn.textContent = isAutomation() ? "Update KB from this automation" : "Update KB from this script";
    }
  }
  const learnBtn = $("capabilitiesLearnBtn");
  if (learnBtn) {
    learnBtn.hidden = false;
    learnBtn.disabled = false;
  }
  const visualBtn = $("viewVisualBtn");
  if (visualBtn) visualBtn.disabled = state.capabilitiesView;
}

async function fetchCapabilitiesYaml() {
  const out = await api("/api/capabilities");
  if (typeof out === "string") return out;
  return out?.yaml ?? out?.content ?? out?.raw ?? "";
}

async function openCapabilitiesView() {
  if (state.capabilitiesView) return;
  if (state.dirty) {
    const ok = confirm("Open the knowledgebase view? Unsaved changes stay in memory.");
    if (!ok) return;
  }
  state.capabilitiesSnapshot = {
    viewMode: state.viewMode,
    compareTarget: state.compareTarget,
  };
  state.capabilitiesView = true;
  document.body.classList.add("capabilities-view");
  state.compareTarget = "current";
  setEditorReadOnly(true);
  setViewMode("yaml");
  updateCompareTabs();
  setButtons(Boolean(state.activeId));
  setVersionButtons(false);
  $("aTitle").textContent = "Capabilities";
  $("aMeta").textContent = "capabilities.yaml";
  syncCapabilitiesUi();
  updateArchitectActionState();
  updateAutomationPanels();

  try {
    const yamlText = await fetchCapabilitiesYaml();
    if (!state.capabilitiesView) return;
    state.capabilitiesYaml = yamlText;
    setEditorValue(yamlText);
    toast("Knowledgebase loaded.");
  } catch (e) {
    toast("Capabilities load failed - check log", 3500);
    log(`Capabilities load failed: ${e.message || e}`);
  }
}

function closeCapabilitiesView() {
  if (!state.capabilitiesView) return;
  state.capabilitiesView = false;
  document.body.classList.remove("capabilities-view");
  syncCapabilitiesUi();

  const snapshot = state.capabilitiesSnapshot || {};
  state.compareTarget = snapshot.compareTarget || "current";
  if (state.compareTarget === "version" && state.selectedVersionYaml) {
    setEditorReadOnly(true);
    setEditorValue(state.selectedVersionYaml);
  } else {
    setEditorReadOnly(false);
    setEditorValue(state.currentDraft ?? getLatestSavedYaml());
    if (state.compareTarget === "version") state.compareTarget = "current";
  }
  setViewMode(snapshot.viewMode || "yaml");
  updateCompareTabs();

  if (settings.tryLocalEditFirst && state.activeId) {
    $("aTitle").textContent = state.active?.alias || state.activeId;
    $("aMeta").textContent = `${state.active?.source || "Unknown"} - ${state.activeId}`;
  } else {
    $("aTitle").textContent = `Select a ${entityLabel()}`;
    $("aMeta").textContent = "Pick one from the list on the left.";
  }
  setButtons(Boolean(state.activeId));
  setVersionButtons(Boolean(state.selectedVersionId));
  updateEnableButtonFromState(state.active?.state);
  updateArchitectActionState();
  setDirty(state.dirty);
  updateAutomationPanels();
}

async function toggleCapabilitiesView() {
  if (state.capabilitiesView) {
    closeCapabilitiesView();
  } else {
    await openCapabilitiesView();
  }
}

async function refreshCapabilities() {
  if (!state.capabilitiesView) return;
  const ok = confirm("Refresh the knowledgebase from Home Assistant now?");
  if (!ok) return;
  try {
    const out = await api("/api/capabilities/refresh", { method: "POST" });
    const yamlText = typeof out === "string" ? out : (out?.yaml ?? "");
    if (!state.capabilitiesView) return;
    state.capabilitiesYaml = yamlText;
    setEditorValue(yamlText);
    state.capabilitiesCache = null;
    const summary = out?.summary || out?.counts || null;
    if (summary) {
      const parts = [];
      if (summary.areas !== undefined) parts.push(`${summary.areas} areas`);
      if (summary.entities !== undefined) parts.push(`${summary.entities} entities`);
      if (summary.automations !== undefined) parts.push(`${summary.automations} automations`);
      if (summary.scripts !== undefined) parts.push(`${summary.scripts} scripts`);
      if (summary.services !== undefined) parts.push(`${summary.services} services`);
      toast(`Knowledgebase refreshed: ${parts.join(", ")}`);
      log(`Capabilities refreshed: ${parts.join(", ")}.`);
    } else {
      toast("Knowledgebase refreshed.");
      log("Capabilities refreshed.");
    }
  } catch (e) {
    toast("Capabilities refresh failed - check log", 3500);
    log(`Capabilities refresh failed: ${e.message || e}`);
  }
}

function collectEntityIdsFromObject(obj, out, keyHint = "") {
  if (typeof obj === "string") {
    if (keyHint === "service" || keyHint === "service_template") return;
    const matches = obj.match(/\b[a-z_]+\.[a-z0-9_]+\b/gi) || [];
    matches.forEach((m) => out.add(m));
    return;
  }
  if (Array.isArray(obj)) {
    obj.forEach((item) => collectEntityIdsFromObject(item, out, keyHint));
    return;
  }
  if (obj && typeof obj === "object") {
    Object.entries(obj).forEach(([key, val]) => collectEntityIdsFromObject(val, out, key));
  }
}

function collectServicesFromObject(obj, out) {
  if (Array.isArray(obj)) {
    obj.forEach((item) => collectServicesFromObject(item, out));
    return;
  }
  if (obj && typeof obj === "object") {
    Object.entries(obj).forEach(([key, val]) => {
      if ((key === "service" || key === "service_template") && typeof val === "string") {
        out.add(val.trim());
      } else {
        collectServicesFromObject(val, out);
      }
    });
  }
}

function collectCapabilitiesEntities(caps) {
  const set = new Set();
  const inv = caps?.inventory || {};
  (inv.entities || []).forEach((item) => item?.entity_id && set.add(item.entity_id));
  (inv.used_entities || []).forEach((eid) => eid && set.add(String(eid)));
  (inv.scripts || []).forEach((item) => item?.entity_id && set.add(item.entity_id));
  (caps?.scripts || []).forEach((item) => item?.entity_id && set.add(item.entity_id));
  const hints = caps?.user_context?.entity_hints;
  if (hints && typeof hints === "object") {
    Object.keys(hints).forEach((k) => set.add(k));
  }
  return set;
}

function collectCapabilitiesServices(caps) {
  const set = new Set();
  const inv = caps?.inventory || {};
  (inv.services || []).forEach((svc) => svc && set.add(String(svc)));
  return set;
}

async function getCapabilitiesCache(force = false) {
  if (state.capabilitiesCache && !force) return state.capabilitiesCache;
  try {
    const out = await api("/api/capabilities");
    const yamlText = typeof out === "string" ? out : (out?.yaml ?? "");
    const data = jsyaml.load(yamlText) || {};
    const cache = {
      raw: yamlText,
      data,
      entities: collectCapabilitiesEntities(data),
      services: collectCapabilitiesServices(data),
    };
    state.capabilitiesCache = cache;
    return cache;
  } catch (e) {
    log(`Capabilities cache load failed: ${e.message || e}`);
    return null;
  }
}

async function checkYamlAgainstCapabilities(yamlText) {
  const cache = await getCapabilitiesCache();
  if (!cache || !cache.data) return null;
  if (!yamlText || !yamlText.trim()) return null;

  let obj;
  try {
    obj = jsyaml.load(yamlText);
  } catch (e) {
    return null;
  }
  if (Array.isArray(obj)) {
    obj = obj.find((x) => x && typeof x === "object") || obj[0];
  }
  if (obj && typeof obj === "object" && Array.isArray(obj.automation)) {
    obj = obj.automation.find((x) => x && typeof x === "object") || obj.automation[0];
  }

  const entities = new Set();
  const services = new Set();
  collectEntityIdsFromObject(obj, entities);
  collectServicesFromObject(obj, services);

  const missingEntities = [...entities].filter((e) => cache.entities.size && !cache.entities.has(e));
  const missingServices = [...services].filter((s) => cache.services.size && !cache.services.has(s));
  return { missingEntities, missingServices };
}

async function maybePromptMissingCapabilities(yamlText) {
  if (!yamlText || state.capabilitiesView) return;
  const report = await checkYamlAgainstCapabilities(yamlText);
  if (!report) return;
  const { missingEntities, missingServices } = report;
  if (!missingEntities.length && !missingServices.length) return;
  const lines = [];
  if (missingEntities.length) {
    lines.push("Entities not in capabilities:");
    missingEntities.slice(0, 8).forEach((e) => lines.push(`- ${e}`));
    if (missingEntities.length > 8) lines.push(`- ...and ${missingEntities.length - 8} more`);
  }
  if (missingServices.length) {
    if (lines.length) lines.push("");
    lines.push("Services not in capabilities:");
    missingServices.slice(0, 8).forEach((s) => lines.push(`- ${s}`));
    if (missingServices.length > 8) lines.push(`- ...and ${missingServices.length - 8} more`);
  }
  const result = await openConfirmModal({
    title: "Unmapped items",
    subtitle: "AI suggested items not found in your knowledgebase.",
    message: lines.join("\n"),
    confirmText: "Continue",
    secondaryText: "Open knowledgebase",
    secondaryClass: "primary",
  });
  if (result === "secondary") {
    await openCapabilitiesView();
  }
}

function kbSyncOutputClear() {
  const box = $("kbSyncOutput");
  if (!box) return;
  box.innerHTML = "";
}

function kbSyncOutputAppend(text, role = "assistant") {
  const box = $("kbSyncOutput");
  if (!box) return;
  const div = document.createElement("div");
  div.className = `ai-msg ${role}`;
  div.innerHTML = formatAiMessage(text);
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function openKbSyncModal({ autoRun = false, prefill = "" } = {}) {
  const label = $("kbSyncAutomationLabel");
  if (label) {
    if (state.activeId) {
      const name = state.active?.alias || state.activeId;
      label.textContent = `Current: ${name} (${entityLabel()})`;
    } else {
      label.textContent = "Current: none";
    }
  }
  const prompt = $("kbSyncPrompt");
  if (prompt) {
    prompt.value = prefill || "";
  }
  kbSyncOutputClear();
  clearStatus("kbSyncStatus");
  openModal("kbSyncModal");
  if (autoRun) {
    runKbSync();
  }
}

async function runKbSync() {
  if (!state.activeId) {
    toast("Select an automation or script first.");
    return;
  }
  if (state.kbSyncRunning) return;
  state.kbSyncRunning = true;
  const runBtn = $("kbSyncRunBtn");
  if (runBtn) runBtn.disabled = true;
  const promptText = ($("kbSyncPrompt")?.value || "").trim();
  try {
    setStatus("kbSyncStatus", "kb", agentStatusText("kb_sync_helper", "updating..."));
    if (promptText) {
      kbSyncOutputAppend(promptText, "user");
    }
    kbSyncOutputAppend("Sending to Knowledgebase agent...", "system");
    const out = await api("/api/capabilities/sync", {
      method: "POST",
      body: JSON.stringify({
        entity_id: state.activeId,
        entity_type: state.entityType,
        yaml: getCurrentDraftText(),
        prompt: promptText,
      }),
    });
    if (out?.reply) {
      kbSyncOutputAppend(out.reply, "assistant");
    }
    if (out?.missing_entities?.length || out?.missing_services?.length) {
      const parts = [];
      if (out.missing_entities?.length) parts.push(`${out.missing_entities.length} missing entities`);
      if (out.missing_services?.length) parts.push(`${out.missing_services.length} missing services`);
      log(`KB sync detected: ${parts.join(", ")}.`);
    }
    toast("KB sync questions ready.");
  } catch (e) {
    toast("KB sync failed - check log", 3500);
    log(`KB sync failed: ${e.message || e}`);
    kbSyncOutputAppend(`Error: ${e.message || e}`, "error");
  } finally {
    clearStatus("kbSyncStatus");
    state.kbSyncRunning = false;
    if (runBtn) runBtn.disabled = false;
  }
}

function buildKbPreviewMessage(preview) {
  if (!preview || typeof preview !== "object") return "Save this note to the knowledgebase?";
  const entities = Array.isArray(preview.entities) ? preview.entities : [];
  const scripts = Array.isArray(preview.scripts) ? preview.scripts : [];
  const tags = Array.isArray(preview.tags) ? preview.tags : [];
  const parts = [];
  if (entities.length) parts.push(`Entities: ${entities.slice(0, 8).join(", ")}${entities.length > 8 ? "..." : ""}`);
  if (scripts.length) parts.push(`Scripts: ${scripts.slice(0, 8).join(", ")}${scripts.length > 8 ? "..." : ""}`);
  if (tags.length) parts.push(`Tags: ${tags.join(", ")}`);
  if (!parts.length) return "Save this note to the knowledgebase?";
  return `Save this to the knowledgebase?\n\n${parts.join("\n")}`;
}

async function runKbSave() {
  const promptText = ($("kbSyncPrompt")?.value || "").trim();
  if (!promptText) return toast("Write a note first.");
  if (state.kbSyncRunning) return;
  state.kbSyncRunning = true;
  const saveBtn = $("kbSyncSaveBtn");
  if (saveBtn) saveBtn.disabled = true;
  try {
    setStatus("kbSyncStatus", "kb", agentStatusText("kb_sync_helper", "preparing update..."));
    const previewOut = await api("/api/capabilities/learn", {
      method: "POST",
      body: JSON.stringify({
        text: promptText,
        entity_id: state.activeId,
        entity_type: state.entityType,
        yaml: getCurrentDraftText(),
      }),
    });
    if (previewOut?.intent_summary) {
      kbSyncOutputAppend(previewOut.intent_summary, "assistant");
    }
    if (previewOut?.questions?.length) {
      const qs = previewOut.questions.map((q) => `- ${q}`).join("\n");
      kbSyncOutputAppend(`Questions:\n${qs}`, "assistant");
    }
    const message = buildKbPreviewMessage(previewOut?.preview);
    const ok = await openConfirmModal({
      title: "Save to knowledgebase",
      subtitle: "Confirm what will be stored.",
      message,
      confirmText: "Save",
    });
    if (!ok) return;
    const commitOut = await api("/api/capabilities/learn", {
      method: "POST",
      body: JSON.stringify({
        text: promptText,
        confirm: true,
        entity_id: state.activeId,
        entity_type: state.entityType,
        yaml: getCurrentDraftText(),
      }),
    });
    handleCapabilitiesUpdated(commitOut);
    kbSyncOutputAppend("Saved to knowledgebase.", "system");
    toast("Knowledgebase updated.");
    state.capabilitiesCache = null;
    if (state.capabilitiesView) {
      try {
        const yamlText = await fetchCapabilitiesYaml();
        if (state.capabilitiesView) {
          state.capabilitiesYaml = yamlText;
          setEditorValue(yamlText);
        }
      } catch (e) {
        // ignore refresh errors
      }
    }
  } catch (e) {
    toast("Save to KB failed - check log", 3500);
    log(`KB save failed: ${e.message || e}`);
    kbSyncOutputAppend(`Error: ${e.message || e}`, "error");
  } finally {
    clearStatus("kbSyncStatus");
    state.kbSyncRunning = false;
    if (saveBtn) saveBtn.disabled = false;
  }
}

async function saveCapabilitiesFromHistory(history, entityId, entityType) {
  const items = Array.isArray(history) ? history : [];
  const notes = items
    .filter((m) => (m?.role || "").toLowerCase() === "user")
    .map((m) => String(m.text || m.content || "").trim())
    .filter(Boolean);
  if (!notes.length) return null;
  const combined = notes.join("\n\n");
  try {
    const out = await api("/api/capabilities/learn", {
      method: "POST",
      body: JSON.stringify({
        text: combined,
        confirm: true,
        entity_id: entityId || null,
        entity_type: entityType || state.entityType,
      }),
    });
    handleCapabilitiesUpdated(out);
    return out;
  } catch (e) {
    log(`KB save from history failed: ${e.message || e}`);
    return null;
  }
}

function loadLayout() {
  try {
    const raw = localStorage.getItem(LAYOUT_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return {
      ...DEFAULT_LAYOUT,
      ...parsed,
      sizes: { ...DEFAULT_LAYOUT.sizes, ...(parsed.sizes || {}) },
      order: { ...DEFAULT_LAYOUT.order, ...(parsed.order || {}) },
      columnSplit: { ...DEFAULT_LAYOUT.columnSplit, ...(parsed.columnSplit || {}) },
    };
  } catch (e) {
    return { ...DEFAULT_LAYOUT };
  }
}

function saveLayout() {
  localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(layout));
}

function getSizeAxis(el) {
  const axis = (el?.dataset?.sizeAxis || "y").toLowerCase();
  if (axis.includes("both") || axis === "xy") return "both";
  if (axis.includes("x")) return "x";
  if (axis.includes("y")) return "y";
  return "y";
}

function applySectionSize(el, size) {
  if (!el) return;
  const axis = getSizeAxis(el);
  const hasX = axis === "both" || axis.includes("x");
  const hasY = axis === "both" || axis.includes("y");
  let applied = false;
  let heightApplied = false;
  let widthApplied = false;

  if (hasY && size && size.height) {
    el.style.height = `${size.height}px`;
    applied = true;
    heightApplied = true;
  } else if (hasY) {
    el.style.height = "";
  }

  if (hasX && size && size.width) {
    el.style.width = `${size.width}px`;
    el.style.flex = "0 0 auto";
    applied = true;
    widthApplied = true;
  } else if (hasX) {
    el.style.width = "";
  }

  if (!widthApplied) {
    const parent = el.parentElement;
    const flexDir = parent ? getComputedStyle(parent).flexDirection : "";
    if (heightApplied && (flexDir === "column" || flexDir === "column-reverse")) {
      el.style.flex = "0 0 auto";
    } else if (hasX || hasY) {
      el.style.flex = "";
    }
  }

  if (applied) {
    el.classList.add("has-size");
  } else {
    el.classList.remove("has-size");
  }
}

function getFlexGap(el) {
  if (!el) return 0;
  const style = getComputedStyle(el);
  const raw = style.rowGap || style.gap || "0";
  const gap = parseFloat(raw);
  return Number.isFinite(gap) ? gap : 0;
}

let _layoutSyncRaf = null;
function scheduleLayoutSync() {
  if (_layoutSyncRaf) return;
  _layoutSyncRaf = requestAnimationFrame(() => {
    _layoutSyncRaf = null;
    applyColumnSplit();
    sizeEditorToWrap();
  });
}

function applyColumnSplit() {
  const column = document.querySelector(".editor-column");
  const splitter = $("editorSplitter");
  const editorWrap = document.querySelector(".editor-wrap");
  const aiRow = $("aiRow");
  if (!column || !splitter || !editorWrap || !aiRow) return;

  if (state.aiCollapsed) {
    aiRow.style.display = "none";
    aiRow.style.height = "";
    aiRow.style.flex = "";
    editorWrap.style.height = "auto";
    editorWrap.style.flex = "1 1 auto";
    return;
  }

  aiRow.style.display = "";
  const rect = column.getBoundingClientRect();
  if (!rect.height) return;
  const gap = getFlexGap(column);
  const splitterHeight = splitter.getBoundingClientRect().height || 6;
  const available = rect.height - splitterHeight - gap * 2;
  if (available <= 0) return;

  let pct = layout.columnSplit?.editorPct;
  if (!Number.isFinite(pct)) pct = DEFAULT_LAYOUT.columnSplit.editorPct;
  pct = Math.max(0.2, Math.min(0.8, pct));

  const minEditor = 180;
  const minAi = 180;
  let editorH = Math.round(available * pct);
  let aiH = available - editorH;

  if (available >= minEditor + minAi) {
    if (editorH < minEditor) {
      editorH = minEditor;
      aiH = available - editorH;
    }
    if (aiH < minAi) {
      aiH = minAi;
      editorH = available - aiH;
    }
  } else {
    editorH = Math.max(80, Math.round(available * pct));
    aiH = Math.max(80, available - editorH);
  }

  editorWrap.style.height = `${Math.max(80, editorH)}px`;
  editorWrap.style.flex = "0 0 auto";
  aiRow.style.height = `${Math.max(80, aiH)}px`;
  aiRow.style.flex = "0 0 auto";
}

function applyLayout() {
  const rail = $("rightRail");
  if (rail && layout.rightRailWidth) {
    document.documentElement.style.setProperty("--right-rail-width", `${layout.rightRailWidth}px`);
  }
  document.querySelectorAll("[data-size-id]").forEach((el) => {
    const id = el.dataset.sizeId;
    const size = layout.sizes ? layout.sizes[id] : null;
    applySectionSize(el, size);
  });
  applyColumnSplit();
  sizeEditorToWrap();
}

function resetLayout() {
  localStorage.removeItem(LAYOUT_STORAGE_KEY);
  layout = loadLayout();
  document.documentElement.style.removeProperty("--right-rail-width");
  document.querySelectorAll("[data-size-id]").forEach((el) => applySectionSize(el, null));
  applyLayout();
  toast("Layout reset.");
}

function persistSectionSize(el) {
  const id = el?.dataset?.sizeId;
  if (!id) return;
  const axis = getSizeAxis(el);
  const rect = el.getBoundingClientRect();
  const entry = { ...(layout.sizes?.[id] || {}) };
  if (axis === "both" || axis.includes("y")) {
    entry.height = Math.round(rect.height);
  }
  if (axis === "both" || axis.includes("x")) {
    entry.width = Math.round(rect.width);
  }
  if (!layout.sizes) layout.sizes = {};
  layout.sizes[id] = entry;
  if (id === "editor") {
    const column = document.querySelector(".editor-column");
    const splitter = $("editorSplitter");
    if (column && splitter) {
      const gap = getFlexGap(column);
      const splitterHeight = splitter.getBoundingClientRect().height || 6;
      const available = column.getBoundingClientRect().height - splitterHeight - gap * 2;
      if (available > 0) {
        const pct = entry.height / available;
        layout.columnSplit = {
          ...(layout.columnSplit || {}),
          editorPct: Math.max(0.2, Math.min(0.8, pct)),
        };
      }
    }
  }
  saveLayout();
  if (id === "editor") sizeEditorToWrap();
}

function setupResizableSections() {
  document.querySelectorAll("[data-size-id]").forEach((el) => {
    el.addEventListener("pointerdown", (e) => {
      const rect = el.getBoundingClientRect();
      const edge = 18;
      const axis = getSizeAxis(el);
      const hasX = axis === "both" || axis.includes("x");
      const hasY = axis === "both" || axis.includes("y");
      const nearBottom = rect.bottom - e.clientY < edge;
      const nearRight = rect.right - e.clientX < edge;
      if ((hasY && nearBottom) || (hasX && nearRight)) {
        _activeResizeSection = el;
        _resizeDrag = {
          el,
          axis,
          hasX,
          hasY,
          startX: e.clientX,
          startY: e.clientY,
          startW: rect.width,
          startH: rect.height,
          minW: getMinSize(el, "x"),
          minH: getMinSize(el, "y"),
        };
        document.body.style.cursor = hasY && !hasX ? "ns-resize" : hasX && !hasY ? "ew-resize" : "nwse-resize";
        e.preventDefault();
      }
    });
  });
  window.addEventListener("pointermove", (e) => {
    if (!_resizeDrag) return;
    const { el, hasX, hasY, startX, startY, startW, startH, minW, minH } = _resizeDrag;
    if (!el) return;
    if (hasY) {
      const nextH = Math.max(minH || 80, startH + (e.clientY - startY));
      el.style.height = `${Math.round(nextH)}px`;
      el.style.flex = "0 0 auto";
    }
    if (hasX) {
      const nextW = Math.max(minW || 120, startW + (e.clientX - startX));
      el.style.width = `${Math.round(nextW)}px`;
      el.style.flex = "0 0 auto";
    }
    scheduleLayoutSync();
  });
  window.addEventListener("pointerup", () => {
    if (!_activeResizeSection) return;
    persistSectionSize(_activeResizeSection);
    _activeResizeSection = null;
    _resizeDrag = null;
    document.body.style.cursor = "";
  });
}

function setupColumnSplitter() {
  const splitter = $("panelSplitter");
  const rail = $("rightRail");
  const panel = document.querySelector(".panel-body");
  if (!splitter || !rail || !panel) return;
  let startX = 0;
  let startWidth = 0;
  const min = 220;
  const max = 560;

  const onMove = (e) => {
    const delta = e.clientX - startX;
    let next = startWidth - delta;
    const panelWidth = panel.getBoundingClientRect().width || 0;
    const maxAllowed = Math.max(min, Math.min(max, panelWidth - 260));
    next = Math.max(min, Math.min(maxAllowed, next));
    layout.rightRailWidth = Math.round(next);
    document.documentElement.style.setProperty("--right-rail-width", `${layout.rightRailWidth}px`);
  };
  const onUp = () => {
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
    saveLayout();
  };

  splitter.addEventListener("mousedown", (e) => {
    startX = e.clientX;
    startWidth = rail.getBoundingClientRect().width;
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}

function setupRowSplitter() {
  const splitter = $("editorSplitter");
  const column = document.querySelector(".editor-column");
  if (!splitter || !column) return;
  let dragging = false;

  const onMove = (e) => {
    if (!dragging) return;
    if (state.aiCollapsed) return;
    const rect = column.getBoundingClientRect();
    const gap = getFlexGap(column);
    const splitterHeight = splitter.getBoundingClientRect().height || 6;
    const available = rect.height - splitterHeight - gap * 2;
    if (available <= 0) return;
    const offset = e.clientY - rect.top - gap;
    let pct = offset / available;
    pct = Math.max(0.2, Math.min(0.8, pct));
    layout.columnSplit = { ...(layout.columnSplit || {}), editorPct: pct };
    applyColumnSplit();
    sizeEditorToWrap();
  };

  const onUp = () => {
    if (!dragging) return;
    dragging = false;
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
    saveLayout();
  };

  splitter.addEventListener("mousedown", (e) => {
    if (state.aiCollapsed) return;
    if (e.target && e.target.closest && e.target.closest("button")) return;
    dragging = true;
    e.preventDefault();
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}

function loadCollapsedCards() {
  try {
    const raw = localStorage.getItem("collapsed_cards");
    const arr = raw ? JSON.parse(raw) : [];
    collapsedCards = new Set(Array.isArray(arr) ? arr : []);
  } catch (e) {
    collapsedCards = new Set();
  }
}

function setCardCollapsed(id, collapsed, persist = true) {
  const card = document.querySelector(`.card[data-card="${id}"]`);
  if (!card) return;
  card.classList.toggle("collapsed", collapsed);
  const btn = card.querySelector("[data-toggle]");
  if (btn) btn.textContent = collapsed ? "Show" : "Hide";
  if (persist) {
    if (collapsed) collapsedCards.add(id);
    else collapsedCards.delete(id);
    localStorage.setItem("collapsed_cards", JSON.stringify([...collapsedCards]));
  }
  syncViewMenuToggleState();
}

function applyCollapsedCards() {
  loadCollapsedCards();
  collapsedCards.forEach((id) => setCardCollapsed(id, true, false));
  syncViewMenuToggleState();
}

function loadHiddenCards() {
  try {
    const raw = localStorage.getItem(HIDDEN_CARDS_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    hiddenCards = new Set(Array.isArray(arr) ? arr : []);
  } catch (e) {
    hiddenCards = new Set();
  }
}

function setCardVisible(id, visible, persist = true) {
  const card = document.querySelector(`.card[data-card="${id}"]`);
  if (!card) return;
  card.hidden = !visible;
  if (persist) {
    if (visible) hiddenCards.delete(id);
    else hiddenCards.add(id);
    localStorage.setItem(HIDDEN_CARDS_KEY, JSON.stringify([...hiddenCards]));
  }
  syncViewMenuToggleState();
}

function applyCardVisibility() {
  loadHiddenCards();
  VIEW_MENU_CARD_IDS.forEach((id) => setCardVisible(id, !hiddenCards.has(id), false));
  syncViewMenuToggleState();
}

function updateSidebarTabs() {
  const autoTab = $("tabAutomations");
  const scriptTab = $("tabScripts");
  const autoActive = isAutomation();
  if (autoTab) {
    autoTab.classList.toggle("active", autoActive);
    autoTab.setAttribute("aria-selected", autoActive ? "true" : "false");
    autoTab.tabIndex = autoActive ? 0 : -1;
  }
  if (scriptTab) {
    scriptTab.classList.toggle("active", !autoActive);
    scriptTab.setAttribute("aria-selected", autoActive ? "false" : "true");
    scriptTab.tabIndex = autoActive ? -1 : 0;
  }
  const listEl = $("automationList");
  if (listEl) {
    listEl.setAttribute("aria-labelledby", autoActive ? "tabAutomations" : "tabScripts");
  }
  const hideBtn = $("hideDisabledToggleBtn");
  if (hideBtn) {
    hideBtn.hidden = !autoActive;
  }
  updateAutomationPanels();
}

function updateAutomationPanels() {
  const show = isAutomation() && !state.capabilitiesView;
  const healthCard = document.querySelector('.card[data-card="health"]');
  const scenarioCard = document.querySelector('.card[data-card="scenario"]');
  if (healthCard) healthCard.hidden = !show || DISABLE_HEALTH_PANEL;
  if (scenarioCard) scenarioCard.hidden = !show;
  renderHealthPanel();
  renderScenarioOutput();
}

function getMinSize(el, axis) {
  if (!el) return 0;
  const styles = getComputedStyle(el);
  if (axis === "x") {
    const w = parseFloat(styles.minWidth || "0");
    return Number.isFinite(w) ? w : 0;
  }
  const h = parseFloat(styles.minHeight || "0");
  return Number.isFinite(h) ? h : 0;
}

function updateEntityUi() {
  const search = $("searchInput");
  if (search) search.placeholder = `Search ${entityLabelPlural()} (alias/desc/id)...`;

  const newBtn = $("newEntityBtn");
  if (newBtn) {
    const label = `New ${entityLabel()}`;
    newBtn.title = label;
    newBtn.setAttribute("aria-label", label);
  }

  const prompt = $("aiPrompt");
  if (prompt) {
    prompt.placeholder = isAutomation()
      ? "e.g. Add a 30 min bedtime window, and do not announce if I am already in bed..."
      : "e.g. Create a script that powers down the living room and turns off the TV...";
  }

  const promptExpanded = $("aiPromptExpanded");
  if (promptExpanded) {
    promptExpanded.placeholder = isAutomation()
      ? "Add your next automation change request..."
      : "Add your next script change request...";
  }

  const createTitle = $("createTitle");
  if (createTitle) createTitle.textContent = `Create a new ${entityLabel()}`;
  const createSubtitle = $("createSubtitle");
  if (createSubtitle) {
    createSubtitle.textContent = `Chat with the Architect to refine details, then finalize and build your ${entityLabel()}.`;
  }
  const createTip = $("createTip");
  if (createTip) {
    createTip.textContent = isAutomation()
      ? "Tip: be specific about entities, times, and preferred scripts (Alexa occupied-room, phone notify, etc)."
      : "Tip: be specific about inputs, expected outputs, and preferred services.";
  }
  const createPrompt = $("promptText");
  if (createPrompt) {
    createPrompt.placeholder = isAutomation()
      ? "e.g. When sleep_time turns on, remind me to set an alarm..."
      : "e.g. A script that sets movie lighting and turns on the TV...";
  }

  if (!state.activeId) {
    $("aTitle").textContent = `Select a ${entityLabel()}`;
    $("aMeta").textContent = "Pick one from the list on the left.";
  }
  syncCombineButton();
  updateArchitectAssistCopy();
  updateArchitectActionState();
}

function setEntityType(type) {
  const next = type === "script" ? "script" : "automation";
  if (state.entityType === next) return;
  if (state.capabilitiesView) {
    closeCapabilitiesView();
  }

  cacheActiveTab();
  state.tabsByType[state.entityType] = state.tabs;
  state.tabCacheByType[state.entityType] = state.tabCache;
  state.lastActiveByType[state.entityType] = state.activeId;

  state.entityType = next;
  if (next !== "automation") {
    clearCombineSelection();
  }
  state.tabs = state.tabsByType[next] || [];
  state.tabCache = state.tabCacheByType[next] || {};
  state.activeId = state.lastActiveByType[next] || null;
  state.architectConversationId = null;
  state.architectContextSent = false;
  state.lastAiPrompt = "";
  state.aiHistory = [];
  aiOutputClear();
  state.health = null;
  state.healthLoading = false;
  state.scenarioResult = null;
  state.scenarioRunning = false;
  renderHealthPanel();
  renderScenarioOutput();

  updateSidebarTabs();
  updateEntityUi();
  syncCapabilitiesUi();
  renderTabs();

  if (state.activeId && restoreTab(state.activeId)) {
    // restored
  } else {
    clearActive();
  }

  loadList();
}

async function openSettingsModal() {
  loadSettings();
  const secret = localStorage.getItem("agent_secret") || "";
  $("settingsAgentSecret").value = secret;
  $("settingsFilePath").value = settings.filePath || "";
  $("settingsViewMode").value = settings.viewMode || "yaml";
  const compareTarget = settings.compareTarget === "latest" ? "version" : (settings.compareTarget || "current");
  $("settingsCompareTarget").value = compareTarget;
  $("settingsCompactList").checked = Boolean(settings.compactList);
  $("settingsAutoOpen").checked = Boolean(settings.autoOpenNew);
  const invertToggle = $("settingsInvertEnter");
  if (invertToggle) invertToggle.checked = Boolean(settings.invertPromptEnter);
  const localEditToggle = $("settingsTryLocalEdit");
  if (localEditToggle) localEditToggle.checked = Boolean(settings.tryLocalEditFirst);
  const allowDiffToggle = $("settingsAllowAiDiff");
  if (allowDiffToggle) allowDiffToggle.checked = Boolean(settings.allowAiDiff);
  const confInput = $("settingsHelperConfidence");
  if (confInput) confInput.value = String(settings.helperMinConfidence ?? DEFAULT_SETTINGS.helperMinConfidence);
  await loadRuntimeSettingsIntoModal();
  openModal("settingsModal");
}

async function loadRuntimeSettingsIntoModal() {
  try {
    const out = await api("/api/admin/runtime");
    if (typeof out?.helper_min_confidence === "number") {
      settings.helperMinConfidence = Math.max(0, Math.min(1, out.helper_min_confidence));
      const confInput = $("settingsHelperConfidence");
      if (confInput) confInput.value = String(settings.helperMinConfidence);
    }
    if (typeof out?.allow_ai_diff === "boolean") {
      settings.allowAiDiff = out.allow_ai_diff;
      const allowDiffToggle = $("settingsAllowAiDiff");
      if (allowDiffToggle) allowDiffToggle.checked = Boolean(settings.allowAiDiff);
    }
    settings.usageCurrency = String(out?.usage_currency || settings.usageCurrency || DEFAULT_SETTINGS.usageCurrency).toUpperCase();

    const agents = await loadConversationAgents();
    populateAgentSelect("settingsBuilderAgent", agents, out?.builder_agent_id || "");
    populateAgentSelect("settingsArchitectAgent", agents, out?.architect_agent_id || "");
    populateAgentSelect("settingsSummaryAgent", agents, out?.summary_agent_id || "");
    populateAgentSelect("settingsCapabilityMapperAgent", agents, out?.capability_mapper_agent_id || "");
    populateAgentSelect("settingsSemanticDiffAgent", agents, out?.semantic_diff_agent_id || "");
    populateAgentSelect("settingsKbSyncAgent", agents, out?.kb_sync_helper_agent_id || "");
    populateAgentSelect("settingsDumbBuilderAgent", agents, out?.dumb_builder_agent_id || "");
    populateAgentSelect("settingsConfirmAgent", agents, out?.confirm_agent_id || "");

    const currencies = Array.isArray(out?.supported_currencies) && out.supported_currencies.length
      ? out.supported_currencies.map((cur) => String(cur || "").toUpperCase()).filter(Boolean)
      : ["GBP", "USD", "EUR", "CAD", "AUD"];
    populateValueSelect("settingsUsageCurrency", currencies, settings.usageCurrency, "");

    const models = Array.isArray(out?.pricing_models) && out.pricing_models.length
      ? out.pricing_models.map((model) => String(model || "").trim()).filter(Boolean)
      : ["gpt-5.2", "gpt-4o-mini"];
    RUNTIME_MODEL_FIELDS.forEach((field) => {
      populateValueSelect(field.selectId, models, out?.[field.key] || "", "Use server default");
    });
  } catch (e) {
    // ignore admin endpoint errors
  }
}

function saveSettingsFromModal() {
  settings.filePath = $("settingsFilePath").value.trim();
  settings.viewMode = $("settingsViewMode").value || "yaml";
  settings.compareTarget = $("settingsCompareTarget").value || "current";
  if (settings.compareTarget === "latest") settings.compareTarget = "version";
  settings.compactList = $("settingsCompactList").checked;
  settings.autoOpenNew = $("settingsAutoOpen").checked;
  const invertToggle = $("settingsInvertEnter");
  settings.invertPromptEnter = invertToggle ? invertToggle.checked : false;
  const localEditToggle = $("settingsTryLocalEdit");
  settings.tryLocalEditFirst = localEditToggle ? localEditToggle.checked : true;
  const allowDiffToggle = $("settingsAllowAiDiff");
  settings.allowAiDiff = allowDiffToggle ? allowDiffToggle.checked : false;
  const confInput = $("settingsHelperConfidence");
  const confValue = parseFloat(confInput ? confInput.value : "");
  settings.helperMinConfidence = Number.isFinite(confValue)
    ? Math.max(0, Math.min(1, confValue))
    : DEFAULT_SETTINGS.helperMinConfidence;
  settings.usageCurrency = String(readAgentSelect("settingsUsageCurrency") || settings.usageCurrency || DEFAULT_SETTINGS.usageCurrency).toUpperCase();
  localStorage.setItem("ui_settings", JSON.stringify(settings));

  const secret = $("settingsAgentSecret").value.trim();
  if (secret) {
    localStorage.setItem("agent_secret", secret);
  } else {
    localStorage.removeItem("agent_secret");
  }
  applySettings();
  setViewMode(settings.viewMode);
  setCompareTarget(settings.compareTarget, { silent: true });
  saveRuntimeSettingsFromModal();
  toast("Settings saved.");
  closeModal("settingsModal");
}

async function saveRuntimeSettingsFromModal() {
  try {
    const payload = {
      helper_min_confidence: settings.helperMinConfidence,
      allow_ai_diff: settings.allowAiDiff,
      usage_currency: settings.usageCurrency,
      builder_agent_id: readAgentSelect("settingsBuilderAgent"),
      architect_agent_id: readAgentSelect("settingsArchitectAgent"),
      summary_agent_id: readAgentSelect("settingsSummaryAgent"),
      capability_mapper_agent_id: readAgentSelect("settingsCapabilityMapperAgent"),
      semantic_diff_agent_id: readAgentSelect("settingsSemanticDiffAgent"),
      kb_sync_helper_agent_id: readAgentSelect("settingsKbSyncAgent"),
      dumb_builder_agent_id: readAgentSelect("settingsDumbBuilderAgent"),
      confirm_agent_id: readAgentSelect("settingsConfirmAgent"),
    };
    RUNTIME_MODEL_FIELDS.forEach((field) => {
      payload[field.key] = readAgentSelect(field.selectId);
    });
    await api("/api/admin/runtime", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  } catch (e) {
    // ignore admin endpoint errors
  }
}

async function copyEnvLine() {
  const path = $("settingsFilePath").value.trim();
  if (!path) return toast("Enter a UNC path first.");
  const line = `AUTOMATIONS_FILE_PATH=${path}`;
  try {
    await navigator.clipboard.writeText(line);
    toast("Copied env line.");
  } catch (e) {
    toast("Copy failed - check browser permissions.");
  }
}

function cacheActiveTab() {
  if (!state.activeId) return;
  state.tabCache[state.activeId] = {
    active: state.active,
    originalYaml: state.originalYaml,
    currentDraft: state.currentDraft,
    dirty: state.dirty,
    versions: state.versions,
    selectedVersionId: state.selectedVersionId,
    selectedVersionYaml: state.selectedVersionYaml,
    latestVersionYaml: state.latestVersionYaml,
    previousSavedYaml: state.previousSavedYaml,
    lastAiPrompt: state.lastAiPrompt,
    aiHistory: state.aiHistory,
    architectConversationId: state.architectConversationId,
    architectContextSent: state.architectContextSent,
  };
}

function renderTabs() {
  const bar = $("tabStrip");
  if (!bar) return;
  bar.innerHTML = "";
  if (!state.tabs.length) {
    bar.innerHTML = `<div class="tab-empty">No tabs</div>`;
    scheduleLayoutSync();
    return;
  }
  for (const id of state.tabs) {
    const meta = state.tabCache[id]?.active || (state.activeId === id ? state.active : null);
    const title = meta?.alias || id;
    const div = document.createElement("div");
    div.className = "tab-item" + (id === state.activeId ? " active" : "");
    div.dataset.id = id;
    div.innerHTML = `
      <span class="tab-title">${escapeHtml(title)}</span>
      <button class="tab-close" data-tab-close="1">x</button>
    `;
    bar.appendChild(div);
  }
  scheduleLayoutSync();
}

function addTab(id) {
  if (!state.tabs.includes(id)) state.tabs.push(id);
  renderTabs();
}

function closeTab(id) {
  if (!id) return;
  const isDirty = (() => {
    if (state.activeId === id) {
      return (state.currentDraft ?? "") !== (getLatestSavedYaml() || "");
    }
    const cached = state.tabCache[id];
    if (!cached) return false;
    return (cached.currentDraft ?? "") !== (cached.originalYaml ?? "");
  })();
  if (isDirty) {
    const ok = confirm("Close this tab and discard unsaved changes?");
    if (!ok) return;
  }
  state.tabs = state.tabs.filter(t => t !== id);
  delete state.tabCache[id];

  if (state.activeId === id) {
    const next = state.tabs[state.tabs.length - 1];
    if (next) {
      activateTab(next);
    } else {
      clearActive();
    }
  }
  renderTabs();
}

function clearActive() {
  state.activeId = null;
  state.active = null;
  state.originalYaml = "";
  state.currentDraft = "";
  state.dirty = false;
  state.selectedVersionId = null;
  state.selectedVersionYaml = "";
  state.latestVersionYaml = "";
  state.previousSavedYaml = "";
  state.versions = [];
  state.aiHistory = [];
  state.health = null;
  state.healthLoading = false;
  state.scenarioResult = null;
  state.scenarioRunning = false;
  setEditorReadOnly(false);
  setEditorValue("");
  setButtons(false);
  setVersionButtons(false);
  syncCapabilitiesUi();
  $("aTitle").textContent = `Select a ${entityLabel()}`;
  $("aMeta").textContent = "Pick one from the list on the left.";
  aiOutputClear();
  renderVersions();
  renderHealthPanel();
  renderScenarioOutput();
  updateDiff();
}

function restoreTab(id) {
  const cached = state.tabCache[id];
  if (!cached) return false;
  state.activeId = id;
  state.active = cached.active;
  state.originalYaml = cached.originalYaml ?? "";
  state.currentDraft = cached.currentDraft ?? "";
  state.dirty = cached.dirty || false;
  state.versions = cached.versions || [];
  state.selectedVersionId = cached.selectedVersionId || null;
  state.selectedVersionYaml = cached.selectedVersionYaml ?? "";
  state.latestVersionYaml = cached.latestVersionYaml ?? state.originalYaml ?? "";
  state.previousSavedYaml = cached.previousSavedYaml ?? "";
  state.lastAiPrompt = cached.lastAiPrompt ?? "";
  state.aiHistory = cached.aiHistory ?? [];
  state.architectConversationId = cached.architectConversationId ?? null;
  state.architectContextSent = cached.architectContextSent ?? false;

  $("aTitle").textContent = state.active?.alias || id;
  $("aMeta").textContent = `${state.active?.source || "Unknown"} - ${id}`;

  if (state.compareTarget === "version" && state.selectedVersionYaml) {
    setEditorReadOnly(true);
    setEditorValue(state.selectedVersionYaml);
  } else {
    setEditorReadOnly(false);
    setEditorValue(state.currentDraft ?? getLatestSavedYaml());
    if (state.compareTarget === "version") {
      state.compareTarget = "current";
    }
  }
  updateCompareTabs();

  setButtons(true);
  setDirty((state.currentDraft ?? "") !== (getLatestSavedYaml() || ""));
  updateEnableButtonFromState(state.active?.state);
  if (isAutomation() && (state.active?.state === null || state.active?.state === undefined || state.active?.state === "")) {
    refreshAutomationState(id);
  }
  renderList();
  setAiHistory(state.aiHistory);
  renderVersions();
  updateDiff();
  if (state.viewMode === "visual") scheduleVisualRender();
  renderTabs();
  syncCapabilitiesUi();
  loadHealth(id);
  renderScenarioOutput();
  return true;
}

async function activateTab(id) {
  if (!id) return;
  if (id === state.activeId) return;
  if (state.compareTarget !== "current") {
    setCompareTarget("current");
  }
  cacheActiveTab();
  if (restoreTab(id)) return;
  await openAutomation(id);
}

function isCardVisible(cardId) {
  const card = document.querySelector(`.card[data-card="${cardId}"]`);
  if (!card) return false;
  return !card.hidden;
}

function syncViewMenuToggleState() {
  const setChecked = (id, value) => {
    const el = $(id);
    if (el) el.checked = Boolean(value);
  };
  setChecked("viewToggleSidebar", !state.sidebarCollapsed);
  setChecked("viewToggleAi", !state.aiCollapsed);
  setChecked("viewToggleRightRail", !state.railCollapsed);
  setChecked("viewToggleActivity", isCardVisible("activity"));
  setChecked("viewToggleUsage", isCardVisible("usage"));
  setChecked("viewToggleVersions", isCardVisible("versions"));
}

function setViewMenuOpen(open) {
  const menu = $("viewMenu");
  const btn = $("viewMenuBtn");
  if (!menu || !btn) return;
  const isOpen = Boolean(open);
  menu.hidden = !isOpen;
  btn.setAttribute("aria-expanded", isOpen ? "true" : "false");
}

function setSidebarCollapsed(collapsed) {
  state.sidebarCollapsed = collapsed;
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  document.querySelectorAll("[data-toggle-sidebar]").forEach((btn) => {
    const label = collapsed ? "Show list" : "Hide list";
    btn.textContent = label;
    btn.title = label;
  });
  syncViewMenuToggleState();
}

function setRailCollapsed(collapsed) {
  state.railCollapsed = collapsed;
  document.body.classList.toggle("rail-collapsed", collapsed);
  document.querySelectorAll("[data-toggle-rail]").forEach((btn) => {
    const label = collapsed ? "Show right panel" : "Hide right panel";
    btn.textContent = label;
    btn.title = label;
  });
  relocateAiOutput();
  syncViewMenuToggleState();
}

function captureAiOutputHome() {
  const card = $("aiOutputCard");
  const slot = $("aiOutputSlot");
  if (!card || !slot) return;
  if (card.parentElement !== slot) slot.appendChild(card);
  state.aiOutputHome = { parent: slot, next: card.nextSibling };
}

function relocateAiOutput() {
  const card = $("aiOutputCard");
  const slot = $("aiOutputSlot");
  if (!card || !slot) return;
  if (card.parentElement !== slot) slot.appendChild(card);
  card.classList.remove("ai-output-docked");
}

function setVersionButtons(enabled) {
  const loadBtn = $("loadVersionBtn");
  const applyBtn = $("applyVersionBtn");
  if (loadBtn) loadBtn.disabled = !enabled;
  if (applyBtn) applyBtn.disabled = !enabled;
}

function renderVersions() {
  const list = $("versionList");
  if (!list) return;

  if (state.capabilitiesView) {
    list.innerHTML = `<div class="empty">Knowledgebase view (versions hidden).</div>`;
    setVersionButtons(false);
    updateCompareTabs();
    return;
  }

  if (!state.activeId) {
    list.innerHTML = `<div class="empty">Select a ${entityLabel()}.</div>`;
    setVersionButtons(false);
    updateCompareTabs();
    return;
  }

  if (!state.versions.length) {
    list.innerHTML = `<div class="empty">No versions yet.</div>`;
    setVersionButtons(false);
    updateCompareTabs();
    return;
  }

  list.innerHTML = "";
  for (const v of state.versions) {
    const div = document.createElement("div");
    div.className = "version-item" + (v.id === state.selectedVersionId ? " active" : "");
    div.onclick = () => selectVersion(v.id);

    const stamp = formatStamp(v.ts || v.timestamp || "");
    const size = typeof v.size === "number" ? `${Math.round(v.size / 1024)} KB` : "";
    const label = v.label || v.version || v.reason || "Version";
    const rawDesc = v.description || v.note || v.prompt || "";
    const desc = rawDesc.length > 240 ? `${rawDesc.slice(0, 237)}...` : rawDesc;
    const summary = v.summary || v.diff_summary || v.delta || "";
    let fileLabel = v.id || "";
    if (label && fileLabel && !fileLabel.includes(label)) {
      fileLabel = `${label} - ${fileLabel}`;
    }
    const summaryHtml = summary ? `<div class="version-summary">${escapeHtml(summary)}</div>` : "";

    div.innerHTML = `
      <div class="version-top">
        <div class="version-title">${escapeHtml(label)}</div>
        <div class="version-meta">${escapeHtml(stamp)}</div>
      </div>
      <div class="version-sub">${escapeHtml(fileLabel)} ${size ? ` - ${size}` : ""}</div>
      ${summaryHtml}
      <div class="version-desc" data-version-desc></div>
    `;
    const descWrap = div.querySelector("[data-version-desc]");
    if (descWrap) {
      if (v.id === state.selectedVersionId) {
        const textarea = document.createElement("textarea");
        textarea.className = "version-desc-input";
        textarea.placeholder = "Add a description for this version...";
        textarea.value = rawDesc;
        textarea.addEventListener("click", (e) => e.stopPropagation());
        textarea.addEventListener("input", (e) => e.stopPropagation());

        const actions = document.createElement("div");
        actions.className = "version-desc-actions";
        const saveBtn = document.createElement("button");
        saveBtn.className = "btn ghost tiny";
        saveBtn.textContent = "Save description";
        saveBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          saveVersionDescription(v.id, textarea.value);
        });
        actions.appendChild(saveBtn);

        descWrap.appendChild(textarea);
        descWrap.appendChild(actions);
      } else if (desc) {
        descWrap.classList.add("version-note");
        descWrap.textContent = desc;
      }
    }
    list.appendChild(div);
  }

  autoSizeVersionsCard();
  setVersionButtons(Boolean(state.selectedVersionId));
  updateCompareTabs();
}

function autoSizeVersionsCard() {
  const card = document.querySelector('.card[data-card="versions"]');
  if (!card) return;
  if (layout?.sizes?.versions?.height) return;
  const count = Math.max(0, state.versions.length || 0);
  const base = 240;
  const per = 44;
  const max = 640;
  const desired = Math.min(max, base + Math.min(count, 8) * per);
  card.style.height = `${desired}px`;
  card.style.flex = "0 0 auto";
}

async function fetchVersionYaml(versionId) {
  if (!state.activeId || !versionId) return "";
  const data = await api(`${entityEndpoint()}/${encodeURIComponent(state.activeId)}/versions/${encodeURIComponent(versionId)}`);
  return data.yaml || "";
}

async function saveVersionDescription(versionId, description) {
  if (!state.activeId || !versionId) return;
  const desc = (description || "").trim();
  try {
    const out = await api(`${entityEndpoint()}/${encodeURIComponent(state.activeId)}/versions/${encodeURIComponent(versionId)}`, {
      method: "PATCH",
      body: JSON.stringify({ description: desc }),
    });
    const entry = state.versions.find((v) => v.id === versionId);
    if (entry) {
      entry.description = out?.description ?? desc;
      entry.note = entry.note || "";
    }
    toast("Version description saved.");
    renderVersions();
  } catch (e) {
    toast("Description save failed - check log", 3000);
    log(`Version description save failed: ${e.message || e}`);
  }
}

async function ensureLatestVersionYaml() {
  const latestId = state.versions.length ? state.versions[0].id : null;
  state.latestVersionId = latestId;
  if (!latestId) {
    state.latestVersionYaml = state.originalYaml || "";
    return;
  }
  if (latestId === state.selectedVersionId && state.selectedVersionYaml) {
    state.latestVersionYaml = state.selectedVersionYaml;
    return;
  }
  try {
    state.latestVersionYaml = await fetchVersionYaml(latestId);
  } catch (e) {
    state.latestVersionYaml = "";
    log(`Latest version load failed: ${e.message || e}`);
  }
}

async function ensurePreviousSavedYaml() {
  if (state.versions.length < 2) {
    state.previousSavedYaml = "";
    return;
  }
  const prevId = state.versions[1].id;
  try {
    state.previousSavedYaml = await fetchVersionYaml(prevId);
  } catch (e) {
    state.previousSavedYaml = "";
    log(`Previous version load failed: ${e.message || e}`);
  }
}

async function loadVersions(id) {
  if (!id) return;
  try {
    const data = await api(`${entityEndpoint()}/${encodeURIComponent(id)}/versions`);
    state.versions = data.items || [];
  } catch (e) {
    const msg = String(e.message || e);
    if (msg.startsWith("404")) {
      try {
        await api(`${entityEndpoint()}/${encodeURIComponent(id)}/versions`, {
          method: "POST",
          body: JSON.stringify({
            yaml: getLatestSavedYaml(),
            reason: "loaded_seed",
          }),
        });
        const data = await api(`${entityEndpoint()}/${encodeURIComponent(id)}/versions`);
        state.versions = data.items || [];
      } catch (err) {
        state.versions = [];
        log(`Version seed failed: ${err.message || err}`);
      }
    } else {
      state.versions = [];
      log(`Version load failed: ${e.message || e}`);
    }
  }
  renderVersions();
  await ensureLatestVersionYaml();
  await ensurePreviousSavedYaml();
  if (state.selectedVersionId && !state.versions.find(v => v.id === state.selectedVersionId)) {
    state.selectedVersionId = null;
  }
  if (state.versions.length && !state.selectedVersionId) {
    await selectVersion(state.versions[0].id, { preview: false });
  } else {
    updateDiff();
    updateCompareTabs();
  }
}

async function selectVersion(id, options = {}) {
  if (!state.activeId || !id) return;
  const { preview = true } = options;
  state.selectedVersionId = id;
  renderVersions();
  try {
    state.selectedVersionYaml = await fetchVersionYaml(id);
    if (id === state.latestVersionId) {
      state.latestVersionYaml = state.selectedVersionYaml;
    }
    if (preview) {
      setCompareTarget("version");
    } else {
      updateDiff();
      updateCompareTabs();
    }
  } catch (e) {
    state.selectedVersionYaml = "";
    updateDiff();
    updateCompareTabs();
    toast("Version load failed - check log", 3000);
    log(`Version load failed: ${e.message || e}`);
  }
}

async function loadSelectedVersionToEditor() {
  if (!state.selectedVersionYaml) return;
  const ok = await openConfirmModal({
    title: "Restore version",
    subtitle: "Replace the live draft with this version.",
    message: "Load this version into the editor? This will replace the current editor content.",
    confirmText: "Restore version",
  });
  if (!ok) return;
  if (state.compareTarget !== "current") {
    setCompareTarget("current");
  }
  state.currentDraft = state.selectedVersionYaml;
  setEditorValue(state.selectedVersionYaml);
  setDirty(state.currentDraft !== (getLatestSavedYaml() || ""));
  updateDiff();
  if (state.viewMode === "visual") scheduleVisualRender();
  toast("Version loaded into editor.");
}

async function applySelectedVersionToHa() {
  if (!state.activeId || !state.selectedVersionYaml) return;
  const summary = buildChangeSummary(state.originalYaml || "", state.selectedVersionYaml);
  const ok = confirm(`Restore this ${entityLabel()} version to Home Assistant now? A backup will be created first.\n\n${summary}`);
  if (!ok) return;
  try {
    const out = await api(`${entityEndpoint()}/${encodeURIComponent(state.activeId)}/apply`, {
      method: "POST",
      body: JSON.stringify({ yaml: state.selectedVersionYaml, note: summary }),
    });
    toast("Version restored.");
    const haId = out?.automation_id || out?.script_id || out?.entity_id;
    log(`Applied ${state.activeId}. ${haId ? `HA id: ${haId}` : ""}`);
    state.previousSavedYaml = state.originalYaml;
    state.originalYaml = state.selectedVersionYaml;
    state.currentDraft = state.selectedVersionYaml;
    state.latestVersionYaml = state.selectedVersionYaml;
    setEditorReadOnly(false);
    setEditorValue(state.selectedVersionYaml);
    setDirty(false);
    updateEnableButtonFromState(state.active?.state);
    await loadList();
    await loadVersions(state.activeId);
    await loadHealth(state.activeId);
  } catch (e) {
    toast("Apply failed - check log", 3500);
    log(`Apply failed: ${e.message || e}`);
  }
}

function parseYamlToObject(text) {
  try {
    return jsyaml.load(text || "");
  } catch (e) {
    return null;
  }
}

function normalizeAutomationObject(obj) {
  if (Array.isArray(obj)) {
    obj = obj.find((x) => x && typeof x === "object") || obj[0];
  }
  if (obj && typeof obj === "object" && Array.isArray(obj.automation)) {
    obj = obj.automation.find((x) => x && typeof x === "object") || obj.automation[0];
  }
  return obj && typeof obj === "object" ? obj : null;
}

function toList(val) {
  if (!val) return [];
  if (Array.isArray(val)) return val;
  return [val];
}

function summarizeTrigger(t) {
  if (!t || typeof t !== "object") return "trigger";
  const platform = (t.platform || t.trigger || "").trim();
  if (platform === "time") {
    if (t.at) return `time at ${t.at}`;
    return `time ${t.after || ""}-${t.before || ""}`.trim();
  }
  if (platform === "state") {
    if (t.entity_id && t.to !== undefined) return `state ${t.entity_id} -> ${t.to}`;
    if (t.entity_id) return `state ${t.entity_id}`;
  }
  if (platform === "numeric_state") {
    return `numeric ${t.entity_id || ""} ${t.above || ""}-${t.below || ""}`.trim();
  }
  if (platform === "event") {
    return `event ${t.event_type || ""}`.trim();
  }
  return platform ? `${platform} trigger` : "trigger";
}

function summarizeCondition(c) {
  if (!c || typeof c !== "object") return "condition";
  const cond = (c.condition || "").trim();
  if (cond === "state") {
    if (c.entity_id && c.state !== undefined) return `state ${c.entity_id} = ${c.state}`;
    if (c.entity_id) return `state ${c.entity_id}`;
  }
  if (cond === "numeric_state") {
    return `numeric ${c.entity_id || ""} ${c.above || ""}-${c.below || ""}`.trim();
  }
  if (cond === "time") {
    return `time ${c.after || ""}-${c.before || ""}`.trim();
  }
  return cond ? `${cond} condition` : "condition";
}

function summarizeAction(a) {
  if (!a || typeof a !== "object") return "action";
  if (a.service) {
    const target = a.target || {};
    const entity = typeof target === "object" ? target.entity_id : null;
    if (entity) return `service ${a.service} -> ${entity}`;
    return `service ${a.service}`;
  }
  if (a.choose) {
    return `choose (${Array.isArray(a.choose) ? a.choose.length : 0} options)`;
  }
  if (a.delay) return `delay ${a.delay}`;
  return "action";
}

function diffList(baseList, nextList) {
  const base = new Map();
  const next = new Map();
  baseList.forEach((item) => base.set(item, (base.get(item) || 0) + 1));
  nextList.forEach((item) => next.set(item, (next.get(item) || 0) + 1));
  const added = [];
  const removed = [];
  next.forEach((count, item) => {
    const diff = count - (base.get(item) || 0);
    if (diff > 0) added.push(...Array(diff).fill(item));
  });
  base.forEach((count, item) => {
    const diff = count - (next.get(item) || 0);
    if (diff > 0) removed.push(...Array(diff).fill(item));
  });
  return { added, removed };
}

function buildSemanticDiff(baseText, compareText) {
  const baseObj = normalizeAutomationObject(parseYamlToObject(baseText));
  const nextObj = normalizeAutomationObject(parseYamlToObject(compareText));
  if (!baseObj || !nextObj) return null;

  const baseTriggers = toList(baseObj.trigger || baseObj.triggers).map(summarizeTrigger);
  const nextTriggers = toList(nextObj.trigger || nextObj.triggers).map(summarizeTrigger);
  const baseConditions = toList(baseObj.condition || baseObj.conditions).map(summarizeCondition);
  const nextConditions = toList(nextObj.condition || nextObj.conditions).map(summarizeCondition);
  const baseActionsRaw = baseObj.action || baseObj.actions || baseObj.sequence;
  const nextActionsRaw = nextObj.action || nextObj.actions || nextObj.sequence;
  const baseActions = toList(baseActionsRaw).map(summarizeAction);
  const nextActions = toList(nextActionsRaw).map(summarizeAction);

  return {
    triggers: diffList(baseTriggers, nextTriggers),
    conditions: diffList(baseConditions, nextConditions),
    actions: diffList(baseActions, nextActions),
  };
}

function buildSemanticSummary(baseText, nextText) {
  const diff = buildSemanticDiff(baseText, nextText);
  if (!diff) return "";
  const parts = [];
  const { triggers, conditions, actions } = diff;

  if (triggers.added.length) parts.push(triggers.added.length === 1 ? `Added trigger: ${triggers.added[0]}` : `Added ${triggers.added.length} triggers`);
  if (triggers.removed.length) parts.push(triggers.removed.length === 1 ? `Removed trigger: ${triggers.removed[0]}` : `Removed ${triggers.removed.length} triggers`);
  if (conditions.added.length) parts.push(conditions.added.length === 1 ? `Added condition: ${conditions.added[0]}` : `Added ${conditions.added.length} conditions`);
  if (conditions.removed.length) parts.push(conditions.removed.length === 1 ? `Removed condition: ${conditions.removed[0]}` : `Removed ${conditions.removed.length} conditions`);
  if (actions.added.length && actions.removed.length && actions.added.length === 1 && actions.removed.length === 1) {
    parts.push(`Changed action from ${actions.removed[0]} -> ${actions.added[0]}`);
  } else {
    if (actions.added.length) parts.push(actions.added.length === 1 ? `Added action: ${actions.added[0]}` : `Added ${actions.added.length} actions`);
    if (actions.removed.length) parts.push(actions.removed.length === 1 ? `Removed action: ${actions.removed[0]}` : `Removed ${actions.removed.length} actions`);
  }

  if (!parts.length) return "No semantic changes.";
  return parts.join(". ");
}

function renderDiff(baseText, compareText) {
  const box = $("diffBox");
  if (!box) return;
  if (DISABLE_SEMANTIC_DIFF) {
    box.innerHTML = `<div class="muted">Semantic diff temporarily disabled.</div>`;
    return;
  }
  if (state.capabilitiesView) {
    box.innerHTML = `<div class="muted">Knowledgebase view.</div>`;
    return;
  }
  if (!compareText) {
    box.innerHTML = `<div class="muted">Select a version to compare.</div>`;
    return;
  }
  const diff = buildSemanticDiff(baseText, compareText);
  if (!diff) {
    box.innerHTML = `<div class="muted">Unable to compute semantic diff.</div>`;
    return;
  }
  const groups = [
    ["Triggers", diff.triggers],
    ["Conditions", diff.conditions],
    ["Actions", diff.actions],
  ];
  let html = "";
  for (const [label, group] of groups) {
    const added = group.added || [];
    const removed = group.removed || [];
    if (!added.length && !removed.length) continue;
    html += `<div class="diff-group"><div class="diff-group-title">${label}</div>`;
    added.forEach((item) => {
      html += `<div class="diff-item add"><span class="diff-label">+</span>${escapeHtml(item)}</div>`;
    });
    removed.forEach((item) => {
      html += `<div class="diff-item remove"><span class="diff-label">-</span>${escapeHtml(item)}</div>`;
    });
    html += `</div>`;
  }
  box.innerHTML = html || `<div class="muted">No semantic changes.</div>`;
}

function clearDiffMarks() {
  if (!state.diffMarks.length) return;
  state.diffMarks.forEach((m) => {
    try { m.clear(); } catch (e) {}
  });
  state.diffMarks = [];
}

function flashEditorChanges(baseText, compareText) {
  if (!state.editor || typeof Diff === "undefined" || !Diff.diffLines) return;
  clearDiffMarks();

  const currentText = compareText !== undefined ? compareText : state.editor.getValue();
  const diff = Diff.diffLines(baseText || "", currentText || "");
  let currLine = 0;

  diff.forEach((part) => {
    const lines = part.value.split("\n");
    if (lines.length && lines[lines.length - 1] === "") lines.pop();
    const lineCount = lines.length;

    if (part.added) {
      const from = { line: currLine, ch: 0 };
      const endLine = Math.max(currLine + lineCount - 1, currLine);
      const to = { line: endLine, ch: state.editor.getLine(endLine)?.length || 0 };
      const mark = state.editor.markText(from, to, { className: "cm-diff-flash-add" });
      state.diffMarks.push(mark);
      currLine += lineCount;
      return;
    }
    if (part.removed) {
      const line = Math.min(currLine, state.editor.lineCount() - 1);
      if (line >= 0) {
        const from = { line, ch: 0 };
        const to = { line, ch: state.editor.getLine(line)?.length || 0 };
        const mark = state.editor.markText(from, to, { className: "cm-diff-flash-del" });
        state.diffMarks.push(mark);
      }
      return;
    }

    currLine += lineCount;
  });

  if (state.diffMarks.length) {
    setTimeout(clearDiffMarks, 3000);
  }
}

function lcsLength(aLines, bLines) {
  const aLen = aLines.length;
  const bLen = bLines.length;
  if (!aLen || !bLen) return 0;
  const dp = new Array(bLen + 1).fill(0);
  for (let i = 1; i <= aLen; i++) {
    let prev = 0;
    const aVal = aLines[i - 1];
    for (let j = 1; j <= bLen; j++) {
      const temp = dp[j];
      if (aVal === bLines[j - 1]) {
        dp[j] = prev + 1;
      } else {
        dp[j] = Math.max(dp[j], dp[j - 1]);
      }
      prev = temp;
    }
  }
  return dp[bLen];
}

function diffLineCounts(baseText, nextText) {
  const baseCounts = new Map();
  const nextCounts = new Map();
  toLineArray(baseText).forEach((line) => {
    const key = line.trim();
    if (!key) return;
    baseCounts.set(key, (baseCounts.get(key) || 0) + 1);
  });
  toLineArray(nextText).forEach((line) => {
    const key = line.trim();
    if (!key) return;
    nextCounts.set(key, (nextCounts.get(key) || 0) + 1);
  });
  let added = 0;
  let removed = 0;
  nextCounts.forEach((count, line) => {
    const diff = count - (baseCounts.get(line) || 0);
    if (diff > 0) added += diff;
  });
  baseCounts.forEach((count, line) => {
    const diff = count - (nextCounts.get(line) || 0);
    if (diff > 0) removed += diff;
  });
  return { added, removed };
}

function buildDiffStats(baseText, nextText) {
  return diffLineCounts(baseText || "", nextText || "");
}

function buildChangeSummary(baseText, nextText) {
  const semantic = buildSemanticSummary(baseText, nextText);
  const stats = buildDiffStats(baseText, nextText);
  let summary = semantic || `Changes: +${stats.added} / -${stats.removed} lines.`;
  if (state.lastAiPrompt) {
    const snippet = state.lastAiPrompt.length > 120 ? `${state.lastAiPrompt.slice(0, 117)}...` : state.lastAiPrompt;
    summary += `\nAI prompt: "${snippet}"`;
  }
  return summary;
}

function getLatestSavedYaml() {
  return state.originalYaml || state.latestVersionYaml || "";
}

function getSavedComparisonYaml() {
  return getLatestSavedYaml() || state.previousSavedYaml || "";
}

function updateDiff() {
  const base = state.currentDraft ?? (state.editor ? state.editor.getValue() : "");
  const compareText = state.selectedVersionYaml || "";
  if (!compareText) {
    renderDiff("", "");
    return;
  }
  renderDiff(base, compareText);
}

function setViewMode(mode) {
  state.viewMode = mode;
  $("viewYamlBtn").classList.toggle("active", mode === "yaml");
  $("viewVisualBtn").classList.toggle("active", mode === "visual");
  const yamlView = $("yamlView");
  const visualView = $("visualView");
  if (mode === "visual") {
    yamlView.classList.add("hidden");
    visualView.classList.add("active");
    visualView.setAttribute("aria-hidden", "false");
    renderVisual();
  } else {
    yamlView.classList.remove("hidden");
    visualView.classList.remove("active");
    visualView.setAttribute("aria-hidden", "true");
    if (state.editor) {
      setTimeout(() => {
        state.editor.refresh();
        sizeEditorToWrap();
      }, 0);
    }
  }
}


function getSelectedVersionMeta() {
  if (!state.selectedVersionId) return null;
  return state.versions.find((v) => v.id === state.selectedVersionId) || null;
}

function versionTabLabel() {
  const meta = getSelectedVersionMeta();
  if (meta?.label) return meta.label;
  if (!meta?.id) return "Version";
  const short = meta.id.length > 10 ? `${meta.id.slice(0, 10)}...` : meta.id;
  return short;
}
function updateCompareTabs() {
  const currentBtn = $("compareCurrentBtn");
  const versionBtn = $("compareVersionBtn");
  if (state.capabilitiesView) {
    if (currentBtn) {
      currentBtn.classList.toggle("active", true);
      currentBtn.disabled = true;
    }
    if (versionBtn) {
      versionBtn.disabled = true;
      versionBtn.textContent = "Version";
      versionBtn.classList.remove("active");
    }
    const restoreBtn = $("restoreVersionBtn");
    if (restoreBtn) {
      restoreBtn.hidden = true;
      restoreBtn.disabled = true;
    }
    return;
  }
  if (currentBtn) currentBtn.classList.toggle("active", state.compareTarget === "current");
  if (versionBtn) {
    const hasVersion = Boolean(state.selectedVersionId);
    versionBtn.disabled = !hasVersion;
    versionBtn.textContent = hasVersion ? versionTabLabel() : "Version";
    versionBtn.classList.toggle("active", state.compareTarget === "version");
  }
  const restoreBtn = $("restoreVersionBtn");
  if (restoreBtn) {
    const show = state.compareTarget === "version" && Boolean(state.selectedVersionYaml);
    restoreBtn.hidden = !show;
    restoreBtn.disabled = !show;
  }
}

function setCompareTarget(target, options = {}) {
  const { silent = false } = options;
  if (!state.editor) {
    state.compareTarget = target;
    return;
  }

  const next = target === "version" ? "version" : "current";
  const prev = state.compareTarget;
  if (prev === "current" && next !== "current") {
    state.currentDraft = state.editor ? state.editor.getValue() : getCurrentDraftText();
  }

  state.compareTarget = next;
  updateCompareTabs();

  if (next === "version") {
    if (!state.selectedVersionYaml) {
      if (!silent) toast("Select a version first.");
      state.compareTarget = "current";
      updateCompareTabs();
      setEditorReadOnly(false);
      setEditorValue(state.currentDraft ?? getLatestSavedYaml());
      return;
    }
    setEditorReadOnly(true);
    setEditorValue(state.selectedVersionYaml);
  } else {
    setEditorReadOnly(false);
    if (state.currentDraft === null || state.currentDraft === undefined) {
      state.currentDraft = getLatestSavedYaml();
    }
    setEditorValue(state.currentDraft);
  }

  setDirty((state.currentDraft ?? "") !== (getLatestSavedYaml() || ""));
  setButtons(Boolean(state.activeId));
  updateEnableButtonFromState(state.active?.state);
  updateDiff();

  if (next === "version") {
    flashEditorChanges(state.currentDraft ?? "", state.selectedVersionYaml || "");
  } else {
    const fromText = prev === "version" ? (state.selectedVersionYaml || "") : getLatestSavedYaml();
    flashEditorChanges(fromText, state.currentDraft ?? "");
  }
}

let _visualTimer;
function scheduleVisualRender() {
  clearTimeout(_visualTimer);
  _visualTimer = setTimeout(renderVisual, 200);
}

function formatIdList(val, max = 3) {
  if (!val) return "";
  const items = Array.isArray(val) ? val : [val];
  const clean = items.map((v) => String(v)).filter(Boolean);
  if (!clean.length) return "";
  const shown = clean.slice(0, max);
  const more = clean.length > max ? ` +${clean.length - max}` : "";
  return `${shown.join(", ")}${more}`;
}

function summarizeTarget(item) {
  const target = item?.target || {};
  const entity = target.entity_id || item?.entity_id;
  if (entity) return formatIdList(entity);
  const device = target.device_id || item?.device_id;
  if (device) return `device:${formatIdList(device)}`;
  const area = target.area_id || item?.area_id;
  if (area) return `area:${formatIdList(area)}`;
  return "";
}

function summarizeTriggerItem(item) {
  const alias = item.alias ? `${item.alias} - ` : "";
  const platform = item.platform || "";
  if (platform === "state") {
    const entity = formatIdList(item.entity_id);
    const to = item.to ? ` -> ${item.to}` : "";
    const from = item.from ? ` from ${item.from}` : "";
    return `${alias}Trigger: state ${entity}${from}${to}`.trim();
  }
  if (platform === "time") {
    const at = formatIdList(item.at);
    const after = item.after ? ` after ${item.after}` : "";
    const before = item.before ? ` before ${item.before}` : "";
    const atText = at ? ` at ${at}` : "";
    return `${alias}Trigger: time${atText}${after}${before}`.trim();
  }
  if (platform === "sun") {
    const event = item.event ? ` ${item.event}` : "";
    return `${alias}Trigger: sun${event}`.trim();
  }
  if (platform === "event") {
    const eventType = item.event_type ? ` ${item.event_type}` : "";
    return `${alias}Trigger: event${eventType}`.trim();
  }
  if (platform === "device") {
    const type = item.type ? ` ${item.type}` : "";
    return `${alias}Trigger: device${type}`.trim();
  }
  if (platform === "calendar") {
    const entity = formatIdList(item.entity_id);
    return `${alias}Trigger: calendar ${entity}`.trim();
  }
  if (platform === "zone") {
    const entity = formatIdList(item.entity_id);
    const zone = formatIdList(item.zone);
    return `${alias}Trigger: zone ${entity} -> ${zone}`.trim();
  }
  if (platform) return `${alias}Trigger: ${platform}`.trim();
  if (item.event_type) return `${alias}Trigger: event ${item.event_type}`.trim();
  return `${alias}Trigger`.trim();
}

function summarizeConditionItem(item) {
  const alias = item.alias ? `${item.alias} - ` : "";
  const cond = item.condition || "";
  if (cond === "state") {
    const entity = formatIdList(item.entity_id);
    const state = item.state ? ` = ${formatIdList(item.state)}` : "";
    return `${alias}Condition: state ${entity}${state}`.trim();
  }
  if (cond === "numeric_state") {
    const entity = formatIdList(item.entity_id);
    const above = item.above !== undefined ? ` > ${item.above}` : "";
    const below = item.below !== undefined ? ` < ${item.below}` : "";
    return `${alias}Condition: numeric ${entity}${above}${below}`.trim();
  }
  if (cond === "time") {
    const after = item.after ? ` after ${item.after}` : "";
    const before = item.before ? ` before ${item.before}` : "";
    const weekday = item.weekday ? ` weekdays ${formatIdList(item.weekday)}` : "";
    return `${alias}Condition: time${after}${before}${weekday}`.trim();
  }
  if (cond === "sun") {
    const after = item.after ? ` after ${item.after}` : "";
    const before = item.before ? ` before ${item.before}` : "";
    return `${alias}Condition: sun${after}${before}`.trim();
  }
  if (cond === "zone") {
    const entity = formatIdList(item.entity_id);
    const zone = formatIdList(item.zone);
    return `${alias}Condition: zone ${entity} in ${zone}`.trim();
  }
  if (cond === "device") {
    const entity = formatIdList(item.entity_id);
    const type = item.type ? ` ${item.type}` : "";
    return `${alias}Condition: device ${entity}${type}`.trim();
  }
  if (cond === "calendar") {
    const entity = formatIdList(item.entity_id);
    return `${alias}Condition: calendar ${entity}`.trim();
  }
  if (cond === "trigger") {
    const id = formatIdList(item.id || item.ids || item.trigger_id);
    return `${alias}Condition: trigger ${id}`.trim();
  }
  if (cond === "template") return `${alias}Condition: template`;
  if (cond) return `${alias}Condition: ${cond}`.trim();
  return `${alias}Condition`.trim();
}

function summarizeActionItem(item) {
  const alias = item.alias ? `${item.alias} - ` : "";
  if (item.service || item.service_template) {
    const service = item.service || item.service_template;
    const target = summarizeTarget(item);
    return `${alias}Action: ${service}${target ? ` -> ${target}` : ""}`.trim();
  }
  if (item.choose) return `${alias}Choose (${item.choose.length || 0} options)`.trim();
  if (item.if) return `${alias}If (${item.if.length || 0} steps)`.trim();
  if (item.repeat) return `${alias}Repeat`.trim();
  if (item.parallel) return `${alias}Parallel (${item.parallel.length || 0} branches)`.trim();
  if (item.delay) return `${alias}Delay: ${item.delay}`.trim();
  if (item.wait_for_trigger) return `${alias}Wait for trigger`.trim();
  if (item.wait_template) return `${alias}Wait template`.trim();
  if (item.condition) return `${alias}Condition: ${item.condition}`.trim();
  if (item.variables) return `${alias}Set variables`.trim();
  return `${alias}Action`.trim();
}

function summarizeItem(kind, item) {
  if (!item || typeof item !== "object") return `${kind} item`;
  if (kind === "trigger") return summarizeTriggerItem(item);
  if (kind === "condition") return summarizeConditionItem(item);
  if (kind === "action") return summarizeActionItem(item);
  if (kind === "variables") return "Variables";
  return `${kind} item`;
}

function renderSection(title, items, kind) {
  const safeTitle = escapeHtml(title);
  let html = `<details class="v-section" open><summary>${safeTitle}</summary>`;
  if (!items.length) {
    html += `<div class="v-item"><div class="muted">No ${safeTitle.toLowerCase()}.</div></div>`;
  } else {
    for (const item of items) {
      const summary = escapeHtml(summarizeItem(kind, item));
      let detail = "";
      try {
        detail = jsyaml.dump(item, { noRefs: true });
      } catch (e) {
        detail = String(item);
      }
      html += `<details class="v-item"><summary>${summary}</summary><pre>${escapeHtml(detail)}</pre></details>`;
    }
  }
  html += `</details>`;
  return html;
}

function renderVisual() {
  const host = $("visualView");
  if (!host) return;
  const yamlText = state.editor ? state.editor.getValue() : (state.originalYaml || "");
  if (!yamlText.trim()) {
    host.innerHTML = `<div class="muted">No ${entityLabel()} loaded.</div>`;
    return;
  }
  let obj;
  try {
    obj = jsyaml.load(yamlText);
  } catch (e) {
    host.innerHTML = `<div class="muted">YAML parse error: ${escapeHtml(e.message || e)}</div>`;
    return;
  }

  // If we got a list, try to use the first automation
  if (Array.isArray(obj)) {
    obj = obj.find(x => x && typeof x === "object") || obj[0];
  }
  if (obj && typeof obj === "object" && Array.isArray(obj.automation)) {
    obj = obj.automation.find(x => x && typeof x === "object") || obj.automation[0];
  }

  if (!obj || typeof obj !== "object") {
    host.innerHTML = `<div class="muted">This ${entityLabel()} is not a YAML object.</div>`;
    return;
  }

  const alias = escapeHtml(obj.alias || "(no alias)");
  const desc = escapeHtml(obj.description || "");
  let html = `<details class="v-section" open><summary>${alias}</summary>`;
  if (desc) html += `<div class="v-item"><div class="muted">${desc}</div></div>`;
  html += `</details>`;

  const toArray = (val) => {
    if (!val) return [];
    if (Array.isArray(val)) return val;
    return [val];
  };

  if (isAutomation()) {
    const triggers = toArray(obj.trigger || obj.triggers);
    const conditions = toArray(obj.condition || obj.conditions);
    const actions = toArray(obj.action || obj.actions || obj.sequence);
    const variables = obj.variables ? [obj.variables] : [];

    html += renderSection("Triggers", triggers, "trigger");
    html += renderSection("Conditions", conditions, "condition");
    html += renderSection("Actions", actions, "action");
    html += renderSection("Variables", variables, "variables");
  } else {
    const sequence = toArray(obj.sequence || obj.action || obj.actions);
    const variables = obj.variables ? [obj.variables] : [];
    html += renderSection("Sequence", sequence, "action");
    html += renderSection("Variables", variables, "variables");
  }

  host.innerHTML = html;
}


function setButtons(enabled) {
  const allowActions = enabled && !state.capabilitiesView;
  const allowCopy = Boolean(enabled || state.capabilitiesView);
  const copyBtn = $("copyBtn");
  if (copyBtn) copyBtn.disabled = !allowCopy;
  ["revertBtn", "saveBtn", "applyBtn"].forEach((id) => {
    const el = $(id);
    if (el) el.disabled = !allowActions;
  });
  const toggleBtn = $("toggleEnableBtn");
  if (toggleBtn) {
    toggleBtn.disabled = !allowActions || !isAutomation() || state.compareTarget !== "current";
  }
  updateArchitectActionState();
  syncCombineButton();
}

function updateArchitectActionState() {
  const prompt = ($("aiPrompt")?.value || "").trim();
  const hasPrompt = Boolean(prompt);
  const hasConv = Boolean(state.architectConversationId);
  const isArchitect = state.aiMode === "architect";
  const combineMode = isCombineModeActive();

  const planBtns = [$("aiPlanBtn"), $("aiPlanBtnExpanded")].filter(Boolean);
  const finalizeBtns = [$("aiFinalizeBtn"), $("aiFinalizeBtnExpanded")].filter(Boolean);
  const finalizeLabel = combineMode ? "Finalize and combine" : "Finalize and build";
  finalizeBtns.forEach((btn) => {
    btn.textContent = finalizeLabel;
  });

  if (state.capabilitiesView) {
    planBtns.forEach((btn) => (btn.disabled = true));
    finalizeBtns.forEach((btn) => (btn.disabled = true));
    return;
  }

  if (!isArchitect) {
    planBtns.forEach((btn) => (btn.disabled = !state.activeId || !hasPrompt));
    finalizeBtns.forEach((btn) => {
      btn.hidden = true;
      btn.disabled = true;
    });
    return;
  }

  planBtns.forEach((btn) => (btn.disabled = combineMode || !hasPrompt));
  finalizeBtns.forEach((btn) => {
    btn.hidden = false;
    btn.disabled = combineMode ? false : !(hasConv || hasPrompt);
  });
}

function clearAiPrompts() {
  const main = $("aiPrompt");
  const expanded = $("aiPromptExpanded");
  if (main) main.value = "";
  if (expanded) expanded.value = "";
  updateArchitectActionState();
}

function syncPromptInputs(value, source) {
  if (state.promptSyncing) return;
  state.promptSyncing = true;
  const main = $("aiPrompt");
  const expanded = $("aiPromptExpanded");
  if (source !== "main" && main) main.value = value;
  if (source !== "expanded" && expanded) expanded.value = value;
  state.promptSyncing = false;
}

function shouldSendPromptOnEnter(e) {
  const invert = Boolean(settings.invertPromptEnter);
  const modifier = e.shiftKey || e.ctrlKey || e.metaKey;
  return invert ? modifier : !modifier;
}

function bindPromptEnter(el, handler) {
  if (!el) return;
  el.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    if (!shouldSendPromptOnEnter(e)) return;
    e.preventDefault();
    handler();
  });
}

function updateCreateFinalizeState() {
  const btn = $("createFinalizeBtn");
  if (!btn) return;
  const hasConv = Boolean(state.createArchitectConversationId);
  const hasPrompt = Boolean(($("promptText")?.value || "").trim());
  btn.disabled = !(hasConv || hasPrompt);
  const planBtn = $("runPromptBtn");
  if (planBtn) planBtn.disabled = !hasPrompt;
}

function parseAutomationYaml(yamlText) {
  if (!isAutomation()) return null;
  try {
    let obj = jsyaml.load(yamlText);
    if (Array.isArray(obj)) {
      obj = obj.find(x => x && typeof x === "object") || obj[0];
    }
    if (obj && typeof obj === "object" && Array.isArray(obj.automation)) {
      obj = obj.automation.find(x => x && typeof x === "object") || obj.automation[0];
    }
    if (!obj || typeof obj !== "object") return null;
    return obj;
  } catch (e) {
    return null;
  }
}

function normalizeAutomationState(value) {
  if (value === null || value === undefined) return "";
  return String(value).trim().toLowerCase();
}

function isAutomationDisabled(objOrState) {
  if (!objOrState) return false;
  if (typeof objOrState === "string") {
    const state = normalizeAutomationState(objOrState);
    if (state === "off" || state === "disabled") return true;
    if (state === "on" || state === "enabled") return false;
    return false;
  }
  if (typeof objOrState === "object") {
    const state = normalizeAutomationState(objOrState.state);
    if (state === "off" || state === "disabled") return true;
    if (state === "on" || state === "enabled") return false;
    if (objOrState.enabled === false) return true;
    if (objOrState.initial_state === false) return true;
  }
  return false;
}

function updateEnableButtonFromState(stateValue) {
  const btn = $("toggleEnableBtn");
  if (!btn) return;
  if (!isAutomation()) {
    btn.textContent = "Disable";
    return;
  }
  const stateValueNorm = normalizeAutomationState(stateValue);
  if (!stateValueNorm) {
    btn.textContent = "Disable";
    return;
  }
  const disabled = isAutomationDisabled(stateValueNorm);
  btn.textContent = disabled ? "Enable" : "Disable";
}

function scheduleEnableUpdate() {
  clearTimeout(_enableTimer);
  _enableTimer = setTimeout(() => updateEnableButtonFromState(state.active?.state), 150);
}

async function refreshAutomationState(id = state.activeId) {
  if (!id || !isAutomation()) return null;
  try {
    const out = await api(`${entityEndpoint()}/${encodeURIComponent(id)}/state`);
    const nextState = out?.state ?? null;
    const entityId = out?.entity_id ?? out?.entityId ?? null;
    if (state.activeId === id) {
      if (!state.active) state.active = { id };
      state.active.state = nextState;
      if (entityId) state.active.entity_id = entityId;
      updateEnableButtonFromState(nextState);
    }
    const item = state.list.find((it) => it.id === id);
    if (item) {
      item.state = nextState;
      if (entityId) item.entity_id = entityId;
      renderList();
    }
    return out;
  } catch (e) {
    log(`State refresh failed: ${e.message || e}`);
    return null;
  }
}

async function toggleAutomationEnabled() {
  if (!state.activeId) return;
  if (state.capabilitiesView) {
    toast("Knowledgebase is read-only.");
    return;
  }
  if (!isAutomation()) {
    toast("Enable/disable is only available for automations.");
    return;
  }
  if (state.compareTarget !== "current") {
    setCompareTarget("current");
  }

  let currentState = normalizeAutomationState(state.active?.state);
  if (currentState !== "on" && currentState !== "off") {
    const refreshed = await refreshAutomationState(state.activeId);
    currentState = normalizeAutomationState(refreshed?.state ?? state.active?.state);
  }
  if (currentState !== "on" && currentState !== "off") {
    toast("Unable to determine current automation state.");
    return;
  }

  const nextState = currentState === "on" ? "off" : "on";
  const payload = { state: nextState };
  if (state.active?.entity_id) payload.entity_id = state.active.entity_id;

  try {
    await api(`${entityEndpoint()}/${encodeURIComponent(state.activeId)}/state`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (state.active) state.active.state = nextState;
    updateEnableButtonFromState(nextState);
    await loadList();
    await loadHealth(state.activeId);
    toast(nextState === "on" ? "Enabled." : "Disabled.");
  } catch (e) {
    toast("Enable/disable failed - check log", 3500);
    log(`Enable/disable failed: ${e.message || e}`);
  }
}

function setDirty(isDirty) {
  state.dirty = isDirty;
  if (state.capabilitiesView) return;
  const title = state.active?.alias ? state.active.alias : `Select a ${entityLabel()}`;
  $("aTitle").textContent = isDirty ? `${title} - unsaved` : title;
  $("saveBtn").textContent = isDirty ? "Save*" : "Save";
  if (state.compareTarget === "current") updateDiff();
}

async function loadList() {
  const requestId = ++listRequestSeq;
  const q = encodeURIComponent(state.q || "");
  const url = `${entityEndpoint()}?q=${q}`;

  log("Loading list...");
  try {
    const data = await api(url);
    if (requestId !== listRequestSeq) return false;
    state.list = normalizeListPayload(data);
    if (state.activeId && state.active) {
      const activeItem = state.list.find((it) => it.id === state.activeId);
      if (activeItem && Object.prototype.hasOwnProperty.call(activeItem, "state")) {
        state.active.state = activeItem.state;
        if (activeItem.entity_id) state.active.entity_id = activeItem.entity_id;
        updateEnableButtonFromState(state.active.state);
      }
    }
    hideSetupBanner();
    setConn(true, "Online");
    renderList();
    log(`Loaded ${state.list.length} ${entityLabelPlural()}.`);
    return true;
  } catch (e) {
    if (isRequestCancelledError(e)) {
      log("List load cancelled.");
      return false;
    }
    if (requestId !== listRequestSeq) return false;
    const msg = String(e.message || e);
    if (msg.startsWith("412") || msg.includes("AUTOMATIONS_FILE_PATH") || msg.includes("SCRIPTS_FILE_PATH")) {
      showSetupBanner();
      setConn(false, "Setup needed");
      state.list = [];
      renderList();
      log("File path not configured. Open settings to set UNC path.");
      toast("Setup required - open Settings to set the UNC path.", 3500);
      return false;
    }
    throw e;
  }
}

async function openAutomation(id) {
  if (!id) return;
  if (id === state.activeId) return;
  if (state.capabilitiesView) {
    closeCapabilitiesView();
  }

  if (state.compareTarget !== "current") {
    setCompareTarget("current");
  }
  cacheActiveTab();

  if (restoreTab(id)) return;

  state.activeId = id;
  state.selectedVersionId = null;
  state.selectedVersionYaml = "";
  renderList();

  log(`Loading ${entityLabel()} ${id}...`);
  let data;
  try {
    data = await api(`${entityEndpoint()}/${encodeURIComponent(id)}`);
  } catch (e) {
    if (isRequestCancelledError(e)) {
      log(`Load cancelled for ${id}.`);
      return;
    }
    toast("Load failed - check log", 3500);
    log(`Load failed: ${e.message || e}`);
    return;
  }

  const yaml = data.yaml ?? data.raw ?? data.content ?? "";
  state.active = {
    id,
    alias: data.alias ?? data.meta?.alias ?? data.name ?? id,
    description: data.description ?? data.meta?.description ?? "",
    source: data.source ?? data.meta?.source ?? "",
    ha_id: data.ha_id ?? data.meta?.ha_id ?? data.automation_id ?? data.script_id ?? data.entity_id ?? null,
    state: data.state ?? data.meta?.state ?? null,
    entity_id: data.entity_id ?? data.meta?.entity_id ?? null,
  };
  state.originalYaml = yaml;
  state.currentDraft = yaml;
  state.latestVersionYaml = yaml;
  state.previousSavedYaml = "";
  state.health = null;
  state.healthLoading = false;
  state.scenarioResult = null;
  state.scenarioRunning = false;

  $("aTitle").textContent = state.active.alias || id;
  $("aMeta").textContent = `${state.active.source || "Unknown"} - ${id}`;

  setEditorReadOnly(false);
  setEditorValue(state.currentDraft);
  updateCompareTabs();
  aiOutputClear();
  state.lastAiPrompt = "";
  state.architectConversationId = data.conversation_id || null;
  state.architectContextSent = Boolean(state.architectConversationId);
  setAiHistory(Array.isArray(data.conversation_history) ? data.conversation_history : []);
  clearDiffMarks();
  setButtons(true);
  updateEnableButtonFromState(state.active?.state);
  if (state.active?.state === null || state.active?.state === undefined || state.active?.state === "") {
    refreshAutomationState(id);
  }
  setDirty(false);
  syncCapabilitiesUi();
  toast("Loaded.");
  if (state.viewMode === "visual") renderVisual();
  await loadVersions(id);
  await loadHealth(id);
  renderScenarioOutput();
  addTab(id);
}

async function validateYaml() {
  try {
    const txt = getCurrentDraftText();
    jsyaml.load(txt);
    toast("YAML looks valid");
    log("Validation OK.");
  } catch (e) {
    toast("YAML error - check log", 3000);
    log(`YAML validation failed: ${e.message || e}`);
  }
}

async function saveAutomation() {
  if (!state.activeId) return;
  if (state.capabilitiesView) {
    toast("Knowledgebase is read-only.");
    return;
  }
  return applyAutomation({ source: "save" });
}

async function applyAutomation(options = {}) {
  if (!state.activeId) return;
  if (state.capabilitiesView) {
    toast("Knowledgebase is read-only.");
    return;
  }
  const source = options.source === "save" ? "save" : "apply";

  const yaml = getCurrentDraftText();
  const summary = buildChangeSummary(state.originalYaml || "", yaml);
  const confirmText = source === "save"
    ? `Save a local version and apply this ${entityLabel()} to Home Assistant now?\nA backup of the current HA version will be created first.\n\n${summary}`
    : `Apply this ${entityLabel()} to Home Assistant now?\nA backup of the current HA version will be created first.\n\n${summary}`;
  const ok = confirm(confirmText);
  if (!ok) return;

  try {
    const out = await api(`${entityEndpoint()}/${encodeURIComponent(state.activeId)}/apply`, {
      method: "POST",
      body: JSON.stringify({ yaml, note: state.lastAiPrompt || summary }),
    });

    toast(source === "save" ? "Saved and applied." : "Applied to Home Assistant.");
    state.lastAiPrompt = "";
    state.previousSavedYaml = state.originalYaml;
    state.originalYaml = yaml;
    state.currentDraft = yaml;
    state.latestVersionYaml = yaml;
    setDirty(false);
    const haId = out?.automation_id || out?.script_id || out?.entity_id;
    log(`Applied ${state.activeId}. ${haId ? `HA id: ${haId}` : ""}`);
    await loadList();
    await loadVersions(state.activeId);
    await loadHealth(state.activeId);
  } catch (e) {
    toast("Apply failed - check log", 3500);
    log(`Apply failed: ${e.message || e}`);
  }
}

async function combineSelectedAutomations(options = {}) {
  const opts = options && typeof options === "object" ? options : {};
  if (!isAutomation()) {
    toast("Combine is only available for automations.");
    return;
  }
  const ids = getCombineSelectionIds();
  if (ids.length < 2) {
    toast("Select at least two automations to combine.");
    return;
  }

  const ok = await openConfirmModal({
    title: "Combine automations",
    subtitle: "Build one automation from selected items.",
    message: `Combine ${ids.length} automations and disable the originals when complete?`,
    confirmText: "Combine now",
    confirmClass: "danger",
  });
  if (!ok) return;

  const adjustments = typeof opts.adjustments === "string"
    ? opts.adjustments.trim()
    : (($("aiPrompt")?.value || "").trim());
  const alias = typeof opts.alias === "string" ? opts.alias.trim() : "";

  let stopCycle = null;
  try {
    stopCycle = startBuilderHandoff("aiStatus");
    if (adjustments) aiOutputAppend(adjustments, "user");
    aiOutputAppend(`Combine selected automations: ${ids.join(", ")}`, "system");
    aiOutputAppend("Combining and building one automation...", "system");
    toast("Combining automations...", 2500);

    const out = await api("/api/automations/combine", {
      method: "POST",
      body: JSON.stringify({
        automation_ids: ids,
        prompt: adjustments || "",
        alias: (alias || "").trim() || null,
        disable_redundant: true,
      }),
    });
    const createdId = out?.automation_id || out?.entity_id || null;
    const disabled = Array.isArray(out?.disabled_automations) ? out.disabled_automations : [];
    const disableFailed = Array.isArray(out?.disable_failed) ? out.disable_failed : [];
    aiOutputAppend(
      `Combined ${ids.length} automations${createdId ? ` into ${createdId}` : ""}.`,
      "assistant"
    );
    if (disabled.length) {
      log(`Disabled redundant automations: ${disabled.join(", ")}`);
    }
    if (disableFailed.length) {
      const detail = disableFailed
        .map((item) => `${item?.id || "unknown"} (${item?.detail || "failed"})`)
        .join(", ");
      log(`Some automations could not be disabled: ${detail}`);
    }

    clearCombineSelection();
    if (opts.clearPrompt) {
      clearAiPrompts();
    }
    await loadList();
    if (createdId) {
      await openAutomation(createdId);
    }
    if (out?.yaml) await maybePromptMissingCapabilities(out.yaml);
    toast("Combine complete.");
  } catch (e) {
    if (isRequestCancelledError(e)) {
      log("Combine request cancelled.");
      return;
    }
    toast("Combine failed - check log", 3500);
    log(`Combine failed: ${e.message || e}`);
    aiOutputAppend(`Error: ${e.message || e}`, "error");
  } finally {
    if (typeof stopCycle === "function") stopCycle();
    clearStatus("aiStatus");
  }
}

/* ------------------------------
   Modals + tabs
-------------------------------- */
function openModal(id) {
  const m = $(id);
  if (!m) return;
  m.setAttribute("aria-hidden", "false");
  // focus first textarea/input inside
  const focusTarget = m.querySelector("textarea, input, button");
  if (focusTarget) setTimeout(() => focusTarget.focus(), 0);
}
function closeModal(id) {
  const m = $(id);
  if (!m) return;
  m.setAttribute("aria-hidden", "true");
  if (id === "confirmModal") resolveConfirm(false);
}
function wireModals() {
  document.addEventListener("click", (e) => {
    const closeId = e.target?.dataset?.close;
    if (closeId) closeModal(closeId);
  });
}

/* ------------------------------
   Create from prompt
-------------------------------- */
async function runCreateFromPrompt() {
  const text = $("promptText").value.trim();
  if (!text) return toast("Write a prompt first.");
  if (shouldRememberPrompt(text)) {
    openKbSyncModal({ prefill: text });
  }

  try {
    setStatus("createStatus", "architect", agentStatusText("architect", "thinking..."));
    toast("Asking Architect...", 2500);
    createOutputAppend(text, "user");
    createOutputAppend("Sending to Architect...", "system");
    $("promptText").value = "";

    const out = await api(`/api/architect/chat`, {
      method: "POST",
      body: JSON.stringify({
        text,
        mode: "create",
        conversation_id: state.createArchitectConversationId,
        entity_type: state.entityType,
        save_entity_hint: true,
      }),
    });

    handleCapabilitiesUpdated(out);
    appendAgentStatus(out, createOutputAppend, "createStatus");
    if (out.conversation_id) state.createArchitectConversationId = out.conversation_id;
    if (out.reply) createOutputAppend(out.reply, "assistant");

    const finalizeBtn = $("createFinalizeBtn");
    if (finalizeBtn) finalizeBtn.disabled = false;
  } catch (e) {
    if (isRequestCancelledError(e)) {
      log("Architect create chat cancelled.");
      return;
    }
    toast("Architect chat failed - check log", 3500);
    log(`Architect chat failed: ${e.message || e}`);
    createOutputAppend(`Error: ${e.message || e}`, "error");
  } finally {
    clearStatus("createStatus");
    updateCreateFinalizeState();
  }
}

async function revertToSavedAndApply() {
  if (!state.activeId) return;
  const saved = getLatestSavedYaml();
  if (!saved) return toast("No saved version available.");
  const summary = buildChangeSummary(getCurrentDraftText(), saved);
  const ok = confirm(`Revert to the last saved ${entityLabel()} and apply it to Home Assistant now?\n\n${summary}`);
  if (!ok) return;
  try {
    if (state.compareTarget !== "current") {
      setCompareTarget("current");
    }
    const out = await api(`${entityEndpoint()}/${encodeURIComponent(state.activeId)}/apply`, {
      method: "POST",
      body: JSON.stringify({ yaml: saved, note: `revert:${summary}` }),
    });
    toast("Reverted and applied.");
    state.lastAiPrompt = "";
    state.previousSavedYaml = state.originalYaml;
    state.originalYaml = saved;
    state.currentDraft = saved;
    state.latestVersionYaml = saved;
    setEditorReadOnly(false);
    setEditorValue(saved);
    setDirty(false);
    updateEnableButtonFromState(state.active?.state);
    const haId = out?.automation_id || out?.script_id || out?.entity_id;
    log(`Reverted ${state.activeId}. ${haId ? `HA id: ${haId}` : ""}`);
    await loadList();
    await loadVersions(state.activeId);
  } catch (e) {
    toast("Revert failed - check log", 3500);
    log(`Revert failed: ${e.message || e}`);
  }
}

async function createArchitectFinalize() {
  if (!state.createArchitectConversationId && !$("promptText").value.trim()) {
    return toast("Write a prompt first.");
  }
  let handoffTimer = null;
  const ok = await openConfirmModal({
    title: "Finalize and build",
    subtitle: "Send the Architect plan to the Builder.",
    message: `Finalize and build a new ${entityLabel()}?`,
    confirmText: "Build now",
  });
  if (!ok) return;

  try {
    const text = $("promptText").value.trim();
    if (text) {
      createOutputAppend(text, "user");
      $("promptText").value = "";
    }

    handoffTimer = startBuilderHandoff("createStatus");
    toast("Handing off to Builder...", 2500);

    const out = await api(`/api/architect/finalize`, {
      method: "POST",
      body: JSON.stringify({
        conversation_id: state.createArchitectConversationId,
        mode: "create",
        entity_type: state.entityType,
        text,
      }),
    });

    if (out.final_prompt) {
      showBuilderPrompt(createOutputAppend, out.final_prompt);
    }
    handleCapabilitiesUpdated(out);
    appendAgentStatus(out, createOutputAppend, "createStatus", "Agent");

    toast("Building complete and live.");
    if (out.conversation_id) state.createArchitectConversationId = out.conversation_id;
    const createdId = out.entity_id || out.automation_id || out.script_id || null;
    if (createdId) {
      await attachConversationHistory(
        createdId,
        out.entity_type || state.entityType,
        state.createArchitectConversationId,
        state.createHistory
      );
    }
    await saveCapabilitiesFromHistory(state.createHistory, createdId, out.entity_type || state.entityType);
    log(`Created ${entityLabel()}: ${out.alias || createdId || "unknown"}`);

    createOutputAppend("Builder finished. You can close this window.", "system");
    state.createArchitectConversationId = null;
    state.createHistory = [];
    $("promptText").value = "";

    await loadList();
    if (settings.autoOpenNew && createdId) {
      try {
        await openAutomation(createdId);
      } catch (e) {
        log(`Auto-open failed: ${e.message || e}`);
      }
    }
    if (out?.yaml) {
      await maybePromptMissingCapabilities(out.yaml);
    }
  } catch (e) {
    if (isRequestCancelledError(e)) {
      log("Builder handoff cancelled.");
      return;
    }
    toast("Builder handoff failed - check log", 3500);
    log(`Architect finalize failed: ${e.message || e}`);
    createOutputAppend(`Error: ${e.message || e}`, "error");
  } finally {
    if (typeof handoffTimer === "function") handoffTimer();
    clearStatus("createStatus");
    updateCreateFinalizeState();
  }
}

/* ------------------------------
   AI Improve / Rewrite
-------------------------------- */
function buildAiPrompt(userPrompt) {
  const mode = state.aiMode;
  const templateEl = $("aiTemplate");
  const template = templateEl ? templateEl.value : "";
  const target = entityLabel();

  const templateHints = {
    safety: "Make it safer and more reliable: add guards, choose blocks, timeouts, and avoid noisy retriggers.",
    readability: "Improve readability: clearer structure, consistent indentation, and simplify conditions/actions where possible.",
    noise: "Reduce spam: add cooldowns, conditions, and avoid repeated notifications. Prefer a single notification per window.",
    debug: "Add debug visibility: use logbook.log and/or persistent_notification.create for key branches and decisions.",
  };

  const head = [];
  if (mode === "improve") {
    head.push(`Improve this existing ${target} while preserving intent and structure.`);
    head.push("Prefer minimal changes; only rewrite what is necessary.");
  } else {
    head.push(`Rewrite this ${target} more substantially to meet the request (structure can change).`);
    head.push("Still keep it correct and robust; do not invent entity_ids unless already present.");
  }

  if (template && templateHints[template]) {
    head.push(templateHints[template]);
  }

  head.push("User request follows:");
  return head.join("\n") + "\n" + userPrompt;
}

function isSmallEditPrompt(text) {
  const t = (text || "").trim();
  if (!t) return false;
  const patterns = [
    /^\s*(rename|name|alias)\s+/i,
    /^\s*set\s+alias\s+/i,
    /^\s*change\s+alias\s+/i,
    /^\s*set\s+description\s+/i,
    /^\s*change\s+description\s+/i,
    /\bmode\s*(to|=)?\s*(single|restart|queued|parallel)\b/i,
    /\bchange\s+service\s+[a-z_]+\.[a-z0-9_]+\s*(to|->)\s*[a-z_]+\.[a-z0-9_]+\b/i,
    /\b(only if|if)\s+[a-z_]+\.[a-z0-9_]+\s+(is|=)\s+[a-z0-9_]+\b/i,
    /\b(initial_state|start)\s*(is\s*)?(enabled|disabled|on|off|true|false)\b/i,
  ];
  return patterns.some((re) => re.test(t));
}

function shouldRememberPrompt(text) {
  const t = (text || "").trim();
  if (!t) return false;
  return /\b(remember|save to kb|save to knowledgebase|add to kb|add to knowledgebase|knowledgebase|capabilities)\b/i.test(t);
}

async function tryLocalEdit(prompt) {
  if (!state.activeId) return false;
  if (!isSmallEditPrompt(prompt)) return false;
  try {
    const out = await api(`${entityEndpoint()}/${encodeURIComponent(state.activeId)}/ai_update`, {
      method: "POST",
      body: JSON.stringify({
        prompt,
        yaml: getCurrentDraftText(),
        local_only: true,
      }),
    });
    if (!out?.ok || !out?.yaml) {
      return false;
    }
    state.currentDraft = out.yaml;
    setEditorValue(out.yaml);
    state.lastAiPrompt = prompt;
    setDirty(state.currentDraft !== (getLatestSavedYaml() || ""));
    updateDiff();
    if (state.viewMode === "visual") scheduleVisualRender();
    aiOutputAppend(prompt, "user");
    aiOutputAppend(out.message || "Applied local edit.", "assistant");
    toast("Applied local edit.");
    return true;
  } catch (e) {
    return false;
  }
}

async function aiImprove() {
  if (state.aiMode === "architect") {
    return aiArchitectChat();
  }
  if (!state.activeId) return;
  const prompt = $("aiPrompt").value.trim();
  if (!prompt) return toast("Write an AI prompt first.");

  const ok = confirm(`Generate an AI-updated ${entityLabel()} draft?\nThis will replace the editor content (you can Revert).`);
  if (!ok) return;

  try {
    if (state.compareTarget !== "current") {
      setCompareTarget("current");
    }
    aiOutputClear();
    aiOutputAppend(prompt, "user");
    aiOutputAppend("Sending request...", "system");
    toast("Asking AI...", 2500);
    log(`AI update request (${state.aiMode})...`);

    const out = await api(`${entityEndpoint()}/${encodeURIComponent(state.activeId)}/ai_update`, {
      method: "POST",
      body: JSON.stringify({
        prompt: buildAiPrompt(prompt),
        yaml: getCurrentDraftText(),
      }),
    });

    if (!out.ok || !out.yaml) throw new Error("AI did not return YAML");
    state.currentDraft = out.yaml;
    setEditorValue(out.yaml);
    state.lastAiPrompt = prompt;
    toast("Draft updated.");
    log("AI draft applied to editor (not saved).");
    if (out.question) {
      aiOutputAppend(`Question: ${out.question}`, "assistant");
    }
    if (Array.isArray(out.questions)) {
      out.questions.forEach((q) => aiOutputAppend(`Question: ${q}`, "assistant"));
    }
    if (out.message) {
      aiOutputAppend(out.message, "assistant");
    }
    aiOutputAppend("Draft applied to editor. Review and Save or Apply.", "assistant");
    // keep dirty marker accurate
    setDirty(state.currentDraft !== (getLatestSavedYaml() || ""));
    updateDiff();
    if (state.viewMode === "visual") scheduleVisualRender();
    await maybePromptMissingCapabilities(out.yaml);
  } catch (e) {
    if (isRequestCancelledError(e)) {
      log("AI update cancelled.");
      return;
    }
    toast("AI update failed - check log", 3500);
    log(`AI update failed: ${e.message || e}`);
    aiOutputAppend(`Error: ${e.message || e}`, "error");
  }
}

async function createNewFromAiPrompt() {
  const prompt = $("aiPrompt").value.trim();
  if (!prompt) return toast("Write a prompt first.");
  let handoffTimer = null;

  try {
    setStatus("aiStatus", "architect", agentStatusText("architect", "thinking..."));
    toast("Asking Architect...", 2500);
    aiOutputAppend(prompt, "user");
    aiOutputAppend("Sending to Architect...", "system");
    pushAiHistory("user", prompt);
    clearAiPrompts();

    const chatOut = await api(`/api/architect/chat`, {
      method: "POST",
      body: JSON.stringify({
        text: prompt,
        mode: "create",
        entity_type: state.entityType,
        save_entity_hint: true,
      }),
    });

    handleCapabilitiesUpdated(chatOut);
    appendAgentStatus(chatOut, aiOutputAppend, "aiStatus");
    if (chatOut.conversation_id) state.architectConversationId = chatOut.conversation_id;
    if (chatOut.reply) {
      aiOutputAppend(chatOut.reply, "assistant");
      pushAiHistory("assistant", chatOut.reply);
    }

    const ok = await openConfirmModal({
      title: "Finalize and build",
      subtitle: "Send the Architect plan to the Builder.",
      message: `Finalize and build a new ${entityLabel()}?`,
      confirmText: "Build now",
    });
    if (!ok) return;

    handoffTimer = startBuilderHandoff("aiStatus");
    aiOutputAppend("Handing off to Builder...", "system");
    const out = await api(`/api/architect/finalize`, {
      method: "POST",
      body: JSON.stringify({
        conversation_id: chatOut.conversation_id,
        mode: "create",
        entity_type: state.entityType,
      }),
    });

    if (out.final_prompt) showBuilderPrompt(aiOutputAppend, out.final_prompt);
    handleCapabilitiesUpdated(out);
    appendAgentStatus(out, aiOutputAppend, "aiStatus", "Agent");

    toast("Building complete and live.");
    if (out.conversation_id) state.architectConversationId = out.conversation_id;
    const newId = out.entity_id || out.automation_id || out.script_id;
    if (newId) {
      await attachConversationHistory(
        newId,
        out.entity_type || state.entityType,
        state.architectConversationId,
        state.aiHistory
      );
    }
    await saveCapabilitiesFromHistory(state.aiHistory, newId, out.entity_type || state.entityType);
    await loadList();
    if (newId) {
      await openAutomation(newId);
    }
  } catch (e) {
    if (isRequestCancelledError(e)) {
      log("Create from AI cancelled.");
      return;
    }
    toast("Create failed - check log", 3500);
    log(`Create from AI failed: ${e.message || e}`);
    aiOutputAppend(`Error: ${e.message || e}`, "error");
  } finally {
    if (typeof handoffTimer === "function") handoffTimer();
    clearStatus("aiStatus");
  }
}

async function aiArchitectChat() {
  const prompt = $("aiPrompt").value.trim();
  if (!prompt) return toast("Write a prompt first.");
  if (shouldRememberPrompt(prompt)) {
    openKbSyncModal({ prefill: prompt });
  }

  if (state.activeId) {
    const localApplied = await tryLocalEdit(prompt);
    if (localApplied) {
      syncPromptInputs("", "main");
      updateArchitectActionState();
      return;
    }
  }

  try {
    setStatus("aiStatus", "architect", agentStatusText("architect", "thinking..."));
    aiOutputAppend(prompt, "user");
    aiOutputAppend("Sending to Architect...", "system");
    pushAiHistory("user", prompt);
    clearAiPrompts();
    toast("Asking Architect...", 2500);

    const body = {
      text: prompt,
      conversation_id: state.architectConversationId,
      mode: state.activeId ? "edit" : "create",
      entity_type: state.entityType,
      entity_id: state.activeId || null,
      save_entity_hint: true,
    };
    if (state.activeId && !state.architectContextSent) {
      body.automation_id = state.activeId;
      body.current_yaml = getCurrentDraftText();
      body.include_context = true;
    }

    const out = await api("/api/architect/chat", {
      method: "POST",
      body: JSON.stringify(body),
    });

    handleCapabilitiesUpdated(out);
    appendAgentStatus(out, aiOutputAppend, "aiStatus");
    if (out.conversation_id) state.architectConversationId = out.conversation_id;
    state.architectContextSent = true;
    if (out.reply) {
      aiOutputAppend(out.reply, "assistant");
      pushAiHistory("assistant", out.reply);
    }
    syncPromptInputs("", "main");

    const finalizeBtn = $("aiFinalizeBtn");
    if (finalizeBtn) finalizeBtn.disabled = false;
  } catch (e) {
    if (isRequestCancelledError(e)) {
      log("Architect chat cancelled.");
      return;
    }
    toast("Architect chat failed - check log", 3500);
    log(`Architect chat failed: ${e.message || e}`);
    aiOutputAppend(`Error: ${e.message || e}`, "error");
  } finally {
    clearStatus("aiStatus");
    updateArchitectActionState();
  }
}

async function aiArchitectFinalize(options = {}) {
  const { forcePrompt = false } = options;
  const mode = state.activeId ? "edit" : "create";
  const prompt = $("aiPrompt")?.value?.trim() || "";
  const historyStart = state.aiHistory.length;
  let handoffTimer = null;

  if (isCombineModeActive()) {
    await combineSelectedAutomations({
      adjustments: prompt,
      clearPrompt: true,
    });
    return;
  }

  if (forcePrompt && !prompt) {
    return toast("Write a prompt first.");
  }
  if (!state.architectConversationId && !prompt) {
    return toast("Write a prompt first.");
  }

  const ok = await openConfirmModal({
    title: "Finalize and build",
    subtitle: "Send the Architect plan to the Builder.",
    message: mode === "edit"
      ? `Finalize and build updates to this ${entityLabel()}?`
      : `Finalize and build a new ${entityLabel()}?`,
    confirmText: "Build now",
  });
  if (!ok) return;

  try {
    if (forcePrompt) {
      state.architectConversationId = null;
      state.architectContextSent = false;
    }

    if (prompt) {
      aiOutputAppend(prompt, "user");
      pushAiHistory("user", prompt);
      clearAiPrompts();
    }

    handoffTimer = startBuilderHandoff("aiStatus");
    toast("Handing off to Builder...", 2500);

    const body = {
      conversation_id: state.architectConversationId,
      mode,
      entity_type: state.entityType,
      entity_id: state.activeId || null,
      automation_id: state.activeId || null,
    };
    if (prompt) {
      body.text = prompt;
    }
    if (state.activeId) {
      body.current_yaml = getCurrentDraftText();
      body.include_context = true;
    }

    const out = await api("/api/architect/finalize", {
      method: "POST",
      body: JSON.stringify(body),
    });

    if (out.conversation_id) state.architectConversationId = out.conversation_id;
    if (out.final_prompt) {
      showBuilderPrompt(aiOutputAppend, out.final_prompt);
      state.lastAiPrompt = out.final_prompt;
    }
    handleCapabilitiesUpdated(out);
    appendAgentStatus(out, aiOutputAppend, "aiStatus", "Agent");

    if (mode === "edit") {
      if (out.yaml) {
        if (state.compareTarget !== "current") setCompareTarget("current");
        state.previousSavedYaml = state.originalYaml;
        state.originalYaml = out.yaml;
        state.currentDraft = out.yaml;
        state.latestVersionYaml = out.yaml;
        setEditorValue(out.yaml);
        setDirty(false);
        await maybePromptMissingCapabilities(out.yaml);
      }
      toast("Building complete and live.");
      await loadList();
      await loadVersions(state.activeId);
      if (state.activeId) {
        const newMessages = state.aiHistory.slice(historyStart);
        await appendConversationHistory(
          state.activeId,
          state.entityType,
          state.architectConversationId,
          newMessages
        );
      }
    } else {
      toast("Building complete and live.");
      if (out.conversation_id) state.architectConversationId = out.conversation_id;
      const newId = out.entity_id || out.automation_id || out.script_id;
      if (newId) {
        await attachConversationHistory(
          newId,
          out.entity_type || state.entityType,
          state.architectConversationId,
          state.aiHistory
        );
      }
      await saveCapabilitiesFromHistory(state.aiHistory, newId, out.entity_type || state.entityType);
      await loadList();
      if (settings.autoOpenNew && newId) {
        await openAutomation(newId);
      }
      if (out.yaml) {
        await maybePromptMissingCapabilities(out.yaml);
      }
    }
  } catch (e) {
    if (isRequestCancelledError(e)) {
      log("Architect finalize cancelled.");
      return;
    }
    toast("Builder handoff failed - check log", 3500);
    log(`Architect finalize failed: ${e.message || e}`);
    aiOutputAppend(`Error: ${e.message || e}`, "error");
  } finally {
    if (typeof handoffTimer === "function") handoffTimer();
    clearStatus("aiStatus");
    updateArchitectActionState();
  }
}

async function aiFinalizeWithPrompt() {
  await aiArchitectFinalize({ forcePrompt: true });
}

/* ------------------------------
   UI wiring
-------------------------------- */
function wireUI() {
  $("refreshBtn").onclick = async () => {
    try {
      const ok = await loadList();
      setConn(ok, ok ? "Online" : "Setup needed");
    } catch (err) {
      setConn(false, "API error");
      log(err.message || err);
    }
  };
  ["stopRequestsBtn", "aiStopBtn", "aiStopBtnExpanded"].forEach((id) => {
    const btn = $(id);
    if (btn) btn.onclick = () => abortAllActiveRequests();
  });
  updateStopRequestsButton();
  const viewMenuBtn = $("viewMenuBtn");
  const viewMenu = $("viewMenu");
  if (viewMenuBtn && viewMenu) {
    viewMenuBtn.onclick = (e) => {
      e.stopPropagation();
      syncViewMenuToggleState();
      setViewMenuOpen(viewMenu.hidden);
    };
    viewMenu.addEventListener("click", (e) => e.stopPropagation());
  }
  const bindViewToggle = (id, handler) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("change", () => handler(Boolean(el.checked)));
  };
  bindViewToggle("viewToggleSidebar", (show) => setSidebarCollapsed(!show));
  bindViewToggle("viewToggleAi", (show) => setAiCollapsed(!show));
  bindViewToggle("viewToggleRightRail", (show) => setRailCollapsed(!show));
  bindViewToggle("viewToggleActivity", (show) => setCardVisible("activity", show, true));
  bindViewToggle("viewToggleUsage", (show) => setCardVisible("usage", show, true));
  bindViewToggle("viewToggleVersions", (show) => setCardVisible("versions", show, true));
  document.addEventListener("click", (e) => {
    const wrap = e.target?.closest?.(".view-menu-wrap");
    if (!wrap) setViewMenuOpen(false);
  });
  syncViewMenuToggleState();

  document.querySelectorAll("[data-toggle-sidebar]").forEach((btn) => {
    btn.onclick = () => setSidebarCollapsed(!state.sidebarCollapsed);
  });
  document.querySelectorAll("[data-toggle-rail]").forEach((btn) => {
    btn.onclick = () => setRailCollapsed(!state.railCollapsed);
  });

  const autoTab = $("tabAutomations");
  if (autoTab) autoTab.onclick = () => setEntityType("automation");
  const scriptTab = $("tabScripts");
  if (scriptTab) scriptTab.onclick = () => setEntityType("script");
  const combineBtn = $("combineBtn");
  if (combineBtn) {
    combineBtn.onclick = () => combineSelectedAutomations({
      adjustments: ($("aiPrompt")?.value || "").trim(),
      clearPrompt: true,
    });
  }
  syncCombineButton();

  $("viewYamlBtn").onclick = () => setViewMode("yaml");
  $("viewVisualBtn").onclick = () => setViewMode("visual");

  $("compareCurrentBtn").onclick = () => setCompareTarget("current");
  const compareVersionBtn = $("compareVersionBtn");
  if (compareVersionBtn) compareVersionBtn.onclick = () => setCompareTarget("version");

  $("loadVersionBtn").onclick = () => loadSelectedVersionToEditor();
  $("applyVersionBtn").onclick = () => applySelectedVersionToHa();
  const restoreVersionBtn = $("restoreVersionBtn");
  if (restoreVersionBtn) restoreVersionBtn.onclick = () => applySelectedVersionToHa();


  const saveBtn = $("saveBtn");
  if (saveBtn) saveBtn.onclick = () => saveAutomation();
  const applyBtn = $("applyBtn");
  if (applyBtn) applyBtn.onclick = () => applyAutomation();

  const capabilitiesBtn = $("capabilitiesBtn");
  if (capabilitiesBtn) capabilitiesBtn.onclick = () => toggleCapabilitiesView();
  const capabilitiesRefreshBtn = $("capabilitiesRefreshBtn");
  if (capabilitiesRefreshBtn) {
    capabilitiesRefreshBtn.onclick = () => refreshCapabilities();
  }
  const capabilitiesSyncBtn = $("capabilitiesSyncBtn");
  if (capabilitiesSyncBtn) {
    capabilitiesSyncBtn.onclick = () => openKbSyncModal();
  }
  const capabilitiesLearnBtn = $("capabilitiesLearnBtn");
  if (capabilitiesLearnBtn) {
    capabilitiesLearnBtn.onclick = () => openKbSyncModal();
  }
  const kbSyncRunBtn = $("kbSyncRunBtn");
  if (kbSyncRunBtn) kbSyncRunBtn.onclick = () => runKbSync();
  const kbSyncSaveBtn = $("kbSyncSaveBtn");
  if (kbSyncSaveBtn) kbSyncSaveBtn.onclick = () => runKbSave();
  syncCapabilitiesUi();

  const aiPlanBtn = $("aiPlanBtn");
  if (aiPlanBtn) aiPlanBtn.onclick = () => aiImprove();
  const aiFinalizeBtn = $("aiFinalizeBtn");
  if (aiFinalizeBtn) aiFinalizeBtn.onclick = () => aiFinalizeWithPrompt();
  const aiResetHistoryBtn = $("aiResetHistoryBtn");
  if (aiResetHistoryBtn) aiResetHistoryBtn.onclick = () => resetAiHistory();
  const aiExpandBtn = $("aiExpandBtn");
  if (aiExpandBtn) {
    aiExpandBtn.onclick = () => {
      syncPromptInputs(($("aiPrompt")?.value || ""), "main");
      openModal("architectModal");
    };
  }
  const aiPlanBtnExpanded = $("aiPlanBtnExpanded");
  if (aiPlanBtnExpanded) aiPlanBtnExpanded.onclick = () => aiImprove();
  const aiFinalizeBtnExpanded = $("aiFinalizeBtnExpanded");
  if (aiFinalizeBtnExpanded) aiFinalizeBtnExpanded.onclick = () => aiFinalizeWithPrompt();

  bindPromptEnter($("aiPrompt"), () => aiImprove());
  bindPromptEnter($("aiPromptExpanded"), () => aiImprove());
  bindPromptEnter($("promptText"), () => runCreateFromPrompt());

  $("settingsBtn").onclick = () => openSettingsModal();
  const openSettings = $("openSettingsBtn");
  if (openSettings) openSettings.onclick = () => openSettingsModal();
  $("saveSettingsBtn").onclick = () => saveSettingsFromModal();
  const resetLayoutBtn = $("resetLayoutBtn");
  if (resetLayoutBtn) {
    resetLayoutBtn.onclick = async () => {
      const ok = await openConfirmModal({
        title: "Reset layout",
        subtitle: "Restore the default panel layout.",
        message: "Reset panel sizes and positions to the default view?",
        confirmText: "Reset view",
        confirmClass: "danger",
      });
      if (!ok) return;
      resetLayout();
    };
  }
  $("copyEnvBtn").onclick = () => copyEnvLine();

  const tabStrip = $("tabStrip");
  if (tabStrip) {
    tabStrip.addEventListener("click", (e) => {
      const item = e.target.closest(".tab-item");
      if (!item) return;
      const id = item.dataset.id;
      if (e.target.classList.contains("tab-close") || e.target.dataset.tabClose) {
        closeTab(id);
      } else {
        activateTab(id);
      }
    });
  }

  $("copyBtn").onclick = async () => {
    await navigator.clipboard.writeText(getCurrentDraftText());
    toast("Copied.");
  };
  const toggleEnableBtn = $("toggleEnableBtn");
  if (toggleEnableBtn) toggleEnableBtn.onclick = () => toggleAutomationEnabled();

  const revertBtn = $("revertBtn");
  if (revertBtn) {
    revertBtn.onclick = () => {
      revertToSavedAndApply();
    };
  }

  const newEntityBtn = $("newEntityBtn") || $("newFromPromptBtn");
  if (newEntityBtn) newEntityBtn.onclick = () => {
    state.createArchitectConversationId = null;
    createOutputClear();
    const finalizeBtn = $("createFinalizeBtn");
    if (finalizeBtn) finalizeBtn.disabled = true;
    updateEntityUi();
    openModal("createModal");
    updateCreateFinalizeState();
  };
  $("runPromptBtn").onclick = () => runCreateFromPrompt();
  const createFinalizeBtn = $("createFinalizeBtn");
  if (createFinalizeBtn) createFinalizeBtn.onclick = () => createArchitectFinalize();

  const templateEl = $("aiTemplate");
  if (templateEl) {
    templateEl.addEventListener("change", () => {
      const v = templateEl.value;
      if (!v) return;
      toast("Template selected. Add your request below.");
    });
  }
  const aiPromptEl = $("aiPrompt");
  if (aiPromptEl) {
    aiPromptEl.addEventListener("input", () => {
      syncPromptInputs(aiPromptEl.value, "main");
      updateArchitectActionState();
    });
  }
  const aiPromptExpandedEl = $("aiPromptExpanded");
  if (aiPromptExpandedEl) {
    aiPromptExpandedEl.addEventListener("input", () => {
      syncPromptInputs(aiPromptExpandedEl.value, "expanded");
      updateArchitectActionState();
    });
  }
  const promptTextEl = $("promptText");
  if (promptTextEl) {
    promptTextEl.addEventListener("input", () => updateCreateFinalizeState());
  }

  $("searchInput").addEventListener("input", (e) => {
    state.q = e.target.value.trim();
    if (!debouncedLoadList) debouncedLoadList = debounce(loadList, SEARCH_DEBOUNCE_MS);
    debouncedLoadList();
  });

  $("clearSearchBtn").onclick = () => {
    $("searchInput").value = "";
    state.q = "";
    loadList();
  };

  const hideDisabledToggleBtn = $("hideDisabledToggleBtn");
  if (hideDisabledToggleBtn) {
    syncHideDisabledToggle();
    hideDisabledToggleBtn.addEventListener("click", () => {
      settings.hideDisabled = !settings.hideDisabled;
      localStorage.setItem("ui_settings", JSON.stringify(settings));
      syncHideDisabledToggle();
      renderList();
    });
  }

  const aiSectionToggleBtn = $("aiSectionToggleBtn");
  if (aiSectionToggleBtn) {
    aiSectionToggleBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      setAiCollapsed(!state.aiCollapsed);
    });
  }

  const runScenarioBtn = $("runScenarioBtn");
  if (runScenarioBtn) runScenarioBtn.onclick = () => runScenarioTest();

  const listEl = $("automationList");
  if (listEl) {
    listEl.addEventListener("click", (e) => {
      const toggle = e.target?.closest?.(".item-toggle");
      if (!toggle) return;
      e.stopPropagation();
      const item = toggle.closest(".item");
      if (!item) return;
      item.classList.toggle("expanded");
      toggle.textContent = item.classList.contains("expanded") ? "Hide" : "Details";
    });
  }

  // ESC closes modals
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    setViewMenuOpen(false);
    for (const id of ["createModal", "settingsModal", "architectModal"]) {
      const m = $(id);
      if (m.getAttribute("aria-hidden") === "false") closeModal(id);
    }
  });

  document.addEventListener("click", (e) => {
    const btn = e.target?.closest?.("[data-toggle]");
    if (!btn) return;
    const id = btn.dataset.toggle;
    if (!id) return;
    const card = document.querySelector(`.card[data-card="${id}"]`);
    if (!card) return;
    const next = !card.classList.contains("collapsed");
    setCardCollapsed(id, next, true);
  });
}

function setAiMode(mode) {
  state.aiMode = "architect";
  const planBtns = [$("aiPlanBtn"), $("aiPlanBtnExpanded")].filter(Boolean);
  planBtns.forEach((btn) => {
    btn.textContent = "Plan changes";
  });
  setButtons(Boolean(state.activeId));
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

async function boot() {
  state.editor = CodeMirror.fromTextArea($("yamlTextarea"), {
    mode: "yaml",
    theme: "material-darker",
    lineNumbers: true,
    lineWrapping: true,
    indentUnit: 2,
    tabSize: 2,
  });

  state.editor.on("change", () => {
    if (state.capabilitiesView) return;
    if (!state.activeId) return;
    if (state.suppressChange) return;
    if (state.compareTarget !== "current") return;
    const now = state.editor.getValue();
    state.currentDraft = now;
    setDirty(now !== (getLatestSavedYaml() || ""));
    scheduleEnableUpdate();
    if (state.viewMode === "visual") scheduleVisualRender();
  });
  state.editor.on("refresh", () => {
    if (!state._sizingEditor) sizeEditorToWrap();
  });

  wireModals();
  wireUI();
  applyLayout();
  setupResizableSections();
  setupRowSplitter();
  setupColumnSplitter();
  window.addEventListener("error", (e) => {
    const msg = e.message || "";
    if (msg === "Script error.") return; // usually browser extension noise
    if (String(e.filename || "").startsWith("chrome-extension://")) return;
    log(`UI error: ${msg || e}`);
  });
  window.addEventListener("unhandledrejection", (e) => {
    const msg = e.reason?.message || e.reason || "Unknown rejection";
    if (String(msg).includes("chrome-extension://")) return;
    log(`UI error: ${msg}`);
  });

  loadSettings();
  loadAiCollapsed();
  setAiCollapsed(state.aiCollapsed, false);
  applyCollapsedCards();
  applyCardVisibility();
  captureAiOutputHome();
  updateSidebarTabs();
  updateEntityUi();
  renderHealthPanel();
  renderScenarioOutput();
  renderUsageChart();
  setSidebarCollapsed(false);
  setRailCollapsed(false);
  setViewMode(settings.viewMode || "yaml");
  setCompareTarget(settings.compareTarget || "current", { silent: true });
  setAiMode(state.aiMode);
  setTimeout(sizeEditorToWrap, 0);
  window.addEventListener("resize", () => {
    applyColumnSplit();
    sizeEditorToWrap();
  });

  try {
    const ok = await loadList();
    if (ok) {
      setConn(true, "Online");
      checkHelperAgents();
    } else {
      setConn(false, "Setup needed");
    }
  } catch (e) {
    setConn(false, "API error");
    log(`List load failed: ${e.message || e}`);
    $("automationList").innerHTML = `<div class="empty">API not responding. Check server routes.</div>`;
  }
}

window.loadSelectedVersionToEditor = loadSelectedVersionToEditor;
window.applySelectedVersionToHa = applySelectedVersionToHa;

boot();

