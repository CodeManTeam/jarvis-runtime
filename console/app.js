// JARVIS console — dark five-page cockpit (v1.0 M1)
// Only touches console/. See docs/design/NewJarvis/CONSOLE_UI.md for spec.
const state = {
  api: sessionStorage.getItem("jarvis.api") || location.origin,
  token: sessionStorage.getItem("jarvis.token") || "",
  data: {},
  refreshError: "",
  activityView: "attention", // attention | all
  memoryView: "timeline", // timeline | projects
  expanded: new Set(), // activity ids with evidence expanded
  composeOpen: false,
  pillSnoozedUntil: 0,
  jobGoalInput: "",
  jobStepsExpanded: new Set(), // job ids with full step list expanded
  jobDetails: {}, // job id -> {job, steps}
  jobPollTimer: null,
  stream: null, // EventSource
  streamFallbackTimer: null,
  streamNoticeTimer: null,
  streamConnected: false,
  streamReconnectAttempts: 0,
  lastSyncedAt: Date.now(),
  sections: loadSectionState(), // sectionKey -> expanded bool
  elapsedTimer: null, // single setInterval(1000) driving all running job elapsed-time nodes
  firstLoadDone: false, // R1: before this, activity page shows a skeleton, never "no data"
  busy: new Set(), // keys of in-flight actions (job ids / "createJob" / "saveModelSettings") — drives button busy state
  permissionPending: new Set(),
  permissionErrors: {},
  permissionPrevious: {},
  destroyStarted: false,
  destroyResult: "",
  destroyError: "",
  composeError: "",
};

// ---------- collapsible sections ----------
function loadSectionState() {
  try { return JSON.parse(sessionStorage.getItem("console.sections") || "{}"); } catch { return {}; }
}
function saveSectionState() {
  sessionStorage.setItem("console.sections", JSON.stringify(state.sections));
}
// First section registered per page defaults open; the rest default closed.
const sectionDefaultsSeen = new Set();
function sectionIsOpen(key) {
  if (Object.prototype.hasOwnProperty.call(state.sections, key)) return !!state.sections[key];
  const isFirst = !sectionDefaultsSeen.has(key) && sectionDefaultsSeen.size === 0;
  return isFirst;
}
function toggleSection(key) {
  const current = sectionIsOpen(key);
  state.sections[key] = !current;
  saveSectionState();
  // keepBody sections are rendered already; just show or hide them.
  const details = document.querySelector(`[data-section-key="${key}"]`);
  const body = details?.querySelector(".collapsible-body");
  if (details && body && body.innerHTML.trim() !== "") {
    body.hidden = !state.sections[key];
    details.open = !!state.sections[key];
    return;
  }
  const active = document.querySelector(".tab-item.active");
  renderView(active ? active.dataset.view : "activity");
}
// badge: short Chinese count label, e.g. "3 个运行中" / "8 项"
function section(key, title, badge, bodyHtml, { resetDefaults = false, headerExtra = "", keepBody = false } = {}) {
  if (resetDefaults) sectionDefaultsSeen.clear();
  const firstOnPage = sectionDefaultsSeen.size === 0;
  sectionDefaultsSeen.add(key);
  const open = Object.prototype.hasOwnProperty.call(state.sections, key) ? !!state.sections[key] : firstOnPage;
  return `
    <details class="collapsible" ${open ? "open" : ""} data-section-key="${esc(key)}">
      <summary onclick="event.preventDefault();toggleSection('${esc(key)}')">
        <span class="collapsible-title">${esc(title)}</span>
        ${badge ? `<span class="collapsible-badge">${esc(badge)}</span>` : ""}
        ${headerExtra ? `<span class="collapsible-extra" onclick="event.stopPropagation()">${headerExtra}</span>` : ""}
      </summary>
      <div class="collapsible-body" ${open ? "" : "hidden"}>${open || keepBody ? bodyHtml : ""}</div>
    </details>`;
}

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/** "（3 小时前）" — absolute timestamps alone read as stale or wrong. */
function relativeSuffix(value) {
  if (!value) return "";
  const when = new Date(value);
  if (Number.isNaN(when.getTime())) return "";
  const seconds = Math.max(0, Math.round((Date.now() - when.getTime()) / 1000));
  if (seconds < 60) return "（刚刚）";
  if (seconds < 3600) return `（${Math.floor(seconds / 60)} 分钟前）`;
  if (seconds < 86400) return `（${Math.floor(seconds / 3600)} 小时前）`;
  return `（${Math.floor(seconds / 86400)} 天前）`;
}

function fmtDateTime(v) {
  if (!v) return "—";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function fmtTime(v) {
  if (!v) return "—";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}
function relativeTime(v) {
  const t = new Date(v).getTime();
  if (!Number.isFinite(t)) return "";
  const mins = Math.max(0, Math.floor((Date.now() - t) / 60000));
  if (mins < 1) return "刚刚";
  if (mins < 60) return `${mins} 分钟前`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}
function fmtPct(v, digits = 1) {
  if (v === null || v === undefined) return "未提供";
  return `${(Number(v) * 100).toFixed(digits)}%`;
}
function fmtElapsed(fromIso) {
  const start = new Date(fromIso).getTime();
  if (Number.isNaN(start)) return "已运行 —";
  const secs = Math.max(0, Math.floor((Date.now() - start) / 1000));
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return m > 0 ? `已运行 ${m} 分 ${s} 秒` : `已运行 ${s} 秒`;
}
function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2600);
}
// Inline confirm: a small green line next to the triggering control, fades after 3s.
// Used instead of toast for "created job" / "saved config" (R2 §7 — toast is easy to miss / noisy when stacked).
function inlineConfirm(elId, message) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.textContent = message;
  el.classList.remove("fade");
  clearTimeout(el._fadeTimer);
  el._fadeTimer = setTimeout(() => { el.classList.add("fade"); }, 3000);
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(`${state.api.replace(/\/$/, "")}${path}`, { ...options, headers });
  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
  const text = await response.text();
  return text ? JSON.parse(text) : null;
}

// ---------- semantic helpers ----------
const RISK_TONE = { high: "danger", medium: "warn", low: "ok" };
const ATTENTION_LABEL = {
  high_risk_waiting: "高风险 · 每次确认",
  suggest_once: "建议 · 确认一次",
  auto_done: "自动 · 已处理 / 可撤销",
  ignored: "已忽略 · AI 已知情",
};
const ATTENTION_TONE = { high_risk_waiting: "danger", suggest_once: "warn", auto_done: "ok", ignored: "neutral" };
const LEVEL_LABEL = { auto: "自动", once: "确认一次", per_action: "每次确认", deny: "禁止" };
const LEVEL_TONE = { auto: "ok", once: "warn", per_action: "danger", deny: "neutral" };

function pill(label, tone) {
  return `<span class="pill ${tone}"><i class="dot"></i>${esc(label)}</span>`;
}
function attentionPill(attention) {
  return pill(ATTENTION_LABEL[attention] || attention || "—", ATTENTION_TONE[attention] || "neutral");
}
function riskBarClass(attention) {
  return `risk-${ATTENTION_TONE[attention] || "neutral"}`;
}
function confidenceText(item) {
  if (item.confidence === null || item.confidence === undefined) return "置信度 未提供";
  const basis = item.confidence_basis === "user_action" ? "（用户明确操作）" : "";
  return `置信度 ${fmtPct(item.confidence, 0)}${basis}`;
}
function emptyState(title, note) {
  return `<div class="empty-state"><strong>${esc(title)}</strong>${esc(note || "")}</div>`;
}
/**
 * Something that does not exist yet, as opposed to something that is empty.
 * These looked identical before, so "not built" and "no data" were the same
 * screen -- which reads as "it might be broken".
 */
function unbuiltPlaceholder(title, detail) {
  return `<div class="card" style="padding:16px">
      <div class="card-head-left">${unavailableTag("未接入")}<span class="card-title" style="margin:0;font-size:var(--text-sm);font-weight:400">${esc(title)}</span></div>
      <p class="card-footnote" style="border-top:0;margin-top:8px">${esc(detail)}</p>
    </div>`;
}

function unavailableTag(text = "未接入") {
  return `<span class="unavailable-tag">${esc(text)}</span>`;
}

// ---------- shared chrome ----------
// One source of truth for "waiting for you": unexpired pending actions.
// The header lamp, the waiting pill and the metric cell must never disagree.
function waitingActions() {
  return (state.data.pendingActions || []).filter(
    (p) => new Date(p.expires_at).getTime() > Date.now()
  );
}
function renderChrome() {
  const waiting = waitingActions();
  const connected = (state.data.connected || []).length > 0;
  $("#lampOnlineText").textContent = connected ? "在线 · 感知中" : "离线 · 未连接";
  $("#lampOnline").querySelector(".lamp-dot").className = `lamp-dot ${connected ? "ok" : "danger"}`;
  $("#lampWaitingText").textContent = `等待你 ${waiting.length}`;
  renderWaitingPill(waiting.length);
}
function renderWaitingPill(count) {
  const el = $("#waitingPill");
  if (!count || Date.now() < state.pillSnoozedUntil) {
    el.hidden = true;
    el.classList.remove("collapsed");
    // Never leave a stale count behind for the next paint to resurrect.
    $("#waitingPillText").textContent = "";
    return;
  }
  el.hidden = false;
  $("#waitingPillText").textContent = `等待你许可 · ${count}`;
}

// ================= 01 活动流 =================
function activityMetrics() {
  const items = state.data.activityAttention || [];
  const all = state.data.activityAll || [];
  const autoDone = items.filter((i) => i.attention === "auto_done").length;
  const waiting = waitingActions().length;
  const devices = state.data.devices || [];
  const lastSeen = devices.map((d) => d.last_seen).filter(Boolean).sort().pop();
  return `
    <div class="metric-strip">
      <div class="metric-cell"><div class="metric-label">有意义的事件</div><div class="metric-value">${String(items.length).padStart(2, "0")}</div><div class="metric-note">今日 ${all.length} 条记录中</div></div>
      <div class="metric-cell"><div class="metric-label">自动完成</div><div class="metric-value">${String(autoDone).padStart(2, "0")}</div><div class="metric-note">本地操作均有撤销记录</div></div>
      <div class="metric-cell"><div class="metric-label">等待你许可</div><div class="metric-value">${String(waiting).padStart(2, "0")}</div><div class="metric-note">外发消息 · 尚未发送</div></div>
      <div class="metric-cell"><div class="metric-label">最近同步</div><div class="metric-value">${fmtTime(lastSeen)}</div><div class="metric-note">${devices[0] ? esc(devices[0].name) : "尚未注册设备"} · Wi-Fi</div></div>
    </div>`;
}

