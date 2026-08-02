/* cfgdrift dashboard front-end: zero external dependencies. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

async function api(url, options) {
  const res = await fetch(url, options);
  const payload = await res.json().catch(() => ({ code: 2, data: null, message: "invalid response" }));
  if (!res.ok || payload.code !== 0) {
    throw new Error(payload.message || ("HTTP " + res.status));
  }
  return payload.data;
}

function sevClass(sev) {
  return "b-" + String(sev || "NONE").toLowerCase();
}

function fmtTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function itemRows(items) {
  if (!items || !items.length) {
    return '<tr><td colspan="7" class="muted">无漂移项</td></tr>';
  }
  return items.map((it) => {
    const sev = it.severity || "NONE";
    const where = it.key_path || "(file)";
    return (
      "<tr>" +
      '<td><span class="badge ' + sevClass(sev) + '">' + esc(sev) + "</span></td>" +
      "<td>" + esc(where) + "</td>" +
      "<td>" + esc(it.change_type) + "</td>" +
      "<td>" + esc(it.file) + "</td>" +
      "<td>" + esc(JSON.stringify(it.old_value)) + "</td>" +
      "<td>" + esc(JSON.stringify(it.new_value)) + "</td>" +
      "<td>" + esc(it.rule_id == null ? "-" : "#" + it.rule_id) + "</td>" +
      "</tr>"
    );
  }).join("");
}

// ---------------------------------------------------------------------------
// Views
// ---------------------------------------------------------------------------

async function renderOverview() {
  const data = await api("/api/overview");
  const latest = data.latest_scan;
  const s = latest ? latest.summary : { added: 0, removed: 0, modified: 0, type_changed: 0, ignored: 0, total: 0, max_severity: "NONE" };
  const t = data.totals || {};
  $("#view-overview").innerHTML =
    '<h2>概览</h2>' +
    '<div class="card">' +
    "<p class=\"muted\">共 " + data.scan_count + " 次扫描 · " + data.baseline_count + " 个基线</p>" +
    "<p>最近扫描 #" + (latest ? latest.scan_id : "-") +
    " · " + fmtTime(latest ? latest.created_at : null) +
    ' · <span class="badge ' + sevClass(s.max_severity) + '">' + esc(s.max_severity) + "</span></p>" +
    "</div>" +
    '<div class="stat-row">' +
    stat("累计漂移", t.total || 0, "muted") +
    stat("新增", t.added || 0, "info") +
    stat("删除", t.removed || 0, "critical") +
    stat("修改", t.modified || 0, "warn") +
    stat("类型变化", t.type_changed || 0, "critical") +
    stat("忽略", t.ignored || 0, "none") +
    "</div>";
}

function stat(label, num, cls) {
  return '<div class="stat"><div class="num ' + cls + '">' + num + '</div><div class="label">' + label + "</div></div>";
}

async function renderTimeline() {
  const data = await api("/api/overview");
  const scans = data.timeline || [];
  $("#view-timeline").innerHTML =
    "<h2>时间线</h2>" +
    (scans.length
      ? '<div class="card" id="timeline">' +
        scans.map((sc) => {
          const s = sc.summary;
          return (
            '<div class="scan-item" style="padding:10px 0;border-bottom:1px solid var(--border)">' +
            '<div><strong>#' + sc.scan_id + "</strong> " + fmtTime(sc.created_at) +
            ' <span class="muted">' + esc(sc.mode) + "</span>" +
            (sc.baseline ? ' <span class="muted">vs ' + esc(sc.baseline.name) + " v" + sc.baseline.version + "</span>" : "") +
            "</div>" +
            '<div><span class="badge ' + sevClass(s.max_severity) + '">' + esc(s.max_severity) + "</span> " +
            "total=" + s.total + " · added=" + s.added + " · removed=" + s.removed +
            " · modified=" + s.modified + " · ignored=" + s.ignored + "</div>" +
            "</div>"
          );
        }).join("") + "</div>"
      : '<div class="card muted">暂无扫描记录，请先运行 <code>cfgdrift scan</code>。</div>');
}

async function renderSeverity() {
  const data = await api("/api/overview");
  const dist = data.severity_distribution || { CRITICAL: 0, WARN: 0, INFO: 0, NONE: 0 };
  const order = ["CRITICAL", "WARN", "INFO", "NONE"];
  const max = Math.max(1, ...order.map((k) => dist[k] || 0));
  const bars = order.map((k) => {
    const n = dist[k] || 0;
    const color = { CRITICAL: "var(--critical)", WARN: "var(--warn)", INFO: "var(--info)", NONE: "var(--none)" }[k];
    return (
      '<div class="bar-row">' +
      '<div class="bar-label">' + k + "</div>" +
      '<div class="bar-track"><div class="bar-fill" style="width:' + (n / max * 100) + "%;background:" + color + '"></div></div>' +
      '<div class="bar-num">' + n + "</div>" +
      "</div>"
    );
  }).join("");

  const svg = renderSvgPie(dist);
  $("#view-severity").innerHTML =
    "<h2>严重度分布</h2>" +
    '<div class="card">' + bars + "</div>" +
    '<div class="card"><p class="muted">按扫描最高严重度统计（最近 50 次）</p>' + svg + "</div>";
}

function renderSvgPie(dist) {
  const order = ["CRITICAL", "WARN", "INFO", "NONE"];
  const colors = { CRITICAL: "#ef4444", WARN: "#f59e0b", INFO: "#22c55e", NONE: "#64748b" };
  const total = order.reduce((a, k) => a + (dist[k] || 0), 0);
  if (total === 0) return '<p class="muted">暂无数据</p>';
  let parts = "";
  let angle = -90;
  const cx = 120, cy = 120, r = 100;
  for (const k of order) {
    const frac = (dist[k] || 0) / total;
    const a2 = angle + frac * 360;
    const x1 = cx + r * Math.cos((angle * Math.PI) / 180);
    const y1 = cy + r * Math.sin((angle * Math.PI) / 180);
    const x2 = cx + r * Math.cos((a2 * Math.PI) / 180);
    const y2 = cy + r * Math.sin((a2 * Math.PI) / 180);
    const large = frac > 0.5 ? 1 : 0;
    parts +=
      '<path d="M' + cx + " " + cy + " L" + x1 + " " + y1 +
      ' A' + r + " " + r + " 0 " + large + " 1 " + x2 + " " + y2 + " Z\" fill=\"" +
      colors[k] + '" opacity="0.85"><title>' + k + ": " + (dist[k] || 0) + "</title></path>";
    angle = a2;
  }
  return (
    '<svg id="severitySvg" viewBox="0 0 240 240">' + parts +
    '<text x="120" y="116" text-anchor="middle" fill="#e2e8f0" font-size="22" font-weight="700">' + total + "</text>" +
    '<text x="120" y="136" text-anchor="middle" fill="#94a3b8" font-size="11">scans</text></svg>'
  );
}

async function renderReports() {
  const data = await api("/api/overview");
  const scans = data.timeline || [];
  const options = scans.map((s) =>
    '<option value="' + s.scan_id + '">#' + s.scan_id + " " + fmtTime(s.created_at) + " (" + (s.baseline ? s.baseline.name : "no baseline") + ")</option>"
  ).join("");
  $("#view-reports").innerHTML =
    "<h2>报告浏览</h2>" +
    '<div class="card form-row">' +
    '<label>扫描：<select id="reportScan">' + options + "</select></label>" +
    '<label>严重度：<select id="reportSev"><option value="">全部</option><option>CRITICAL</option><option>WARN</option><option>INFO</option><option>NONE</option></select></label>' +
    '<button class="action" id="reportLoad">加载</button>' +
    "</div>" +
    '<div class="card" id="reportBody"></div>';
  if (scans.length) {
    $("#reportLoad").addEventListener("click", loadReport);
    loadReport();
  }
}

async function loadReport() {
  const scanId = $("#reportScan").value;
  const sev = $("#reportSev").value;
  const payload = await api("/api/reports/" + scanId);
  const data = payload.data;
  let items = data.items || [];
  if (sev) items = items.filter((it) => it.severity === sev);
  $("#reportBody").innerHTML =
    "<p class=\"muted\">#" + data.scan_id + " · " + fmtTime(data.created_at) + " · mode=" + data.mode +
    (data.baseline ? " · baseline=" + data.baseline.name + " v" + data.baseline.version : "") + "</p>" +
    '<table><thead><tr><th>严重度</th><th>键路径</th><th>类型</th><th>文件</th><th>旧值</th><th>新值</th><th>规则</th></tr></thead>' +
    "<tbody>" + itemRows(items) + "</tbody></table>";
}

async function renderBaselines() {
  const data = await api("/api/baselines");
  const rows = data.baselines || [];
  $("#view-baselines").innerHTML =
    "<h2>基线管理</h2>" +
    '<div class="card"><table><thead><tr><th>名称</th><th>版本</th><th>创建时间</th><th>扫描根</th><th>格式</th><th>说明</th></tr></thead><tbody>' +
    (rows.length ? rows.map((b) =>
      "<tr><td><strong>" + esc(b.name) + "</strong></td><td>v" + b.version + "</td><td>" + fmtTime(b.created_at) +
      "</td><td>" + esc(b.scan_root) + "</td><td>" + esc(b.format) + "</td><td>" + esc(b.description) + "</td></tr>"
    ).join("") : '<tr><td colspan="6" class="muted">暂无基线</td></tr>') +
    "</tbody></table></div>";
}

async function renderRules() {
  const data = await api("/api/rules");
  const rules = data.rules || [];
  $("#view-rules").innerHTML =
    "<h2>忽略规则</h2>" +
    '<div class="card"><h3 style="margin-bottom:10px">新增规则</h3>' +
    '<div class="form-row">' +
    '<input id="ruleName" placeholder="规则名">' +
    '<input id="ruleKey" placeholder="键路径 / 正则">' +
    '<select id="ruleMatch"><option value="path_exact">path_exact</option><option value="path_prefix">path_prefix</option><option value="regex">regex</option></select>' +
    '<input id="ruleFile" placeholder="文件正则（可选）">' +
    '<select id="ruleChange"><option value="">变更类型（可选）</option><option value="added">added</option><option value="removed">removed</option><option value="modified">modified</option><option value="type_changed">type_changed</option></select>' +
    '<button class="action" id="ruleAdd">添加</button>' +
    "</div></div>" +
    '<div class="card"><table><thead><tr><th>ID</th><th>名称</th><th>键模式</th><th>匹配方式</th><th>文件模式</th><th>变更类型</th><th></th></tr></thead><tbody id="rulesBody">' +
    (rules.length ? rules.map((r) =>
      "<tr><td>#" + r.id + "</td><td>" + esc(r.name) + "</td><td>" + esc(r.key_pattern) + "</td><td>" + esc(r.match_type) +
      "</td><td>" + esc(r.file_pattern || "-") + "</td><td>" + esc(r.change_type || "-") +
      '</td><td><button class="danger" data-rid="' + r.id + '">删除</button></td></tr>'
    ).join("") : '<tr><td colspan="7" class="muted">暂无规则</td></tr>') +
    "</tbody></table></div>";

  $("#ruleAdd").addEventListener("click", async () => {
    const name = $("#ruleName").value.trim();
    const key = $("#ruleKey").value.trim();
    if (!name || !key) { alert("请填写规则名与键模式"); return; }
    const body = {
      name,
      key_pattern: key,
      match_type: $("#ruleMatch").value,
      file_pattern: $("#ruleFile").value.trim() || null,
      change_type: $("#ruleChange").value || null,
    };
    await api("/api/rules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    renderRules();
  });

  document.querySelectorAll("#rulesBody [data-rid]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api("/api/rules/" + btn.dataset.rid, { method: "DELETE" });
      renderRules();
    });
  });
}

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

const VIEWS = {
  overview: renderOverview,
  timeline: renderTimeline,
  severity: renderSeverity,
  reports: renderReports,
  baselines: renderBaselines,
  rules: renderRules,
};

function switchView(name) {
  document.querySelectorAll("nav button").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === name);
  });
  Object.keys(VIEWS).forEach((k) => {
    $("#view-" + k).classList.toggle("hidden", k !== name);
  });
  const fn = VIEWS[name];
  fn().catch((e) => {
    $("#view-" + name).innerHTML =
      "<h2>" + name + "</h2><div class=\"card\"><span style=\"color:var(--critical)\">加载失败：" + esc(e.message) + "</span></div>";
  });
}

document.querySelectorAll("nav button").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

switchView("overview");