function evidenceBlock(item) {
  const ev = item.evidence || {};
  const judgment = ev.judgment || {};
  const refs = ev.refs || {};
  const raw = ev.raw || [];
  const memRefs = (refs.memories || []).map((r) => `<span>记忆 <code>${esc(r.id)}</code>${r.available === false ? " · 资料已清理" : ""}</span>`).join("");
  const evtRefs = (refs.events || []).map((r) => `<span>事件 <code>${esc(r.id)}</code>${r.available === false ? " · 资料已清理" : ""}</span>`).join("");
  const toolRefs = (refs.tools || []).map((r) => `<span>工具 <code>${esc(r.id)}</code> ${esc(r.result || "")}</span>`).join("");
  const rawLine = raw.length
    ? raw.map((r) => `${esc(r.kind || "")} ${esc(r.id || "")}${r.needs_grant ? "（需许可）" : ""}`).join(" · ")
    : "无原始资料引用";
  return `
    <div class="evidence">
      <div class="evidence-layer">
        <span class="evidence-num mono">01</span>
        <div><p class="evidence-label">结论</p><p class="evidence-text">${esc(ev.conclusion || item.title || "—")}</p></div>
      </div>
      <div class="evidence-layer">
        <span class="evidence-num mono">02</span>
        <div><p class="evidence-label">判断与依据</p><p class="evidence-text">${esc(judgment.text || "—")}（${confidenceText(item)} · 风险 ${esc(item.risk || "—")}）</p></div>
      </div>
      <div class="evidence-layer">
        <span class="evidence-num mono">03</span>
        <div><p class="evidence-label">引用记忆 / 事件 / 工具</p><div class="evidence-refs">${memRefs}${evtRefs}${toolRefs}` + `${!memRefs && !evtRefs && !toolRefs ? "<span>暂无引用</span>" : ""}</div></div>
      </div>
      <div class="evidence-layer">
        <span class="evidence-num mono">04</span>
        <div><p class="evidence-label">原始输入与门控记录</p><p class="evidence-raw">${esc(rawLine)}</p></div>
      </div>
    </div>`;
}

function activityActionButtons(item) {
  const actions = item.available_actions || [];
  const buttons = [];
  if (item.attention === "high_risk_waiting") {
    buttons.push(`<button class="btn btn-safe" onclick="runActivityAction('${esc(item.id)}','accept')">审阅并确认发送一次</button>`);
    buttons.push(`<button class="btn btn-text" onclick="runActivityAction('${esc(item.id)}','modify')">修改草稿</button>`);
    buttons.push(`<button class="btn btn-danger" onclick="runActivityAction('${esc(item.id)}','reject')">取消待发</button>`);
    return buttons.join("");
  }
  if (item.attention === "suggest_once") {
    if (actions.includes("apply_today")) buttons.push(`<button class="btn btn-safe" onclick="runActivityAction('${esc(item.id)}','apply_today')">仅今天应用</button>`);
    buttons.push(`<button class="btn btn-text" onclick="runActivityAction('${esc(item.id)}','ignore')">忽略并留痕</button>`);
    return buttons.join("");
  }
  if (item.attention === "ignored") {
    buttons.push(`<button class="btn btn-safe" onclick="runActivityAction('${esc(item.id)}','undo')">恢复提醒</button>`);
    buttons.push(`<button class="btn btn-text" onclick="toggleEvidence('${esc(item.id)}')">查看原记录</button>`);
    return buttons.join("");
  }
  if (actions.includes("undo")) buttons.push(`<button class="btn btn-safe" onclick="runActivityAction('${esc(item.id)}','undo')">撤销提醒</button>`);
  if (actions.includes("modify")) buttons.push(`<button class="btn btn-text" onclick="runActivityAction('${esc(item.id)}','modify')">编辑</button>`);
  if (actions.includes("delete")) buttons.push(`<button class="btn btn-text" onclick="runActivityAction('${esc(item.id)}','delete')">删除</button>`);
  return buttons.join("");
}

function activityCard(item) {
  const isHighRisk = item.attention === "high_risk_waiting";
  const expanded = state.expanded.has(item.id);
  const grant = item.grant;
  const historyMeta = item.detail?.history_meta || {};
  const footnote = `创建 ${fmtTime(historyMeta.created)} · 修改 ${historyMeta.modified || 0} · 删除 ${historyMeta.deleted || 0} · 撤销 ${historyMeta.undone || 0}；后续动作均追加审计，不覆盖原记录。`;
  const metricLine = `${confidenceText(item)} / 风险 ${esc(item.risk || "—")} · 记忆 ${item.evidence?.refs?.memories?.length || 0} / 工具 ${item.evidence?.refs?.tools?.length || 0} · 来源 ${esc(item.source || "—")}`;

  if (isHighRisk) {
    return `
      <div class="card card-risk">
        <div class="card-head">
          <div class="card-head-left">${attentionPill(item.attention)}</div>
          <span class="card-id">${fmtTime(item.updated_at)} / ${esc(item.event_id)}</span>
        </div>
        <p class="card-title">${esc(item.title)}</p>
        <p class="card-body">${esc(item.detail?.reply || item.content || "")}</p>
        <div class="card-metrics">
          <span>${metricLine}</span>
          <button class="why-toggle" onclick="toggleEvidence('${esc(item.id)}')">为什么 · 依据${expanded ? "已展开 ⌃" : "已折叠 ⌄"}</button>
        </div>
        <p class="card-body">确认前可以取消待发；送达后不能保证撤回。更正消息与补偿动作将单独留痕。${grant?.compensation_only ? "" : ""}</p>
        <div class="card-actions">${activityActionButtons(item)}</div>
        ${expanded ? evidenceBlock(item) : ""}
        <p class="card-footnote">${esc(footnote)}</p>
      </div>`;
  }
  return `
    <div class="card attn-card ${riskBarClass(item.attention)}">
      <div class="card-head">
        <div class="card-head-left">${attentionPill(item.attention)}</div>
        <span class="card-id">${fmtTime(item.updated_at)} / ${esc(item.event_id)}</span>
      </div>
      <p class="card-title">${esc(item.title)}</p>
      <div class="card-metrics">
        <span>${metricLine}</span>
        <button class="why-toggle" onclick="toggleEvidence('${esc(item.id)}')">为什么 · 四层依据${expanded ? "已展开 ⌃" : "已折叠 ⌄"}</button>
      </div>
      <div class="card-actions">${activityActionButtons(item)}</div>
      ${expanded ? evidenceBlock(item) : ""}
      <p class="card-footnote">${esc(footnote)}</p>
    </div>`;
}

function composeStrip() {
  if (!state.composeOpen) {
    return `<div class="compose-strip compose-collapsed" onclick="toggleCompose()"><span>直接回复…（针对某条活动的即时指令）</span></div>`;
  }
  return `
    <div class="compose-strip compose-open">
      <textarea id="composeInput" placeholder="例如：明天上午 10 点提醒我联系张三"></textarea>
      <div class="compose-actions">
        <button class="btn btn-text" onclick="toggleCompose()">收起</button>
        <button class="btn btn-safe" ${state.busy.has("sendCompose") ? "disabled" : ""} onclick="sendCompose()">${state.busy.has("sendCompose") ? "发送中…" : "发送"}</button>
        ${state.composeError ? `<span class="inline-error">${esc(state.composeError)}</span>` : ""}
      </div>
    </div>`;
}

function auditSummary(action, detail) {
  // Audit rows carry machine-shaped detail. Surface the decision, not the blob.
  const text = String(detail || "");
  if (!text) return "—";
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === "object") {
      const parts = [];
      if (parsed.counts && typeof parsed.counts === "object") {
        const total = Object.values(parsed.counts).reduce((a, b) => a + (Number(b) || 0), 0);
        parts.push(`共 ${total} 条记录被清除`);
      }
      if (parsed.capability) parts.push(String(parsed.capability));
      if (parsed.level) parts.push(`档位 ${parsed.level}`);
      if (parsed.reason) parts.push(String(parsed.reason));
      if (parsed.action) parts.push(String(parsed.action));
      if (parts.length) return parts.join(" · ");
    }
  } catch {}
  return text.length > 120 ? text.slice(0, 117) + "…" : text;
}

function historyTable() {
  const rows = (state.data.auditRecent || []).slice(0, 6);
  const actionLabels = { "job.run_shell": "执行命令" };
  return `
    <div class="table-wrap">
      <table class="table">
        <thead><tr><th>时间 / 记录</th><th>动作</th><th>结果与边界</th></tr></thead>
        <tbody>
          ${rows.map((r) => `<tr><td class="mono">${fmtTime(r.created_at)} / <span class="id">#${esc(r.id)}</span></td><td>${esc(actionLabels[r.action] || r.action)}</td><td title="${esc(r.detail || "")}">${esc(auditSummary(r.action, r.detail))}</td></tr>`).join("") || '<tr class="empty-row"><td colspan="3">暂无记录</td></tr>'}
        </tbody>
      </table>
    </div>`;
}

function fmtBytes(v) {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${n} B`;
}
function artifactKindPill(kind) {
  return `<span class="pill neutral">${esc(kind || "—")}</span>`;
}
function artifactRow(a) {
  return `
    <div class="card">
      <p class="card-body" style="margin:0 0 6px">${esc(a.title || "—")} · ${artifactKindPill(a.kind)} · ${fmtBytes(a.bytes)} · ${fmtDateTime(a.created_at)}</p>
      <div class="card-actions">
        <span class="muted">码 ${a.code ? esc(a.code) : "—"} · 永久链接</span>
        <button class="btn btn-text" onclick="copyArtifactLink('${esc(a.url || a.path || "")}')">复制链接</button>
        ${a.url || a.path ? `<a class="btn btn-text" href="${esc(a.url || a.path)}" target="_blank" rel="noopener">打开</a>` : ""}
      </div>
    </div>`;
}
function uploadsPanel() {
  const uploads = state.data.uploads || [];
  const err = state.uploadError;
  const body = `
    <div class="card" style="padding:12px 16px;margin-bottom:10px">
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <input type="file" id="uploadInput" accept=".txt,.md,.markdown,.json,.csv,.log,.html,.htm,.xml,.yaml,.yml,.pdf,.docx">
        <button class="btn btn-text" id="uploadBtn" onclick="uploadMaterial()">上传</button>
        <span class="hint" id="uploadStatus">${err ? "" : "传上来之后 Jarvis 会读它，不需要你复制粘贴"}</span>
      </div>
      ${err ? `<p class="card-footnote" style="color:var(--danger-fg);border-top:0;padding-top:0">${esc(err)}</p>` : ""}
    </div>
    ${uploads.length ? uploads.map(uploadRow).join("") : emptyState("还没有传过文件")}
  `;
  return section("uploads", "我传过去的材料", `${uploads.length} 项`, body);
}
function uploadRow(item) {
  const size = formatBytes(item.bytes);
  const readable = item.chars > 0
    ? `可读 ${item.chars.toLocaleString()} 字`
    : (item.note || "读不了内容");
  return `
    <div class="card" style="padding:12px 16px;margin-bottom:8px">
      <div style="display:flex;justify-content:space-between;gap:12px;align-items:baseline">
        <strong>${esc(item.filename)}</strong>
        <span class="hint">${size} · ${esc(readable)}</span>
      </div>
      <div class="card-footnote">${esc(item.created_at.slice(0, 16).replace("T", " "))} · 编号 ${esc(item.id)}
        <button class="btn btn-text" onclick="window.open('${esc(item.url)}','_blank','noopener')">下载原文件</button>
      </div>
    </div>
  `;
}
async function uploadMaterial() {
  const input = $("#uploadInput");
  const file = input?.files?.[0];
  if (!file) { state.uploadError = "先选一个文件"; renderActivity(); return; }
  const form = new FormData();
  form.append("file", file);
  try {
    const result = await api("/api/uploads", { method: "POST", body: form });
    state.uploadError = "";
    toast(result.chars > 0 ? `已接收，可读 ${result.chars} 字` : `已保存，${result.note || "但读不了内容"}`);
    await refresh();
  } catch (e) {
    state.uploadError = e.message;
    renderActivity();
  }
}
function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}
function artifactsPanel() {
  const artifacts = state.data.artifacts || [];
  const err = state.artifactLookupError;
  const body = `
    <div class="block-head" style="margin-top:0"><span class="hint">短码输入 <input type="text" id="artifactCodeInput" maxlength="4" style="width:70px;text-transform:uppercase"> <button class="btn btn-text" onclick="lookupArtifactCode()">打开</button></span></div>
    ${err ? `<p class="card-footnote" style="color:var(--danger-fg);border-top:0;padding-top:0">${esc(err)}</p>` : ""}
    ${artifacts.length ? artifacts.map(artifactRow).join("") : emptyState("任务完成后生成的文件会出现在这里")}
  `;
  return section("artifacts", "最近交付物", `${artifacts.length} 项`, body);
}
async function lookupArtifactCode() {
  const input = $("#artifactCodeInput");
  const code = (input?.value || "").trim().toUpperCase();
  if (!code) return;
  try {
    const artifact = await api(`/api/artifacts?code=${encodeURIComponent(code)}`);
    state.artifactLookupError = "";
    const url = artifact?.url || artifact?.path;
    if (url) window.open(url, "_blank", "noopener");
  } catch (e) {
    state.artifactLookupError = e.message.startsWith("404") ? "没有这个短码" : e.message;
  }
  renderActivity();
}
async function copyArtifactLink(url) {
  if (!url) { toast("没有可用链接"); return; }
  try {
    await navigator.clipboard.writeText(url);
    toast("已复制链接");
  } catch (e) { toast("复制失败"); }
}

// ---------- Jobs (长任务) ----------
const JOB_STATUS_LABEL = { queued: "待批准", running: "进行中", done: "已完成", failed: "失败", cancelled: "已取消" };
const JOB_STATUS_TONE = { queued: "neutral", running: "warn", done: "ok", failed: "danger", cancelled: "neutral" };

async function copyJobError(jobId) {
  const job = (state.data.jobs || []).find((j) => j.id === jobId);
  const text = job?.error || "";
  if (!text) { toast("没有错误信息"); return; }
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制错误");
  } catch (e) { toast("复制失败"); }
}

// ---------- R3: running-job elapsed time, 1s tick touching only the text node ----------
function stopElapsedTimer() {
  if (state.elapsedTimer) { clearInterval(state.elapsedTimer); state.elapsedTimer = null; }
}
function startElapsedTimer() {
  stopElapsedTimer(); // never stack timers across re-renders/page switches
  state.elapsedTimer = setInterval(() => {
    document.querySelectorAll("[data-job-elapsed]").forEach((el) => {
      el.textContent = fmtElapsed(el.dataset.startedAt);
    });
  }, 1000);
}

function jobProgressText(job) {
  const steps = job.steps;
  const max = job.max_steps;
  if (steps === null || steps === undefined || max === null || max === undefined) return "—";
  return `${steps}/${max} 步`;
}

function jobStepSummary(value) {
  if (!value || typeof value !== "object") return null;
  if (value.error) return `失败：${value.error}`;
  if (value.code && value.url) return `已交付 · 码 ${value.code} · ${value.title || ""}`;
  if (value.stdout) return String(value.stdout);
  if (value.path && typeof value.bytes === "number") return `${value.path}（${value.bytes} 字节）`;
  if (Array.isArray(value.items)) {
    const names = value.items.map((item) => item.path || item.name).filter(Boolean);
    return names.length ? `${names.length} 项：${names.join(", ")}` : "空目录";
  }
  if (typeof value.returncode === "number") {
    return `退出码 ${value.returncode}`;
  }
  return null;
}

function jobStepText(step) {
  // The backend stores the tool result as JSON; show the part a human reads.
  const raw = step.detail;
  if (raw && typeof raw === "object") {
    return jobStepSummary(raw) ?? JSON.stringify(raw);
  }
  const text = String(raw ?? "");
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === "object") {
      return jobStepSummary(parsed) ?? text;
    }
  } catch { /* not JSON, show as-is */ }
  return text;
}

function jobStepLine(step) {
  const text = jobStepText(step).replace(/\s+/g, " ").trim() || "—";
  const shown = text.length > 96 ? text.slice(0, 96) + "…" : text;
  return `<div class="job-step"><span class="mono job-step-seq">#${esc(step.seq)}</span><span class="job-step-kind">${esc(step.kind || "—")}</span><span class="job-step-detail">${esc(shown)}</span></div>`;
}

function jobStepsBlock(job, steps) {
  if (!state.jobDetails[job.id] && (job.status === "running" || job.status === "queued")) return `<div class="job-steps"><span class="muted">读取步骤中…</span></div>`;
  if (!steps || !steps.length) return `<div class="job-steps"><span class="muted">暂无步骤</span></div>`;
  const expanded = state.jobStepsExpanded.has(job.id);
  const visible = expanded ? steps : steps.slice(-3);
  return `
    <div class="job-steps">
      ${visible.map(jobStepLine).join("")}
      ${steps.length > 3 ? `<button class="btn btn-text" onclick="toggleJobSteps('${esc(job.id)}')">${expanded ? "收起步骤" : `展开全部 ${steps.length} 步 ›`}</button>` : ""}
    </div>`;
}

function jobProgressBar(job) {
  const steps = job.steps;
  const max = job.max_steps;
  if (steps === null || steps === undefined || max === null || max === undefined || !max) {
    return `<div class="job-progress"><div class="job-progress-track"></div></div>`;
  }
  const pct = Math.min(100, Math.max(0, (steps / max) * 100));
  const tone = JOB_STATUS_TONE[job.status] || "neutral";
  return `<div class="job-progress"><div class="job-progress-track"><div class="job-progress-fill tone-${tone}" style="width:${pct}%"></div></div></div>`;
}

function jobArtifactsBlock(jobId) {
  const detail = state.jobDetails[jobId];
  const artifacts = detail?.artifacts || [];
  if (!artifacts.length) return "";
  return `
    <div class="job-artifacts">
      <span class="hint">交付物</span>
      ${artifacts.map((item) => `
        <span class="job-artifact">
          ${pill(item.kind || "file", "neutral")}
          <span class="mono">${esc(item.code || "—")}</span>
          <span>${esc(item.title || "—")}</span><span class="hint">永久链接</span>
          <button class="btn btn-text" onclick="window.open('${esc(item.url)}','_blank','noopener')">打开</button>
          <button class="btn btn-text" onclick="copyArtifactLink('${esc(item.url)}')">复制链接</button>
        </span>`).join("")}
    </div>`;
}

function jobCard(job) {
  const detail = state.jobDetails[job.id];
  // The API returns the count in `steps` and the rows in `steps_detail`.
  const steps = detail?.job?.steps_detail || detail?.steps_detail || job.steps_detail || [];
  const isQueued = job.status === "queued";
  const isRunning = job.status === "running";
  const isFailed = job.status === "failed";
  const tone = JOB_STATUS_TONE[job.status] || "neutral";
  const approveBusy = state.busy.has(`approve-${job.id}`);
  const cancelBusy = state.busy.has(`cancel-${job.id}`);
  const actions = [];
  if (isQueued) actions.push(`<button class="btn btn-safe" ${approveBusy ? "disabled" : ""} onclick="approveJob('${esc(job.id)}')">${approveBusy ? "批准中…" : "批准"}</button>`);
  if (isQueued || isRunning) actions.push(`<button class="btn btn-danger" ${cancelBusy ? "disabled" : ""} onclick="cancelJob('${esc(job.id)}')">${cancelBusy ? "取消中…" : "取消"}</button>`);
  if (isFailed) {
    actions.push(`<button class="btn btn-safe" ${approveBusy ? "disabled" : ""} onclick="approveJob('${esc(job.id)}')">${approveBusy ? "重试中…" : "重试"}</button>`);
    actions.push(`<button class="btn btn-text" onclick="copyJobError('${esc(job.id)}')">复制错误</button>`);
  }
  return `
    <div class="card job-card ${isQueued ? "job-card-queued" : ""}" data-job-id="${esc(job.id)}">
      <div class="card-head">
        <div class="card-head-left">${pill(JOB_STATUS_LABEL[job.status] || job.status, tone)}<span class="card-title" style="margin:0;font-size:var(--text-md);font-weight:400">${esc(job.goal)}</span></div>
        <span class="card-id">${esc(jobProgressText(job))}</span>
      </div>
      ${jobProgressBar(job)}
      ${isQueued ? `<p class="job-queued-notice">等待你批准才会执行；批准前不会运行任何命令。</p>` : ""}
      ${isRunning ? `<p class="card-body job-elapsed" data-job-elapsed="${esc(job.id)}" data-started-at="${esc(job.created_at || "")}">${fmtElapsed(job.created_at)}</p>` : ""}
      ${isFailed ? `<p class="card-body job-error">失败于 ${esc(fmtTime(job.updated_at || job.created_at))} · ${esc(job.error || "—")}</p>` : ""}
      ${jobStepsBlock(job, steps)}
      ${jobArtifactsBlock(job.id)}
      <div class="card-actions">${actions.join("")}</div>
    </div>`;
}

function jobsPanel() {
  const jobs = state.data.jobs || [];
  const running = jobs.filter((j) => j.status === "running" || j.status === "queued").length;
  const body = `
    <div id="jobsList">${jobs.length ? jobs.map(jobCard).join("") : emptyState("新建一个目标，批准后才会执行")}</div>
  `;
  const createBusy = state.busy.has("createJob");
  const newJobControls = `
      <input type="text" id="jobGoalInput" placeholder="新建目标…" value="${esc(state.jobGoalInput)}" ${createBusy ? "disabled" : ""}
             onkeydown="if(event.key==='Enter'){createJob();}">
      <button class="btn btn-safe" id="jobStartBtn" ${createBusy || !state.jobGoalInput.trim() ? "disabled" : ""}
              onclick="event.stopPropagation();createJob()">${createBusy ? "创建中…" : "开始"}</button>
      <span class="inline-confirm fade" id="jobCreateConfirm"></span>`;
  return section("jobs", "长任务", running ? `${running} 个运行中` : `${jobs.length} 项`, body,
                 { headerExtra: newJobControls, keepBody: true });
}

function toggleJobSteps(id) {
  if (state.jobStepsExpanded.has(id)) state.jobStepsExpanded.delete(id); else state.jobStepsExpanded.add(id);
  renderActivity();
}

function renderJobsOnly() {
  // Re-rendering the whole activity page takes ~2s on a long history; a new job
  // must appear immediately, so touch only the list and the badge.
  const jobs = state.data.jobs || [];
  const list = document.getElementById("jobsList");
  if (list) {
    list.innerHTML = jobs.length
      ? jobs.map(jobCard).join("")
      : emptyState("新建一个目标，批准后才会执行");
  }
  const badge = document.querySelector('[data-section-key="jobs"] .collapsible-badge');
  if (badge) {
    const active = jobs.filter((job) => job.status === "running" || job.status === "queued").length;
    badge.textContent = active ? `${active} 个运行中` : `${jobs.length} 项`;
  }
}

async function createJob() {
  const input = $("#jobGoalInput");
  const goal = (input?.value || state.jobGoalInput || "").trim();
  if (!goal || state.busy.has("createJob")) return;
  // The list lives inside a collapsible section; creating something the user
  // cannot see is worse than not creating it, so open the section first.
  const jobsSectionWasOpen = sectionIsOpen("jobs");
  state.sections.jobs = true;
  saveSectionState();
  state.busy.add("createJob");
  // Optimistic placeholder: show it immediately, let job_status (SSE) correct it.
  const placeholderId = `pending-${Date.now()}`;
  (state.data.jobs ||= []).unshift({ id: placeholderId, goal, status: "queued", steps: null, max_steps: null });
  state.jobGoalInput = "";
  // #jobsList exists even while collapsed (keepBody), so this is instant.
  renderJobsOnly();
  if (!jobsSectionWasOpen) {
    const details = document.querySelector('[data-section-key="jobs"]');
    const body = details?.querySelector(".collapsible-body");
    if (details && body) { details.open = true; body.hidden = false; }
  }
  try {
    const created = await api("/api/jobs", { method: "POST", body: JSON.stringify({ goal }) });
    const idx = (state.data.jobs || []).findIndex((j) => j.id === placeholderId);
    if (idx !== -1) {
      if (created?.id) state.data.jobs[idx] = { ...state.data.jobs[idx], id: created.id, ...created };
      else state.data.jobs.splice(idx, 1);
    }
    state.busy.delete("createJob");
    await refreshJobs();
    renderJobsOnly();
    inlineConfirm("jobCreateConfirm", "已创建，等待批准");
  } catch (e) {
    state.busy.delete("createJob");
    state.data.jobs = (state.data.jobs || []).filter((j) => j.id !== placeholderId);
    renderJobsOnly();
    toast(e.message);
  }
}
async function approveJob(id) {
  if (state.busy.has(`approve-${id}`)) return;
  state.busy.add(`approve-${id}`);
  rerenderJobCard(id);
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}/approve`, { method: "POST" });
    toast("已批准");
    state.busy.delete(`approve-${id}`);
    await refreshJobs();
    renderActivity();
  } catch (e) {
    state.busy.delete(`approve-${id}`);
    rerenderJobCard(id);
    toast(e.message);
  }
}
async function cancelJob(id) {
  if (state.busy.has(`cancel-${id}`)) return;
  state.busy.add(`cancel-${id}`);
  rerenderJobCard(id);
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
    toast("已取消");
    state.busy.delete(`cancel-${id}`);
    await refreshJobs();
    renderActivity();
  } catch (e) {
    state.busy.delete(`cancel-${id}`);
    rerenderJobCard(id);
    toast(e.message);
  }
}

async function refreshJobs() {
  try {
    const list = await api("/api/jobs");
    state.data.jobs = list?.jobs || [];
  } catch {
    state.data.jobs = state.data.jobs || [];
  }
  // Fetch details for every visible job, not only the live ones: a finished job
  // that shows "暂无步骤" is indistinguishable from one that did nothing.
  const visible = (state.data.jobs || []).slice(0, 8);
  await Promise.all(visible.map(async (j) => {
    try {
      state.jobDetails[j.id] = await api(`/api/jobs/${encodeURIComponent(j.id)}`);
    } catch { /* keep last known detail */ }
  }));
  scheduleJobPoll();
}

function scheduleJobPoll() {
  if (state.jobPollTimer) { clearInterval(state.jobPollTimer); state.jobPollTimer = null; }
  const hasActive = (state.data.jobs || []).some((j) => j.status === "running" || j.status === "queued");
  if (!hasActive) return;
  state.jobPollTimer = setInterval(async () => {
    const active = document.querySelector(".tab-item.active");
    if (active?.dataset.view !== "activity") return;
    await refreshJobs();
    renderActivity();
  }, 5000);
}

// ---------- SSE: live stream (R2 §3) ----------
function rerenderJobCard(jobId) {
  const job = (state.data.jobs || []).find((j) => j.id === jobId);
  const mount = document.querySelector(`#jobsList [data-job-id="${jobId}"]`);
  if (!job || !mount) {
    // Job not yet in the visible list (e.g. first event for a brand-new job) — full panel render covers it.
    const active = document.querySelector(".tab-item.active");
    if (active?.dataset.view === "activity") renderActivity();
    return;
  }
  mount.outerHTML = jobCard(job);
  const hasRunning = (state.data.jobs || []).some((j) => j.status === "running");
  if (hasRunning) startElapsedTimer(); else stopElapsedTimer();
}
function upsertJobStatus(payload) {
  const jobs = state.data.jobs || (state.data.jobs = []);
  let idx = jobs.findIndex((j) => j.id === payload.job_id);
  const patch = { goal: payload.goal, created_at: payload.created_at, updated_at: payload.updated_at, steps: payload.steps, max_steps: payload.max_steps, status: payload.status, error: payload.error };
  if (payload.goal) {
    const placeholder = jobs.findIndex((j) => String(j.id).startsWith("pending-") &&
      j.goal === payload.goal && Math.abs(Date.now() - Number(String(j.id).slice(8))) <= 10000);
    if (placeholder !== -1 && placeholder !== idx) {
      jobs.splice(placeholder, 1);
      idx = jobs.findIndex((j) => j.id === payload.job_id);
    }
  }
  if (idx === -1) {
    jobs.unshift({ id: payload.job_id, goal: "", ...patch });
  } else {
    jobs[idx] = { ...jobs[idx], ...patch };
  }
  rerenderJobCard(payload.job_id);
}
function appendJobProgress(payload) {
  const detail = (state.jobDetails[payload.job_id] ||= {});
  const steps = (detail.steps_detail ||= []);
  steps.push({ seq: payload.seq, kind: payload.kind, detail: payload.detail, status: payload.status });
  rerenderJobCard(payload.job_id);
}
function prependActivity(payload) {
  const item = {
    id: payload.event_id,
    event_id: payload.event_id,
    attention: payload.attention,
    title: payload.title,
    detail: { reply: payload.reply },
  };
  state.data.activityAll = [item, ...(state.data.activityAll || [])];
  state.data.activityAttention = state.data.activityAll.filter((i) => i.attention !== "ignored");
  const active = document.querySelector(".tab-item.active");
  if (active?.dataset.view === "activity") renderActivity();
}
// R5: the banner tells the truth — reconnect attempt count, and how stale the data
// actually is (seconds since lastSyncedAt), not a made-up "正在重连" with no numbers.
function streamNoticeText() {
  const staleSecs = Math.max(0, Math.floor((Date.now() - state.lastSyncedAt) / 1000));
  return `实时连接断开，正在重连（第 ${state.streamReconnectAttempts} 次）· 数据可能滞后 ${staleSecs} 秒`;
}
function tickStreamNotice() {
  const el = $("#streamNotice");
  if (el && !el.hidden) el.textContent = streamNoticeText();
}
function streamFallbackNotice(show) {
  const el = $("#streamNotice");
  if (!el) return;
  el.hidden = !show;
  if (show) { el.textContent = streamNoticeText(); startStreamNoticeTicker(); }
  else stopStreamNoticeTicker();
}
function startStreamNoticeTicker() {
  if (state.streamNoticeTimer) return;
  state.streamNoticeTimer = setInterval(tickStreamNotice, 1000);
}
function stopStreamNoticeTicker() {
  if (state.streamNoticeTimer) { clearInterval(state.streamNoticeTimer); state.streamNoticeTimer = null; }
}
function startStreamFallbackPoll() {
  if (state.streamFallbackTimer) return;
  state.streamFallbackTimer = setInterval(() => { if (state.token && !document.hidden) refresh(); }, 15000);
}
function stopStreamFallbackPoll() {
  if (state.streamFallbackTimer) { clearInterval(state.streamFallbackTimer); state.streamFallbackTimer = null; }
}
function closeStream() {
  if (state.stream) { state.stream.close(); state.stream = null; }
  stopStreamFallbackPoll();
  stopStreamNoticeTicker();
}
function initStream() {
  closeStream();
  if (!state.token) return;
  const url = `${state.api.replace(/\/$/, "")}/api/stream?token=${encodeURIComponent(state.token)}`;
  const es = new EventSource(url);
  state.stream = es;
  es.onopen = () => {
    const wasDisconnected = !state.streamConnected;
    state.streamConnected = true;
    state.streamReconnectAttempts = 0;
    state.lastSyncedAt = Date.now();
    streamFallbackNotice(false); // disappears immediately, no toast (R5)
    stopStreamFallbackPoll();
    if (wasDisconnected) refresh({ resetJobDetails: true });
  };
  es.onerror = () => {
    if (state.streamConnected) state.lastSyncedAt = Date.now(); // last moment we know data was fresh
    state.streamConnected = false;
    state.streamReconnectAttempts += 1;
    streamFallbackNotice(true);
    startStreamFallbackPoll();
  };
  es.addEventListener("job_status", (e) => { state.lastSyncedAt = Date.now(); try { upsertJobStatus(JSON.parse(e.data)); } catch {} });
  es.addEventListener("job_progress", (e) => { state.lastSyncedAt = Date.now(); try { appendJobProgress(JSON.parse(e.data)); } catch {} });
  es.addEventListener("pending_action", (e) => { state.lastSyncedAt = Date.now(); refreshPendingActions(); });
  es.addEventListener("activity", (e) => { state.lastSyncedAt = Date.now(); try { prependActivity(JSON.parse(e.data)); } catch {} });
  es.addEventListener("asr_partial", () => { /* no console surface yet */ });
}

function showMoreActivity() {
  state.activityLimit = (state.activityLimit || 25) + 25;
  renderActivity();
}

function activitySkeleton() {
  // R1: same shape as the real page (metric strip + card list + panel), static gray blocks, no "no data" text.
  const skeletonCard = `
    <div class="skeleton-card">
      <div class="skeleton-block skeleton-line w-40"></div>
      <div class="skeleton-block skeleton-line w-60"></div>
      <div class="skeleton-block skeleton-line last"></div>
    </div>`;
  return `
    <div class="section-head">
      <div class="section-kicker"><span>01 / ATTENTION &amp; AUDIT</span><span>${new Date().toLocaleDateString("zh-CN")} CST</span></div>
      <h1 class="section-title">把注意力留给真正重要的事。</h1>
      <p class="section-lede">结论在前，证据可查。低风险自动处理，高风险只等待你的明确许可。</p>
      <hr class="section-divider">
    </div>
    <div class="metric-strip">
      ${Array.from({ length: 4 }).map(() => `
        <div class="metric-cell">
          <div class="skeleton-block skeleton-line w-60" style="height:10px"></div>
          <div class="skeleton-block skeleton-line w-40" style="height:22px"></div>
          <div class="skeleton-block skeleton-line last" style="height:10px"></div>
        </div>`).join("")}
    </div>
    <div id="activityList">${skeletonCard}${skeletonCard}${skeletonCard}</div>
  `;
}

function renderActivity() {
  if (!state.firstLoadDone) {
    stopElapsedTimer();
    $("#view-activity").innerHTML = activitySkeleton();
    return;
  }
  const attentionItems = state.data.activityAttention || [];
  const allItems = state.data.activityAll || [];
  const list = state.activityView === "attention" ? attentionItems : allItems;
  const connectionNotice = state.refreshError ? `<div class="empty-state" style="margin-bottom:16px">${esc(state.refreshError)} · 以下为最近一次成功同步的数据</div>` : "";
  $("#view-activity").innerHTML = `
    <div class="section-head">
      <div class="section-kicker"><span>01 / ATTENTION &amp; AUDIT</span><span>${new Date().toLocaleDateString("zh-CN")} CST</span></div>
      <h1 class="section-title">把注意力留给真正重要的事。</h1>
      <p class="section-lede">结论在前，证据可查。低风险自动处理，高风险只等待你的明确许可。</p>
      <hr class="section-divider">
    </div>
    ${connectionNotice}
    ${activityMetrics()}
    ${composeStrip()}
    <div class="view-switch">
      <button class="${state.activityView === "attention" ? "active" : ""}" onclick="setActivityView('attention')">有意义 · ${attentionItems.length}</button>
      <button class="${state.activityView === "all" ? "active" : ""}" onclick="setActivityView('all')">全量记录 · ${allItems.length}</button>
    </div>
    <div class="filter-row">
      <select class="filter-select" disabled><option>设备：全部</option></select>
      <select class="filter-select" disabled><option>项目：全部</option></select>
      <select class="filter-select" disabled><option>来源：全部</option></select>
      <select class="filter-select" disabled><option>状态：全部</option></select>
      <select class="filter-select" disabled><option>今天</option></select>
    </div>
    <div id="activityList">${list.slice(0, state.activityLimit || 25).map(activityCard).join("") || emptyState("暂无活动记录", "等待新的事件进入 Jarvis")}</div>
    ${list.length > (state.activityLimit || 25) ? `<button class="btn btn-text" onclick="showMoreActivity()">再看 ${Math.min(25, list.length - (state.activityLimit || 25))} 条（共 ${list.length} 条）</button>` : ""}
    ${section("history", "修改、删除与补偿轨迹", `${(state.data.auditRecent || []).length} 条`, `
      <p class="muted" style="margin:0 0 10px">追加记录 · 不静默覆盖</p>
      ${historyTable()}
      <p class="muted" style="margin-top:14px">全量模式还包含 ${Math.max(0, allItems.length - attentionItems.length)} 条同步、工具与审计记录，所有记录使用同一事件 ID 关联，可导出脱敏审计。</p>
    `, { resetDefaults: true })}
    ${jobsPanel()}
    ${artifactsPanel()}
    ${uploadsPanel()}
  `;
  const hasRunning = (state.data.jobs || []).some((j) => j.status === "running");
  if (hasRunning) startElapsedTimer(); else stopElapsedTimer();
}
function setActivityView(view) { state.activityView = view; renderActivity(); }
function toggleEvidence(id) {
  if (state.expanded.has(id)) state.expanded.delete(id); else state.expanded.add(id);
  renderActivity();
}
function toggleCompose() { state.composeOpen = !state.composeOpen; renderActivity(); }
async function sendCompose() {
  if (state.busy.has("sendCompose")) return;
  const input = $("#composeInput");
  const text = input?.value.trim();
  if (!text) return;
  state.busy.add("sendCompose");
  state.composeError = "";
  renderActivity();
  try {
    await api("/api/message", { method: "POST", body: JSON.stringify({ text }) });
    toast("已发送");
    state.composeOpen = false;
    await refresh();
  } catch (e) { state.composeError = e.message; toast(e.message); }
  finally { state.busy.delete("sendCompose"); if (state.composeOpen) renderActivity(); }
}
async function runActivityAction(id, action) {
  try {
    await api(`/api/activities/${encodeURIComponent(id.replace(/^event:/, ""))}/actions`, {
      method: "POST",
      body: JSON.stringify({ action, idempotency_key: `${action}-${Date.now()}` }),
    });
    toast("已更新");
    await refresh();
  } catch (e) { toast(e.message); }
}

// ================= 04 权限门控 =================
function permissionLegend() {
  const legend = [
    ["自动", "ok", "低风险直接执行并留痕"],
    ["确认一次", "warn", "限定本次范围与有效期"],
    ["每次确认", "danger", "每次动作前都要许可"],
    ["禁止", "neutral", "拒绝执行并记录原因"],
  ];
  return `<div class="legend-row">${legend.map(([l, tone, d]) => `<div class="legend-item">${pill(l, tone)}<span>${esc(d)}</span></div>`).join("")}</div>`;
}
const LEVEL_ORDER = ["auto", "once", "per_action", "deny"];
function matrixRow(name, spec) {
  const pending = state.permissionPending.has(name.key);
  const error = state.permissionErrors[name.key];
  return `
    <tr>
      <td><b>${esc(name.label)}</b><div class="muted">${esc(name.scope)}</div>${pending ? '<span class="muted">生效中…</span>' : error ? `<span class="inline-error">${esc(error)}</span>` : ""}</td>
      ${LEVEL_ORDER.map((level) => {
        if (!spec.levels.includes(level)) return `<td class="level-col"></td>`;
        const selected = (state.permissionPrevious[name.key] || spec.level) === level;
        const locked = !spec.adjustable || pending;
        return `<td class="level-col"><button class="level-cell-btn" ${locked ? "disabled" : ""} onclick="setPermissionLevel('${esc(name.key)}','${level}')"><span class="level-radio ${selected ? "selected" : ""} ${locked ? "locked" : ""}"></span></button></td>`;
      }).join("")}
    </tr>`;
}
const CAPABILITY_LABELS = {
  "reminder.write": { label: "本地提醒 / 可撤销", scope: "仅当前个人项目" },
  "memory.organize_short": { label: "整理短期记忆", scope: "有来源 · 不覆盖长期偏好" },
  "reminder.adjust_today": { label: "今天调整复核时间", scope: "只对今天的提醒生效" },
  "memory.write_long": { label: "写入长期习惯", scope: "摘要候选 · 明确范围后确认" },
  "file.read": { label: "读取未授权文件", scope: "范围外的数据" },
  "message.send": { label: "外发消息", scope: "每条消息与附件" },
  "payment.execute": { label: "财产 / 支付", scope: "对象、金额、币种逐项核对" },
  "device.configure": { label: "修改设备", scope: "配置、APK 升级与回滚" },
  "device.self_iterate": { label: "自迭代", scope: "仅沙箱生成差异" },
  "device.remote_exec": { label: "远程配置 / 执行", scope: "白名单配置逐次许可" },
};
const DAILY_CAPS = ["reminder.write", "memory.organize_short", "reminder.adjust_today", "memory.write_long", "file.read"];
const HIGH_RISK_CAPS = ["message.send", "payment.execute", "device.configure", "device.self_iterate", "device.remote_exec"];

function permissionMatrixTable(caps, keys) {
  return `
    <div class="table-wrap">
      <table class="table matrix-table">
        <thead><tr><th>能力 / 生效范围</th><th class="level-col">自动</th><th class="level-col">确认一次</th><th class="level-col">每次确认</th><th class="level-col">禁止</th></tr></thead>
        <tbody>
          ${keys.map((key) => {
            const spec = caps[key];
            if (!spec) return "";
            return matrixRow({ key, ...CAPABILITY_LABELS[key] }, spec);
          }).join("")}
        </tbody>
      </table>
    </div>`;
}

function highRiskTable(caps) {
  const rows = HIGH_RISK_CAPS.map((key) => {
    const spec = caps[key];
    const label = CAPABILITY_LABELS[key];
    const level = spec ? LEVEL_LABEL[spec.level] : "—";
    return `<tr><td>${esc(label.label)}</td><td>${esc(level)}</td><td>${esc(label.scope)}；${key === "message.send" ? "外部送达后不承诺可撤回" : key === "device.remote_exec" ? "任意命令不能凭信任解锁" : "逐次核对后执行"}</td></tr>`;
  });
  return `
    <div class="table-wrap">
      <table class="table">
        <thead><tr><th>高风险类别</th><th>当前门控</th><th>范围与不可逆边界</th></tr></thead>
        <tbody>${rows.join("")}</tbody>
      </table>
    </div>`;
}

function agentBoundaryCard() {
  const agents = state.data.permissionAgents || [];
  if (!agents.length) return emptyState("暂无受信 Agent", "尚未创建任何委托授权记录。");
  const byAgent = {};
  for (const g of agents) {
    const key = g.subject;
    (byAgent[key] ||= []).push(g);
  }
  return Object.entries(byAgent).map(([subject, grants]) => {
    const active = grants.filter((g) => !g.revoked_at);
    const caps = [...new Set(active.map((g) => g.capability))];
    const expiresAt = active.map((g) => g.expires_at).filter(Boolean).sort().pop();
    const scopeValues = [...new Set(active.map((g) => g.scope_value).filter(Boolean))];
    return `
      <div class="card">
        <div class="card-head"><p class="card-title" style="margin:0">${esc(subject.replace("agent:", ""))}</p>${pill(active.length ? "有限信任" : "已撤销", active.length ? "ok" : "neutral")}</div>
        <dl class="kv-grid">
          <dt>能力</dt><dd>${caps.map(esc).join(" / ") || "—"}；不含 ${HIGH_RISK_CAPS.filter((c) => !caps.includes(c)).map((c) => CAPABILITY_LABELS[c].label).slice(0, 3).join("、")}</dd>
          <dt>范围</dt><dd>${esc(scopeValues.join("、") || "无特定范围")}</dd>
          <dt>有效期</dt><dd>${fmtDateTime(active[0]?.granted_at)} — ${fmtDateTime(expiresAt)}；到期即失效，不能由 Agent 自行续期</dd>
          <dt>高风险例外</dt><dd>没有例外。即使已受信，外发、财产、设备、自迭代与远程配置仍逐次确认。</dd>
        </dl>
        <div class="card-actions">
          <button class="btn btn-text" onclick="toast('编辑边界：请在矩阵页调整能力范围')">编辑边界</button>
          ${active.length ? `<button class="btn btn-danger" onclick="revokeAgentGrants('${esc(subject)}')">撤销授权</button>` : ""}
          <button class="btn btn-text" onclick="toast('访问记录见下方审计表')">查看访问记录</button>
        </div>
      </div>`;
  }).join("");
}

function currentHighRiskCard() {
  const items = state.data.activityAttention || [];
  const risk = items.find((i) => i.attention === "high_risk_waiting");
  if (!risk) return emptyState("当前没有等待你许可的高风险操作", "有新的待发动作时会出现在这里。");
  return activityCard(risk);
}

function pendingRiskLine(action) {
  let payload = {};
  try { payload = typeof action.payload === "string" ? JSON.parse(action.payload || "{}") : (action.payload || {}); } catch {}
  const label = CAPABILITY_LABELS[action.capability]?.label || action.capability || "—";
  const irreversible = payload.irreversible === true ? "是" : payload.irreversible === false ? "否" : "—";
  return `能力：${label} · 不可逆：${irreversible} · 仅本次有效`;
}
function pendingActionsBlock() {
  return `<div id="pendingActionsList">${(state.data.pendingActions || []).map((action) => `
    <div class="card card-risk pending-action-card" data-pending-id="${esc(action.id)}">
      <div class="card-head"><p class="card-title" style="margin:0">${esc(action.title || "待许可操作")}</p><span class="card-id">${fmtDateTime(action.created_at)}</span></div>
      <p class="card-body">${esc(pendingRiskLine(action))}</p>
      <div class="card-actions">
        <button class="btn btn-safe" onclick="confirmPendingAction('${esc(action.id)}')">批准</button>
        <button class="btn btn-danger" onclick="rejectPendingAction('${esc(action.id)}')">拒绝</button>
      </div>
    </div>`).join("") || emptyState("当前没有等待你许可的高风险操作", "有新的待发动作时会出现在这里。")}</div>`;
}
function renderPendingActionsOnly() {
  const mount = document.getElementById("pendingActionsList");
  if (mount) mount.outerHTML = pendingActionsBlock();
  renderChrome();
}
async function refreshPendingActions() {
  try {
    const result = await api("/api/pending-actions");
    state.data.pendingActions = result?.actions || [];
    renderPendingActionsOnly();
  } catch {}
}
async function confirmPendingAction(id) {
  try { await api(`/api/pending-actions/${encodeURIComponent(id)}/confirm`, { method: "POST" }); await refreshPendingActions(); }
  catch (e) { toast(e.message); }
}
async function rejectPendingAction(id) {
  try { await api(`/api/pending-actions/${encodeURIComponent(id)}/reject`, { method: "POST" }); await refreshPendingActions(); }
  catch (e) { toast(e.message); }
}

function renderPermissions() {
  const matrix = state.data.permissionMatrix || {};
  const caps = matrix.capabilities || {};
  $("#view-permissions").innerHTML = `
    <div class="section-head">
      <div class="section-kicker"><span>04 / PERMISSION BOUNDARIES</span><span>${new Date().toLocaleDateString("zh-CN")} CST</span></div>
      <h1 class="section-title">信任能力，不交出边界。</h1>
      <p class="section-lede">授权总是带着能力、范围与有效期。受信 Agent 也不能绕过任何高风险的逐次许可。</p>
      <hr class="section-divider">
    </div>
    ${permissionLegend()}
    <div class="block-head"><h2>等待你许可</h2><span class="hint">仅本次有效</span></div>
    ${pendingActionsBlock()}
    <div class="block-head"><h2>日常能力矩阵</h2><span class="hint">点击修改将生成权限修订记录</span></div>
    ${Object.keys(caps).length ? permissionMatrixTable(caps, DAILY_CAPS) : emptyState("权限矩阵尚无数据")}
    <div class="block-head"><h2>高风险操作 · 逐次许可，不可降级为自动</h2><span class="hint">始终可见</span></div>
    ${currentHighRiskCard()}
    ${Object.keys(caps).length ? highRiskTable(caps) : ""}
    <div class="block-head"><h2>受信 Agent 的授权边界</h2><span class="hint">单人空间 / 无协作权限</span></div>
    ${agentBoundaryCard()}
    <div class="block-head"><h2>远程配置许可 · 内嵌二次确认</h2>${unavailableTag("未接入")}</div>
    ${unbuiltPlaceholder("远程配置通道未接入", "设备侧 /api/devices/{id}/config 当前返回 501，界面保留结构，接入后在此展示二次确认流程。")}
    <p class="muted" style="margin-top:20px">门控优先级：禁止 ＞ 每次确认 ＞ 限定许可 ＞ 自动。网络恢复、模型切换与重试均不能复用已消费的高风险许可。</p>
  `;
}
async function setPermissionLevel(capability, level) {
  if (state.permissionPending.has(capability)) return;
  const current = state.data.permissionMatrix?.capabilities?.[capability]?.level;
  state.permissionPrevious[capability] = current;
  state.permissionPending.add(capability);
  delete state.permissionErrors[capability];
  renderPermissions();
  try {
    await api(`/api/permissions/matrix/${encodeURIComponent(capability)}`, { method: "PUT", body: JSON.stringify({ level }) });
    toast("权限修订已记录");
    await refresh();
    delete state.permissionPrevious[capability];
  } catch (e) {
    state.permissionErrors[capability] = e.message;
    toast(e.message);
  } finally {
    state.permissionPending.delete(capability);
    renderPermissions();
  }
}
async function revokeAgentGrants(subject) {
  const agents = (state.data.permissionAgents || []).filter((g) => g.subject === subject && !g.revoked_at);
  try {
    for (const g of agents) await api(`/api/permissions/grants/${encodeURIComponent(g.id)}/revoke`, { method: "POST" });
    toast("授权已撤销");
    await refresh();
  } catch (e) { toast(e.message); }
}

// ================= 02 Agent 编排 =================
function renderAgent() {
  const tasks = state.data.tasks || [];
  const active = tasks.find((t) => t.status === "pending" || t.status === "waiting_user" || t.status === "waiting_confirmation") || tasks[0];
  $("#view-agent").innerHTML = `
    <div class="section-head">
      <div class="section-kicker"><span>02 / EXECUTION CONTROL</span><span>${new Date().toLocaleDateString("zh-CN")} CST</span></div>
      <h1 class="section-title">看见每一步，保留最后决定权。</h1>
      <p class="section-lede">这里是执行计划、工具回执与异常处理，不是模拟的思维链。复杂子任务按需展开。</p>
      <hr class="section-divider">
    </div>
    ${active ? `
    <div class="card">
      <div class="card-head"><div class="card-head-left">${pill(taskStatusLabel(active.status), taskStatusTone(active.status))}</div><span class="card-id">TASK ${esc(active.id)}</span></div>
      <p class="card-title">${esc(active.title)}</p>
      <p class="card-body">来源：${esc(active.source || "—")} · ${fmtDateTime(active.run_at || active.created_at)}</p>
      <div class="card-actions">
        <button class="btn btn-safe" onclick="taskAction('${esc(active.id)}','pause')">暂停任务</button>
        <button class="btn btn-safe" onclick="taskAction('${esc(active.id)}','resume')">恢复任务</button>
        <button class="btn btn-danger" onclick="taskAction('${esc(active.id)}','cancel')">打断并取消</button>
      </div>
    </div>` : emptyState("当前没有正在执行的任务")}
    <div class="block-head"><h2>执行计划</h2><span class="hint">${unavailableTag("未接入")}</span></div>
    ${unbuiltPlaceholder("执行计划步骤表未接入", "GET /api/tasks/{id}/run 尚未实现（步骤、工具回执、幂等键）。此处先出结构，待后端补全后渲染真实步骤表。")}
    <div class="block-head"><h2>模型路由与降级</h2><span class="hint">降级不降低权限边界</span></div>
    ${routeSection()}
    <div class="block-head"><h2>工具与异常回执</h2><span class="hint">保留原始错误 · 不是静默重试</span></div>
    ${toolSchemasTable()}
    <div class="block-head"><h2>任务队列</h2><span class="hint">现有任务的真实数据</span></div>
    ${tasksTable(tasks)}
    <p class="muted" style="margin-top:16px">你随时可以停止，已发生的效果如实记录。本地草稿可修改、删除或撤销；外部消息送达不承诺可逆。系统只承诺取消待发、保留送达回执及记录后续更正，不伪造"全部撤销"。</p>
  `;
}
function taskStatusLabel(status) {
  return { pending: "等待许可", waiting: "已提醒", waiting_user: "等待许可", waiting_confirmation: "等待许可", done: "已完成", cancelled: "已取消", failed: "失败" }[status] || status;
}
function taskStatusTone(status) {
  return { pending: "danger", waiting_user: "danger", waiting_confirmation: "danger", done: "ok", cancelled: "neutral", failed: "danger" }[status] || "neutral";
}
function tasksTable(tasks) {
  return `
    <div class="table-wrap">
      <table class="table">
        <thead><tr><th>任务</th><th>状态</th><th>时间</th><th>来源</th></tr></thead>
        <tbody>${tasks.map((t) => `<tr><td><b>${esc(t.title)}</b><div class="muted">${esc(t.id)}</div></td><td>${pill(taskStatusLabel(t.status), taskStatusTone(t.status))}</td><td>${fmtDateTime(t.run_at || t.created_at)}</td><td>${esc(t.source || "—")}</td></tr>`).join("") || '<tr class="empty-row"><td colspan="4">还没有任务</td></tr>'}</tbody>
      </table>
    </div>`;
}
function toolSchemasTable() {
  const tools = state.data.tools || [];
  return `
    <div class="table-wrap">
      <table class="table">
        <thead><tr><th>工具</th><th>说明</th></tr></thead>
        <tbody>${tools.map((t) => `<tr><td class="id">${esc(t.name || t.id || "—")}</td><td>${esc(t.description || "—")}</td></tr>`).join("") || '<tr class="empty-row"><td colspan="2">暂无已注册工具</td></tr>'}</tbody>
      </table>
    </div>`;
}
async function taskAction(id, action) {
  try {
    await api(`/api/tasks/${encodeURIComponent(id)}/${action}`, { method: "POST" });
    toast("任务已更新");
    await refresh();
  } catch (e) { toast(e.message); }
}

// ================= 03 记忆库 =================
function memoryMetrics() {
  const memories = state.data.memories || [];
  const long = memories.filter((m) => m.layer === "long").length;
  const short = memories.filter((m) => m.layer === "short").length;
  const userProjects = (state.data.projects || []).filter((p) => p.origin !== "auto").length;
  const aiProjects = (state.data.projects || []).filter((p) => p.origin === "auto").length;
  return `
    <div class="metric-strip">
      <div class="metric-cell"><div class="metric-label">长期记忆</div><div class="metric-value">${long}</div><div class="metric-note">按周总结生成候选</div></div>
      <div class="metric-cell"><div class="metric-label">短期事件</div><div class="metric-value">${short}</div><div class="metric-note">保留 7 天后按策略清理</div></div>
      <div class="metric-cell"><div class="metric-label">用户项目</div><div class="metric-value">${String(userProjects).padStart(2, "0")}</div><div class="metric-note">你创建，AI 按授权协助</div></div>
      <div class="metric-cell"><div class="metric-label">AI 自建项目</div><div class="metric-value">${String(aiProjects).padStart(2, "0")}</div><div class="metric-note">隔离草案，不自动激活</div></div>
    </div>`;
}
function memoryTimeline() {
  const memories = state.data.memories || [];
  return `
    <div class="table-wrap">
      <table class="table">
        <thead><tr><th>时间 / 标识</th><th>摘要</th><th>重要度 / 标签</th><th>归属</th></tr></thead>
        <tbody>
          ${memories.map((m) => `
            <tr>
              <td class="mono">${fmtDateTime(m.created_at)}<br><span class="id">${esc(m.id)} · ${esc(m.layer || "short")}</span></td>
              <td>${esc(m.content)}</td>
              <td>${Number(m.importance ?? 0).toFixed(2)} / ${esc((safeTags(m.tags) || []).join("、") || "—")}</td>
              <td>${esc(m.project_id || "个人偏好")}</td>
            </tr>`).join("") || '<tr class="empty-row"><td colspan="4">还没有记忆</td></tr>'}
        </tbody>
      </table>
    </div>`;
}
function safeTags(raw) {
  if (Array.isArray(raw)) return raw;
  try { return JSON.parse(raw || "[]"); } catch { return []; }
}
function candidatesBlock() {
  const candidates = (state.data.memories || []).filter((m) => m.status === "candidate");
  if (!candidates.length) return emptyState("暂无候选记忆", "AI 自建项目目前没有待审阅的候选。");
  return candidates.map((c) => `
    <div class="card">
      <div class="card-head"><p class="card-title" style="margin:0">${esc(c.content)}</p>${pill("待审阅", "warn")}</div>
      <p class="card-body">置信度 ${fmtPct(c.confidence)} · 只收集候选摘要，不代你创建习惯</p>
      <div class="card-actions">
        <button class="btn btn-safe" onclick="candidateAction('${esc(c.id)}','accept')">审阅候选</button>
        <button class="btn btn-text" onclick="candidateAction('${esc(c.id)}','ignore')">忽略</button>
      </div>
    </div>`).join("");
}
function renderMemory() {
  $("#view-memory").innerHTML = `
    <div class="section-head">
      <div class="section-kicker"><span>03 / PERSONAL MEMORY</span><span>${new Date().toLocaleDateString("zh-CN")} CST</span></div>
      <h1 class="section-title">记得有出处，忘记有边界。</h1>
      <p class="section-lede">短期事件自动写入，长期记忆定期总结。每一条都能追到来源，由你编辑或忘记。</p>
      <hr class="section-divider">
    </div>
    ${memoryMetrics()}
    <div class="view-switch">
      <button class="${state.memoryView === "timeline" ? "active" : ""}" onclick="setMemoryView('timeline')">时间线</button>
      <button class="${state.memoryView === "projects" ? "active" : ""}" onclick="setMemoryView('projects')">项目视图</button>
    </div>
    <div class="block-head"><h2>AI 自建候选</h2><span class="hint">审阅候选 / 忽略</span></div>
    ${candidatesBlock()}
    <div class="block-head"><h2>记忆时间线</h2></div>
    ${memoryTimeline()}
    <div class="block-head"><h2>危险区域 · 与日常操作隔离</h2><span class="hint">二次确认已展开 · 尚未执行</span></div>
    ${destroyZone()}
  `;
}
function setMemoryView(view) { state.memoryView = view; renderMemory(); }
async function candidateAction(id, action) {
  try {
    await api(`/api/memories/candidates/${encodeURIComponent(id)}/${action}`, { method: "POST" });
    toast(action === "accept" ? "候选已确认为长期记忆" : "候选已忽略");
    await refresh();
  } catch (e) { toast(e.message); }
}
function destroyZone() {
  return `
    <details class="danger-zone" id="destroyDetails">
      <summary>销毁全部个人数据</summary>
      <p class="card-body">将停止 Agent，撤销尚未使用的授权，删除全部记忆与事件内容、原始输入、文件及个人配置。无法恢复；已送达的外部消息不会因此被删除。</p>
      <div class="destroy-step">
        <button class="btn btn-safe" onclick="exportBeforeDestroy()">① 导出需要保留的数据</button>
        <span class="muted" id="exportStatus">尚未导出</span>
      </div>
      <div class="destroy-step">
        <label class="confirm-checkbox"><input type="checkbox" id="destroyExportedCheck" disabled> 我已导出需要保留的数据，理解此操作不可恢复。</label>
        <input type="text" id="destroyPhrase" placeholder="输入：销毁全部">
      </div>
      <div class="card-actions">
        <button class="btn btn-safe" onclick="document.getElementById('destroyDetails').open=false">取消，保留数据</button>
        <button class="btn btn-danger" id="destroyConfirmBtn" disabled onclick="confirmDestroy()">${state.destroyStarted ? (state.destroyResult ? "已销毁" : "销毁中…") : "确认销毁全部数据"}</button>
        ${state.destroyError ? `<span class="inline-error">${esc(state.destroyError)}</span>` : state.destroyResult ? `<span class="muted">${esc(state.destroyResult)}</span>` : ""}
      </div>
    </details>`;
}
let exportToken = "";
async function exportBeforeDestroy() {
  try {
    const result = await api("/api/memories/export", { method: "POST" });
    exportToken = result.token;
    $("#exportStatus").textContent = `已导出 ${result.memories?.length ?? 0} 条 · 令牌 10 分钟内有效`;
    $("#destroyExportedCheck").disabled = false;
    toast("导出成功");
  } catch (e) { toast(e.message); }
}
function wireDestroyWatchers() {
  document.addEventListener("input", (e) => {
    if (e.target.id === "destroyPhrase" || e.target.id === "destroyExportedCheck") {
      const phrase = $("#destroyPhrase")?.value.trim();
      const checked = $("#destroyExportedCheck")?.checked;
      const btn = $("#destroyConfirmBtn");
      if (btn && !state.destroyStarted) btn.disabled = !(phrase === "销毁全部" && checked && exportToken);
    }
  });
}
async function confirmDestroy() {
  if (state.destroyStarted) return;
  state.destroyStarted = true;
  state.destroyError = "";
  const button = $("#destroyConfirmBtn");
  if (button) { button.disabled = true; button.textContent = "销毁中…"; }
  try {
    await api("/api/data/destroy", {
      method: "POST",
      body: JSON.stringify({ confirm_phrase: "销毁全部", exported: true, export_token: exportToken }),
    });
    toast("已销毁，执行回执不包含个人内容");
    state.destroyResult = "个人数据已销毁";
    exportToken = "";
    await refresh();
  } catch (e) {
    state.destroyStarted = false;
    state.destroyError = e.message;
    toast(e.message);
    renderMemory();
  }
}

// ================= 05 设备与模型 =================
function modelSettingsCard() {
  const s = state.data.modelSettings || {};
  const err = state.modelSettingsError;
  const fallbackNote = s.fallback ? "" : "（留空表示不降级）";
  const degraded = !!state.data.routes?.degraded;
  return `
    <div class="card">
      <div class="card-head">
        <p class="card-title" style="margin:0">模型配置</p>
        <span class="muted">保存后立即生效</span>
      </div>
      <dl class="kv-grid">
        <dt>主模型</dt><dd><input type="text" id="modelPrimary" value="${esc(s.primary || "")}" style="width:100%;max-width:320px"></dd>
        <dt>备用模型</dt><dd><input type="text" id="modelFallback" value="${esc(s.fallback || "")}" placeholder="留空表示不降级" style="width:100%;max-width:320px"></dd>
        <dt>超时(秒)</dt><dd><input type="text" id="modelTimeout" value="${esc(s.timeout_seconds ?? "")}" style="width:100px"></dd>
      </dl>
      <div class="card-actions">
        <button class="btn btn-safe" id="modelSaveBtn" ${state.busy.has("saveModelSettings") ? "disabled" : ""} onclick="saveModelSettings()">${state.busy.has("saveModelSettings") ? "保存中…" : "保存配置"}</button>
        <span class="inline-confirm fade" id="modelSaveConfirm"></span>
        <span class="muted">${degraded ? "当前：降级运行" : "当前：主路由正常"} · 备用模型${fallbackNote || "已配置"}</span>
      </div>
      ${err ? `<p class="card-footnote" style="color:var(--danger-fg);border-top:0;padding-top:0">${esc(err)}</p>` : ""}
    </div>`;
}
async function saveModelSettings() {
  if (state.busy.has("saveModelSettings")) return;
  const primary = $("#modelPrimary").value.trim();
  const fallback = $("#modelFallback").value.trim();
  const timeoutRaw = $("#modelTimeout").value.trim();
  const timeout_seconds = Number(timeoutRaw);
  state.busy.add("saveModelSettings");
  renderDevices();
  try {
    state.data.modelSettings = await api("/api/model-settings", {
      method: "PUT",
      body: JSON.stringify({ primary, fallback, timeout_seconds }),
    });
    state.modelSettingsError = "";
    state.busy.delete("saveModelSettings");
    try { state.data.routes = await api("/api/routes"); } catch {}
    renderDevices();
    inlineConfirm("modelSaveConfirm", "配置已保存");
  } catch (e) {
    state.busy.delete("saveModelSettings");
    state.modelSettingsError = e.message;
    renderDevices();
  }
}
function routeSection() {
  const routes = state.data.routes;
  if (!routes) return emptyState("路由信息不可用", "GET /api/routes 没有返回数据。");
  const rows = routes.routes || [];
  const body = rows.map((r) => `
    <tr>
      <td><b>${esc(r.id)}</b>${r.is_fallback ? '<div class="row-meta">备用</div>' : ""}</td>
      <td class="mono">${esc(r.model || "—")}</td>
      <td>${r.timeout_seconds != null ? `${r.timeout_seconds}s` : "—"}</td>
      <td>${(r.capabilities || []).map((c) => `<span class="badge">${esc(c)}</span>`).join(" ") || "—"}</td>
      <td>${r.state && r.state.degraded
        ? '<span class="badge red">降级</span>'
        : '<span class="badge green">正常</span>'}
        ${r.state && r.state.last_error ? `<div class="row-meta">${esc(r.state.last_error)}</div>` : ""}</td>
    </tr>`).join("");
  const note = rows.length < 2
    ? "未配置备用路由（FALLBACK_MODEL 为空），因此不会发生降级。这是刻意的：宁可不降级，也不假装有备份。"
    : `当前使用：${esc(routes.active || "—")}。降级只减少能力，不会放宽权限边界。`;
  return `
    <div class="table-wrap"><table class="table">
      <thead><tr><th>路由</th><th>模型</th><th>超时</th><th>能力</th><th>状态</th></tr></thead>
      <tbody>${body || '<tr class="empty-row"><td colspan="5">没有路由</td></tr>'}</tbody>
    </table></div>
    <p class="muted" style="margin-top:10px">${note}</p>`;
}

function deviceMetricsStrip(metrics) {
  const online = metrics?.online_ratio != null ? fmtPct(metrics.online_ratio, 1) : "—";
  const offlineMin = metrics?.offline_seconds != null ? Math.round(metrics.offline_seconds / 60) : null;
  const latency = metrics?.p50_ms != null ? `${Math.round(metrics.p50_ms)}ms` : "—";
  const latencyNote = metrics?.p95_ms != null
    ? `P95 ${Math.round(metrics.p95_ms)}ms · 服务端`
    : "暂无数据";
  const errorRate = metrics?.error_rate != null ? fmtPct(metrics.error_rate, 1) : "—";
  const errorNote = metrics?.samples ? `${metrics.samples} 个样本 · 含已恢复失败` : "暂无数据";
  const usage = state.data.usage || {};
  return `
    <div class="metric-strip cols-5">
      <div class="metric-cell"><div class="metric-label">24h 在线率</div><div class="metric-value">${online}</div><div class="metric-note">${offlineMin != null ? `离线累计 ${offlineMin} 分钟` : "暂无数据"}</div></div>
      <div class="metric-cell"><div class="metric-label">请求延迟</div><div class="metric-value">${latency}</div><div class="metric-note">${latencyNote}</div></div>
      <div class="metric-cell"><div class="metric-label">请求错误率</div><div class="metric-value">${errorRate}</div><div class="metric-note">${errorNote}</div></div>
      <div class="metric-cell"><div class="metric-label">今日 Token</div><div class="metric-value">${Number(usage.total_tokens || 0).toLocaleString()}</div><div class="metric-note">输入 ${Number(usage.input_tokens || 0).toLocaleString()} / 输出 ${Number(usage.output_tokens || 0).toLocaleString()}</div></div>
      <div class="metric-cell"><div class="metric-label">平均电流</div><div class="metric-value">—</div><div class="metric-note">${unavailableTag("设备未上报")}</div></div>
    </div>`;
}
function deviceCard(device, connected) {
  const battery = device.battery_level ?? null;
  const relative = relativeTime(device.last_seen);
  return `
    <div class="card">
      <div class="card-head">
        <div class="card-head-left"><p class="card-title" style="margin:0">${esc(device.name)}</p></div>
        ${pill(connected ? "在线" : "离线", connected ? "ok" : "neutral")}
      </div>
      <p class="card-body">${esc(device.platform || "—")} · 电量 ${battery == null ? "—" : battery + "%"} · ${esc(device.network_type || "—")}</p>
      <p class="muted">最后在线 ${fmtDateTime(device.last_seen)}${relativeSuffix(device.last_seen)}</p>
    </div>`;
}
function killSwitchControl() {
  const on = state.data.killSwitch?.on;
  return `<button class="btn btn-danger" onclick="toggleKillSwitch(${!on})">${on ? "恢复调度" : "紧急停止全部执行"}</button>`;
}
function renderDevices() {
  const devices = state.data.devices || [];
  const connected = new Set(state.data.connected || []);
  const metrics = state.data.deviceMetrics;
  $("#view-devices").innerHTML = `
    <div class="section-head">
      <div class="section-kicker"><span>05 / DEVICE &amp; MODEL OPERATIONS</span><span>${new Date().toLocaleDateString("zh-CN")} CST</span></div>
      <h1 class="section-title">系统状态，不应是一个黑箱。</h1>
      <p class="section-lede">连接、模型、工具与电量在同一张工作台。失败明确展示，远程动作先许可，再留下可核对的回执。</p>
      <hr class="section-divider">
    </div>
    <div class="block-head"><h2>紧急停止</h2><span class="hint">会中止调度、取消待发与远程队列，保留全部审计</span></div>
    ${killSwitchControl()}
    <div class="block-head" style="margin-top:28px"><h2>指标</h2></div>
    ${deviceMetricsStrip(metrics)}
    <div class="block-head"><h2>设备与连接</h2></div>
    ${devices.map((d) => deviceCard(d, connected.has(d.id))).join("") || emptyState("还没有注册设备")}
    <div class="block-head"><h2>多模型与语音路由</h2><span class="hint">${(state.data.routes?.degraded) ? "降级运行" : "主路由正常"}</span></div>
    ${modelSettingsCard()}
    ${routeSection()}
    <div class="block-head"><h2>远程配置与版本管理</h2><span class="hint">${unavailableTag("未接入")}</span></div>
    ${unbuiltPlaceholder("远程配置与 APK 升级未接入", "/api/devices/{id}/config 与 /api/devices/{id}/upgrade 当前均返回 501（设计已完成，未接入能力）。")}
    <div class="block-head"><h2>自迭代 / RCE / 原始调试</h2><span class="hint">${unavailableTag("默认禁止")}</span></div>
    ${unbuiltPlaceholder("自迭代与远程代码执行均未接入", "/api/rce/request 当前返回 501。界面按设计默认折叠，不出现可点击的启用入口。")}
  `;
}
async function toggleKillSwitch(on) {
  try {
    await api("/api/runtime/kill-switch", { method: "POST", body: JSON.stringify({ on }) });
    toast(on ? "已触发紧急停止" : "已恢复调度");
    await refresh();
  } catch (e) { toast(e.message); }
}

// ================= refresh / routing =================
function renderView(view) {
  if (view === "activity") renderActivity();
  if (view === "agent") renderAgent();
  if (view === "memory") renderMemory();
  if (view === "permissions") renderPermissions();
  if (view === "devices") renderDevices();
}
function switchView(view) {
  document.querySelectorAll(".tab-item").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  $("#tabSelect").value = view;
  if (view !== "activity") stopElapsedTimer(); // only the activity page owns the per-second job-elapsed ticker
  renderView(view);
}

let refreshInFlight = false;
async function refresh({ resetJobDetails = false } = {}) {
  if (refreshInFlight) return;
  refreshInFlight = true;
  if (resetJobDetails) state.jobDetails = {};
  try {
    const [activityAll, dashboard, tasks, memories, projects, deviceState, auditRecent, permissionMatrix, permissionAgents, killSwitch, tools, routes, modelSettings, artifacts, uploads] = await Promise.all([
      api("/api/activities?limit=100&include_ignored=true"),
      api("/api/dashboard"),
      api("/api/tasks"),
      api("/api/memories"),
      api("/api/projects"),
      api("/api/devices"),
      api("/api/audit?limit=20"),
      api("/api/permissions/matrix"),
      api("/api/permissions/agents"),
      api("/api/runtime/kill-switch"),
      api("/api/tools"),
      api("/api/routes"),
      api("/api/model-settings"),
      api("/api/artifacts?limit=10"),
      api("/api/uploads?limit=10"),
    ]);
    state.data.activityAll = activityAll?.items || [];
    state.data.activityAttention = state.data.activityAll.filter((i) => i.attention !== "ignored");
    state.data.pendingActions = dashboard?.pending_actions || [];
    state.data.usage = dashboard?.usage || {};
    state.data.tasks = tasks?.tasks || [];
    state.data.memories = memories?.memories || [];
    state.data.projects = projects?.projects || [];
    state.data.devices = deviceState?.devices || [];
    state.data.connected = deviceState?.connected || [];
    state.data.auditRecent = auditRecent?.records || [];
    state.data.permissionMatrix = permissionMatrix || {};
    state.data.permissionAgents = permissionAgents?.agents || [];
    state.data.killSwitch = killSwitch || {};
    state.data.tools = tools?.tools || [];
    state.data.routes = routes || null;
    state.data.modelSettings = modelSettings || {};
    state.data.artifacts = artifacts?.artifacts || [];
  state.data.uploads = uploads?.uploads || [];
    if (state.data.devices[0]) {
      try { state.data.deviceMetrics = await api(`/api/devices/${encodeURIComponent(state.data.devices[0].id)}/metrics`); }
      catch { state.data.deviceMetrics = null; }
    }
    await refreshJobs();
    state.refreshError = "";
    state.firstLoadDone = true;
    state.lastSyncedAt = Date.now();
    const active = document.querySelector(".tab-item.active");
    renderChrome();
    renderView(active ? active.dataset.view : "activity");
  } catch (e) {
    // R1: a failed first load keeps the skeleton, not an empty state — there is no "old data" to fall back to yet.
    state.refreshError = `同步失败：${e.message}`;
    const active = document.querySelector(".tab-item.active");
    if (active && (state.firstLoadDone || Object.keys(state.data).length)) { renderChrome(); renderView(active.dataset.view); }
  } finally {
    refreshInFlight = false;
  }
}

// ---------- wiring ----------
document.querySelectorAll(".tab-item").forEach((b) => (b.onclick = () => switchView(b.dataset.view)));
$("#tabSelect").onchange = (e) => switchView(e.target.value);
$("#settingsBtn").onclick = () => { $("#apiUrl").value = state.api; $("#apiToken").value = state.token; $("#settingsDialog").showModal(); };
$("#settingsClose").onclick = () => $("#settingsDialog").close();
$("#settingsForm").onsubmit = () => {
  state.api = $("#apiUrl").value.trim().replace(/\/$/, "") || location.origin;
  state.token = $("#apiToken").value.trim();
  sessionStorage.setItem("jarvis.api", state.api);
  sessionStorage.setItem("jarvis.token", state.token);
  state.refreshError = "";
  initStream();
  refresh();
};
$("#waitingPill").onclick = () => {
  if ($("#waitingPill").classList.contains("collapsed")) {
    state.pillSnoozedUntil = 0;
    $("#waitingPill").classList.remove("collapsed");
    return;
  }
  switchView("activity");
  const risk = (state.data.activityAttention || []).find((i) => i.attention === "high_risk_waiting");
  if (risk) { state.expanded.add(risk.id); renderActivity(); }
};
wireDestroyWatchers();
document.addEventListener("input", (e) => {
  if (e.target.id === "artifactCodeInput") e.target.value = e.target.value.toUpperCase();
  if (e.target.id === "jobGoalInput") {
    state.jobGoalInput = e.target.value;
    const btn = $("#jobStartBtn");
    if (btn) btn.disabled = !state.jobGoalInput.trim();
  }
});
document.addEventListener("keydown", (e) => {
  if (e.target.id === "jobGoalInput" && e.key === "Enter") { e.preventDefault(); createJob(); }
});

window.switchView = switchView;
window.setActivityView = setActivityView;
window.toggleEvidence = toggleEvidence;
window.toggleCompose = toggleCompose;
window.sendCompose = sendCompose;
window.runActivityAction = runActivityAction;
window.setPermissionLevel = setPermissionLevel;
window.revokeAgentGrants = revokeAgentGrants;
window.taskAction = taskAction;
window.setMemoryView = setMemoryView;
window.candidateAction = candidateAction;
window.exportBeforeDestroy = exportBeforeDestroy;
window.confirmDestroy = confirmDestroy;
window.toggleKillSwitch = toggleKillSwitch;
window.saveModelSettings = saveModelSettings;
window.lookupArtifactCode = lookupArtifactCode;
window.uploadMaterial = uploadMaterial;
window.copyArtifactLink = copyArtifactLink;
window.copyJobError = copyJobError;
window.createJob = createJob;
window.approveJob = approveJob;
window.cancelJob = cancelJob;
window.toggleJobSteps = toggleJobSteps;
window.confirmPendingAction = confirmPendingAction;
window.rejectPendingAction = rejectPendingAction;
window.toggleSection = toggleSection;

if (!state.token) {
  $("#settingsDialog").showModal();
} else {
  // Paint the skeleton before the first await, otherwise the loading state never
  // reaches the screen and the page looks empty (or broken) for a second.
  renderView("activity");
  initStream();
  refresh();
}
setInterval(() => { if (state.token && !document.hidden) refresh(); }, 10000);
window.addEventListener("beforeunload", closeStream);
