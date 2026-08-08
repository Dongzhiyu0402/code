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

function maskedBadge(it) {
  return it && it.masked ? '<span class="masked-badge">已脱敏</span>' : "";
}

function locationCell(it) {
  // v0.4.0: file:line as a clickable snippet link (falls back to plain file).
  const file = esc(it.file || "-");
  if (it.line != null && it.file) {
    return '<span class="line-link" data-snippet-root="' + esc(it.snippet_root || "") +
      '" data-snippet-file="' + esc(it.file) + '" data-snippet-line="' + it.line + '">' +
      file + ":" + it.line + "</span>" + maskedBadge(it);
  }
  return file + maskedBadge(it);
}

function constraintCell(it) {
  // v0.6.0: render consistency-constraint violations as badges + messages.
  const violations = (it && it.constraint_violations) || [];
  if (!violations.length) return "-";
  return violations.map((v) =>
    '<div class="cv"><span class="cv-id">' + esc(v.constraint_id || "?") + "</span>" +
    '<span class="cv-type">[' + esc(v.type || "?") + "]</span>" +
    esc(v.message || "") + "</div>"
  ).join("");
}

function itemRows(items) {
  if (!items || !items.length) {
    return '<tr><td colspan="9" class="muted">无漂移项</td></tr>';
  }
  return items.map((it) => {
    const sev = it.severity || "NONE";
    const where = it.key_path || "(file)";
    return (
      "<tr>" +
      '<td><span class="badge ' + sevClass(sev) + '">' + esc(sev) + "</span></td>" +
      "<td>" + esc(where) + "</td>" +
      "<td>" + esc(it.change_type) + "</td>" +
      "<td>" + locationCell(it) + "</td>" +
      "<td>" + esc(JSON.stringify(it.old_value)) + "</td>" +
      "<td>" + esc(JSON.stringify(it.new_value)) + "</td>" +
      "<td>" + esc(it.rule_id == null ? "-" : "#" + it.rule_id) + "</td>" +
      "<td>" + constraintCell(it) + "</td>" +
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
  const ds = data.daemon_status || {};
  const daemonCard =
    '<div class="card">' +
    '<h3 style="margin-bottom:8px">守护进程</h3>' +
    (ds.running
      ? '<p><span class="badge b-info">运行中</span> pid=' + esc(ds.pid) +
        (ds.info && ds.info.baseline ? " · baseline=" + esc(ds.info.baseline) : "") +
        (ds.info && ds.info.interval ? " · interval=" + esc(ds.info.interval) + "s" : "") +
        "</p>"
      : '<p><span class="badge b-none">未运行</span>' +
        (ds.stale ? ' <span class="muted">(残留 PID 已清理)</span>' : "") +
        (ds.error ? ' <span style="color:var(--critical)">' + esc(ds.error) + "</span>" : "") +
        "</p>") +
    (ds.last_scan
      ? '<p class="muted">最近守护扫描 #' + ds.last_scan.scan_id + " · " + fmtTime(ds.last_scan.created_at) + "</p>"
      : "") +
    // v0.11.0 (P0-1): error-rate row — rendered only while the daemon is
    // running with cycle records (zero-noise; omitted otherwise).
    (ds.running && ds.error_rate != null && ds.cycles_total != null
      ? '<p>错误率 ' + (ds.error_rate * 100).toFixed(1) + "% (" + ds.cycles_failed + "/" +
        ds.cycles_total + ')</p><p class="muted">最近 ' + ds.cycles_total +
        " 次周期：" + (ds.cycles_total - ds.cycles_failed) + " 次成功 / " +
        ds.cycles_failed + " 次失败</p>"
      : "") +
    "</div>";
  $("#view-overview").innerHTML =
    '<h2>概览</h2>' +
    daemonCard +
    // v0.10.0 (P0-1): zero-noise — the muted-rules card renders only when
    // at least one rule is currently muted.
    (data.muted_rules > 0
      ? '<div class="card"><p>当前静默规则 ' + data.muted_rules + " 条</p></div>"
      : "") +
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

// v0.9.0 (P0-1): timeline search/filter/pagination state.  Module-level so
// it survives view switches (acceptance: filters persist across views).
const timelineState = { q: "", severity: "", mode: "", page: 0 };
const TIMELINE_PAGE_SIZE = 20;

function sevOptions(selected) {
  return ["CRITICAL", "WARN", "INFO", "NONE"].map((s) =>
    '<option value="' + s + '"' + (selected === s ? " selected" : "") + ">" + s + "</option>"
  ).join("");
}

function timelineRow(sc) {
  const s = sc.summary || {};
  return (
    '<tr class="scan-row" data-scan="' + sc.scan_id + '" style="cursor:pointer" title="查看报告">' +
    "<td><strong>#" + sc.scan_id + "</strong></td>" +
    "<td>" + fmtTime(sc.created_at) + "</td>" +
    "<td>" + esc(sc.mode) + "</td>" +
    "<td>" + (sc.baseline ? esc(sc.baseline.name) + " v" + sc.baseline.version : "-") + "</td>" +
    '<td><span class="badge ' + sevClass(s.max_severity) + '">' + esc(s.max_severity) + "</span></td>" +
    "<td>total=" + s.total + " · +" + s.added + " −" + s.removed + " · mod " + s.modified + "</td>" +
    "</tr>"
  );
}

async function renderTimeline() {
  const params = new URLSearchParams({
    limit: TIMELINE_PAGE_SIZE,
    offset: timelineState.page * TIMELINE_PAGE_SIZE,
  });
  if (timelineState.q) params.set("q", timelineState.q);
  if (timelineState.severity) params.set("severity", timelineState.severity);
  if (timelineState.mode) params.set("mode", timelineState.mode);
  const data = await api("/api/scans?" + params.toString());
  const scans = data.scans || [];
  const total = data.total || 0;
  const pageCount = Math.max(1, Math.ceil(total / TIMELINE_PAGE_SIZE));
  const start = total ? timelineState.page * TIMELINE_PAGE_SIZE + 1 : 0;
  const end = Math.min(total, (timelineState.page + 1) * TIMELINE_PAGE_SIZE);

  $("#view-timeline").innerHTML =
    "<h2>时间线</h2>" +
    '<div class="card">' +
    '<div class="form-row">' +
    '<input id="tlQ" placeholder="搜索 #id / 基线名 / 模式" value="' + esc(timelineState.q) + '">' +
    '<select id="tlSev"><option value="">全部严重度</option>' + sevOptions(timelineState.severity) + "</select>" +
    '<select id="tlMode"><option value="">全部模式</option>' +
    '<option value="daemon"' + (timelineState.mode === "daemon" ? " selected" : "") + ">daemon</option>" +
    '<option value="watch"' + (timelineState.mode === "watch" ? " selected" : "") + ">watch</option>" +
    '<option value="manual"' + (timelineState.mode === "manual" ? " selected" : "") + ">manual</option>" +
    "</select>" +
    '<button class="action" id="tlApply">搜索</button>' +
    "</div>" +
    '<p class="muted">共 ' + total + " 次扫描 · 显示 " + start + "–" + end + "</p>" +
    '<table><thead><tr><th>ID</th><th>时间</th><th>模式</th><th>基线</th><th>严重度</th><th>漂移</th></tr></thead><tbody id="tlBody">' +
    (scans.length
      ? scans.map(timelineRow).join("")
      : '<tr><td colspan="6" class="muted">无匹配扫描</td></tr>') +
    "</tbody></table>" +
    '<div class="pager">' +
    '<button id="tlPrev" ' + (timelineState.page === 0 ? "disabled" : "") + ">上一页</button>" +
    "<span>第 " + (timelineState.page + 1) + " 页 / 共 " + pageCount + " 页</span>" +
    '<button id="tlNext" ' + ((timelineState.page + 1) * TIMELINE_PAGE_SIZE >= total ? "disabled" : "") + ">下一页</button>" +
    "</div></div>";

  const applyFilters = () => {
    timelineState.q = $("#tlQ").value.trim();
    timelineState.severity = $("#tlSev").value;
    timelineState.mode = $("#tlMode").value;
    timelineState.page = 0;
    renderTimeline();
  };
  $("#tlApply").addEventListener("click", applyFilters);
  $("#tlQ").addEventListener("keydown", (e) => {
    if (e.key === "Enter") applyFilters();
  });
  $("#tlPrev").addEventListener("click", () => {
    if (timelineState.page > 0) { timelineState.page -= 1; renderTimeline(); }
  });
  $("#tlNext").addEventListener("click", () => {
    if ((timelineState.page + 1) * TIMELINE_PAGE_SIZE < total) {
      timelineState.page += 1;
      renderTimeline();
    }
  });
  // v0.9.0 (P0-1): click a row -> jump to the report view (reportPreselect).
  document.querySelectorAll("#tlBody [data-scan]").forEach((row) => {
    row.addEventListener("click", () => {
      reportPreselect = Number(row.dataset.scan);
      switchView("reports");
    });
  });
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
      '<path data-sev="' + k + '" style="cursor:pointer" d="M' + cx + " " + cy + " L" + x1 + " " + y1 +
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

// v0.9.0 (P0-1/P0-4): one-shot jump target for the timeline row click, and
// the CSV export entry point.
let reportPreselect = null;
// v0.10.0 (P0-3): scan list cache so the compare panel reuses the timeline
// options without a second fetch.
let reportScans = [];

async function renderReports() {
  const data = await api("/api/overview");
  const scans = data.timeline || [];
  reportScans = scans;
  const options = scans.map((s) =>
    '<option value="' + s.scan_id + '">#' + s.scan_id + " " + fmtTime(s.created_at) + " (" + (s.baseline ? s.baseline.name : "no baseline") + ")</option>"
  ).join("");
  let selectId = null;
  if (reportPreselect !== null) {
    // The pre-selected scan may sit beyond the 50-item overview timeline;
    // append it as an option so the report still loads.
    if (!scans.some((s) => s.scan_id === reportPreselect)) {
      options += '<option value="' + reportPreselect + '">#' + reportPreselect + " (…)</option>";
    }
    selectId = reportPreselect;
  }
  $("#view-reports").innerHTML =
    "<h2>报告浏览</h2>" +
    '<div class="card form-row">' +
    '<label>扫描：<select id="reportScan">' + options + "</select></label>" +
    '<label>严重度：<select id="reportSev"><option value="">全部</option><option>CRITICAL</option><option>WARN</option><option>INFO</option><option>NONE</option></select></label>' +
    '<button class="action" id="reportLoad">加载</button>' +
    '<button class="action" id="reportExport">导出 HTML</button>' +
    '<button class="action" id="reportExportCsv">导出 CSV</button>' +
    // v0.10.0 (P0-3): report-to-report diff entry (two-scan compare panel).
    '<button class="action" id="reportCompare">对比</button>' +
    "</div>" +
    '<div class="card" id="reportBody"></div>' +
    '<div id="reportComparePanel"></div>';
  if (scans.length || reportPreselect !== null) {
    if (selectId !== null) $("#reportScan").value = selectId;
    $("#reportLoad").addEventListener("click", loadReport);
    $("#reportExport").addEventListener("click", exportReportHtml);
    $("#reportExportCsv").addEventListener("click", exportReportCsv);
    $("#reportCompare").addEventListener("click", openReportCompare);
    loadReport();
  }
  // One-shot: clear so re-entering the view never re-jumps.
  reportPreselect = null;
}

// v0.10.0 (P0-3): the compare panel reuses the timeline's scan options and
// calls the same diff function as `cfgdrift report --diff`.
function openReportCompare() {
  const panel = $("#reportComparePanel");
  const options = (reportScans || []).map((s) =>
    '<option value="' + s.scan_id + '">#' + s.scan_id + " " + fmtTime(s.created_at) + "</option>"
  ).join("");
  panel.innerHTML =
    '<div class="card form-row">' +
    '<label>扫描 A：<select id="cmpScanA">' + options + "</select></label>" +
    '<label>扫描 B：<select id="cmpScanB">' + options + "</select></label>" +
    '<button class="action" id="cmpRun">对比</button>' +
    "</div>" +
    '<div class="card" id="cmpDiffBody"><p class="muted">选择两个扫描后点击「对比」。</p></div>';
  if ((reportScans || []).length >= 2) {
    $("#cmpScanB").selectedIndex = 1;
  }
  $("#cmpRun").addEventListener("click", runReportCompare);
}

async function runReportCompare() {
  const base = $("#cmpScanA").value;
  const target = $("#cmpScanB").value;
  const body = $("#cmpDiffBody");
  body.innerHTML = '<p class="muted">对比中…</p>';
  try {
    const data = await api("/api/reports/compare?base_id=" + encodeURIComponent(base) +
      "&target_id=" + encodeURIComponent(target));
    body.innerHTML = renderDiffResult(data);
  } catch (e) {
    body.innerHTML = '<p style="color:var(--critical)">对比失败：' + esc(e.message) + "</p>";
  }
}

function renderDiffResult(diff) {
  const s = diff.summary || {};
  if (!s.total) return '<p class="muted">两次扫描无差异</p>';
  const group = (title, items) => {
    if (!items || !items.length) return "";
    return '<div style="margin-bottom:10px"><strong>' + title + "</strong>" +
      items.map((it) => {
        const sev = it.severity || "NONE";
        const where = it.key_path || "(file)";
        const loc = it.line != null && it.file ? esc(it.file) + ":" + it.line : esc(it.file || "-");
        return '<div class="cv">[<span class="badge ' + sevClass(sev) + '">' + esc(sev) + "</span>] " +
          esc(where) + " " + esc(JSON.stringify(it.new_value)) + " (" + loc + ")</div>";
      }).join("") + "</div>";
  };
  let html = group("新增（A 有 B 无，" + s.added + " 项）", diff.added);
  html += group("消失（B 有 A 无，" + s.removed + " 项）", diff.removed);
  if (diff.changed && diff.changed.length) {
    html += '<div style="margin-bottom:10px"><strong>变化（严重度/值变，' + s.changed + " 项）</strong>" +
      diff.changed.map((c) => {
        const a = c.item_a || {};
        const b = c.item_b || {};
        const sevA = a.severity || "NONE";
        const sevB = b.severity || "NONE";
        const where = a.key_path || b.key_path || "(file)";
        return '<div class="cv">[<span class="badge ' + sevClass(sevA) + '">' + esc(sevA) +
          "</span>→<span class=\"badge " + sevClass(sevB) + '">' + esc(sevB) + "</span>] " +
          esc(where) + " " + esc(JSON.stringify(a.new_value)) + " → " +
          esc(JSON.stringify(b.new_value)) + "</div>";
      }).join("") + "</div>";
  }
  return html;
}

async function exportReportCsv() {
  // v0.9.0 (P0-4): download the masked CSV export as a Blob (same renderer
  // as `cfgdrift report --csv`).
  const scanId = $("#reportScan").value;
  try {
    const res = await fetch("/api/reports/" + scanId + "/csv");
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      throw new Error((payload && payload.message) || ("HTTP " + res.status));
    }
    const text = await res.text();
    const blob = new Blob([text], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "report-" + scanId + ".csv";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert("导出失败：" + e.message);
  }
}

async function exportReportHtml() {
  // v0.5.0: fetch the standalone HTML report and download it as a Blob.
  const scanId = $("#reportScan").value;
  try {
    const res = await fetch("/api/reports/" + scanId + "/html");
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      throw new Error((payload && payload.message) || ("HTTP " + res.status));
    }
    const text = await res.text();
    const blob = new Blob([text], { type: "text/html;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "report-" + scanId + ".html";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert("导出失败：" + e.message);
  }
}

async function loadReport() {
  const scanId = $("#reportScan").value;
  const sev = $("#reportSev").value;
  const payload = await api("/api/reports/" + scanId);
  const data = payload.data;
  let items = (data.items || []).map((it) => {
    // Enrich items with the baseline scan root so the snippet modal can
    // validate against a known root.
    it.snippet_root = data.scan_root || "";
    return it;
  });
  if (sev) items = items.filter((it) => it.severity === sev);
  $("#reportBody").innerHTML =
    "<p class=\"muted\">#" + data.scan_id + " · " + fmtTime(data.created_at) + " · mode=" + data.mode +
    (data.baseline ? " · baseline=" + data.baseline.name + " v" + data.baseline.version : "") + "</p>" +
    '<table><thead><tr><th>严重度</th><th>键路径</th><th>类型</th><th>文件 / 行号</th><th>旧值</th><th>新值</th><th>规则</th><th>约束违反</th></tr></thead>' +
    "<tbody>" + itemRows(items) + "</tbody></table>";
}

async function renderBaselines() {
  const data = await api("/api/baselines");
  const rows = data.baselines || [];
  $("#view-baselines").innerHTML =
    "<h2>基线管理</h2>" +
    '<div class="card"><table><thead><tr><th>名称</th><th>版本</th><th>创建时间</th><th>扫描根</th><th>格式</th><th>说明</th><th>操作</th></tr></thead><tbody>' +
    (rows.length ? rows.map((b) =>
      "<tr><td><strong>" + esc(b.name) + "</strong></td><td>v" + b.version + "</td><td>" + fmtTime(b.created_at) +
      "</td><td>" + esc(b.scan_root) + "</td><td>" + esc(b.format) + "</td><td>" + esc(b.description) +
      '</td><td><button class="action" data-bvname="' + esc(b.name) + '">版本对比</button></td></tr>'
    ).join("") : '<tr><td colspan="7" class="muted">暂无基线</td></tr>') +
    "</tbody></table></div>" +
    '<div id="bvPanel"></div>';

  // v0.11.0 (P0-2): per-baseline version-compare entry -> compare panel.
  document.querySelectorAll("#view-baselines [data-bvname]").forEach((btn) => {
    btn.addEventListener("click", () => openBaselineVersionPanel(btn.dataset.bvname));
  });
}

async function openBaselineVersionPanel(name) {
  const data = await api("/api/baselines/" + encodeURIComponent(name) + "/versions");
  const versions = data.versions || [];
  if (versions.length < 2) {
    $("#bvPanel").innerHTML =
      '<div class="card"><p class="muted">基线 ' + esc(name) + " 仅 " + versions.length +
      " 个版本，需要至少两个版本才能对比。</p></div>";
    return;
  }
  const opts = versions.map((v) =>
    '<option value="' + v.version + '">v' + v.version + " " + fmtTime(v.created_at) + "</option>"
  ).join("");
  $("#bvPanel").innerHTML =
    '<div class="card" id="bvCard">' +
    '<h3 style="margin-bottom:10px">基线 ' + esc(name) + " 版本对比</h3>" +
    '<div class="form-row">' +
    '<label>版本 A：<select id="bvVa">' + opts + "</select></label>" +
    '<label>版本 B：<select id="bvVb">' + opts + "</select></label>" +
    '<button class="action" id="bvRun">对比</button>' +
    "</div>" +
    '<div id="bvBody"><p class="muted">选择两个版本后点击「对比」。</p></div>' +
    "</div>";
  const va = $("#bvVa");
  const vb = $("#bvVb");
  if (versions.length >= 2) {
    // Default: newest (A) vs previous (B).
    va.selectedIndex = versions.length - 1;
    vb.selectedIndex = versions.length - 2;
  }
  $("#bvRun").addEventListener("click", () => runBaselineVersionCompare(name));
}

async function runBaselineVersionCompare(name) {
  const va = $("#bvVa").value;
  const vb = $("#bvVb").value;
  const body = $("#bvBody");
  body.innerHTML = '<p class="muted">对比中…</p>';
  try {
    const data = await api(
      "/api/baselines/compare?name=" + encodeURIComponent(name) +
      "&va=" + encodeURIComponent(va) + "&vb=" + encodeURIComponent(vb)
    );
    renderBaselineVersionResult(body, data);
  } catch (e) {
    body.innerHTML = '<p style="color:var(--critical)">对比失败：' + esc(e.message) + "</p>";
  }
}

function renderBaselineVersionResult(body, data) {
  const total = (data.summary || {}).total || 0;
  const head =
    '<p class="muted">' + esc(data.name) + " · v" + data.version_a + " (A, " + fmtTime(data.created_at_a) +
    ") ↔ v" + data.version_b + " (B, " + fmtTime(data.created_at_b) + ")</p>" +
    '<div class="stat-row">' +
    stat("差异总数", total, "warn") +
    stat("新增", (data.added || []).length, "info") +
    stat("消失", (data.removed || []).length, "critical") +
    stat("变化", (data.changed || []).length, "warn") +
    "</div>";
  if (total === 0) {
    body.innerHTML = head + '<p class="muted">两版本无差异</p>';
    return;
  }
  const groupCard = (label, items) => {
    if (!items.length) return "";
    return '<div class="card"><h3 style="margin-bottom:10px">' + label + "（" + items.length + " 项）</h3>" +
      "<table><thead><tr><th>严重度</th><th>键路径</th><th>类型</th><th>文件 / 行号</th><th>旧值</th><th>新值</th></tr></thead>" +
      "<tbody>" + itemRows(items) + "</tbody></table></div>";
  };
  body.innerHTML =
    head +
    // Backend compare_baseline_versions(va, vb) diffs va as old / vb as new
    // (same compare_snapshots direction as the compare CLI): change_type
    // "added" = present in B (vb) but not in A (va), "removed" = present in
    // A but not in B.  Labels below follow that direction.
    groupCard("新增（B 有 A 无）", data.added || []) +
    groupCard("消失（A 有 B 无）", data.removed || []) +
    groupCard("变化（严重度/值变）", data.changed || []);
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
// Compare view (v0.5.0)
// ---------------------------------------------------------------------------

async function renderCompare() {
  // D7: environment options come from /api/baselines (baseline names are the
  // only guaranteed-resolvable environment names).
  const data = await api("/api/baselines");
  const baselines = data.baselines || [];
  const options = baselines.map((b) =>
    '<option value="' + esc(b.name) + '">' + esc(b.name) + " v" + b.version + "</option>"
  ).join("");
  $("#view-compare").innerHTML =
    "<h2>环境对比</h2>" +
    '<div class="card form-row">' +
    '<label>参考环境：<select id="cmpEnv1">' + options + "</select></label>" +
    '<label>对比环境：<select id="cmpEnv2">' + options + "</select></label>" +
    '<label>严重度：<select id="cmpSev"><option value="">全部</option><option>CRITICAL</option><option>WARN</option><option>INFO</option><option>NONE</option></select></label>' +
    '<button class="action" id="cmpRun">对比</button>' +
    "</div>" +
    '<div class="card" id="cmpBody"><p class="muted">选择两个环境后点击「对比」。</p></div>';
  if (baselines.length >= 2) {
    const sel2 = $("#cmpEnv2");
    sel2.selectedIndex = 1;
  }
  $("#cmpRun").addEventListener("click", runCompare);
  if (baselines.length >= 2) runCompare();
}

async function runCompare() {
  const env1 = $("#cmpEnv1").value;
  const env2 = $("#cmpEnv2").value;
  const sev = $("#cmpSev").value;
  const body = $("#cmpBody");
  body.innerHTML = '<p class="muted">对比中…</p>';
  try {
    const data = await api("/api/compare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // v0.9.0 (P0-3): use the default constraint library (built-in + user
      // constraints.yaml); the API also accepts explicit file paths.
      body: JSON.stringify({ env1, env2, constraints: [] }),
    });
    renderCompareResult(data, sev);
  } catch (e) {
    body.innerHTML = '<p style="color:var(--critical)">对比失败：' + esc(e.message) + "</p>";
  }
}

function constraintViolationCard(data) {
  // v0.9.0 (P0-3): render the「约束违反」card only when the response carries
  // the key (zero-noise: no violations -> no card, byte-identical to v0.8.0).
  const cv = data.constraint_violations;
  if (!cv) return "";
  const labels = {
    env_a: "env_a: " + (data.baseline_a || "?"),
    env_b: "env_b: " + (data.baseline_b || "?"),
  };
  const groups = [];
  for (const side of ["env_a", "env_b"]) {
    const violations = cv[side] || [];
    if (!violations.length) continue;
    const rows = violations.map((v) =>
      '<div class="cv">' +
      '<span class="cv-id">' + esc(v.constraint_id || "?") + "</span>" +
      '<span class="badge ' + sevClass(v.severity) + '">' + esc(v.severity || "WARN") + "</span> " +
      "<strong>" + esc((v.involved_keys || []).join(", ") || "-") + "</strong>" +
      (v.file ? ' <span class="muted">(' + esc(v.file) + ")</span>" : "") +
      '<div class="muted">' + esc(v.message || "") + "</div>" +
      "</div>"
    ).join("");
    groups.push(
      '<div style="margin-bottom:6px"><span class="muted">[' + esc(labels[side]) + "]</span></div>" + rows
    );
  }
  if (!groups.length) return "";
  return '<div class="card"><h3 style="margin-bottom:10px">约束违反</h3>' +
    groups.join('<hr style="border-color:var(--border)">') + "</div>";
}

function renderCompareResult(data, sev) {
  let items = (data.items || []).slice();
  if (sev) items = items.filter((it) => it.severity === sev);
  const s = data.summary || {};
  const dist = { CRITICAL: 0, WARN: 0, INFO: 0, NONE: 0 };
  (data.items || []).forEach((it) => {
    const k = it.severity || "NONE";
    dist[k] = (dist[k] || 0) + 1;
  });
  const peak = Math.max(1, ...Object.values(dist));
  const bars = ["CRITICAL", "WARN", "INFO", "NONE"].map((k) => {
    const n = dist[k] || 0;
    const color = { CRITICAL: "var(--critical)", WARN: "var(--warn)", INFO: "var(--info)", NONE: "var(--none)" }[k];
    return (
      '<div class="bar-row">' +
      '<div class="bar-label">' + k + "</div>" +
      '<div class="bar-track"><div class="bar-fill" style="width:' + (n / peak * 100) + "%;background:" + color + '"></div></div>' +
      '<div class="bar-num">' + n + "</div>" +
      "</div>"
    );
  }).join("");
  $("#cmpBody").innerHTML =
    '<p class="muted">' + esc(data.baseline_a) + " (v" + (data.env1_version == null ? "?" : data.env1_version) +
    ") → " + esc(data.baseline_b) + " (v" + (data.env2_version == null ? "?" : data.env2_version) + ")</p>" +
    '<div class="stat-row">' +
    stat("漂移总数", s.total || 0, "warn") +
    stat("新增", s.added || 0, "info") +
    stat("删除", s.removed || 0, "critical") +
    stat("修改", s.modified || 0, "warn") +
    stat("类型变化", s.type_changed || 0, "critical") +
    "</div>" +
    '<div class="card">' + bars + "</div>" +
    constraintViolationCard(data) +
    '<table><thead><tr><th>严重度</th><th>键路径</th><th>类型</th><th>文件 / 行号</th><th>旧值</th><th>新值</th><th>规则</th><th>约束违反</th></tr></thead>' +
    "<tbody>" + itemRows(items) + "</tbody></table>";
}

// ---------------------------------------------------------------------------
// Alerts view (v0.4.0)
// ---------------------------------------------------------------------------

let alertPage = 0;
const ALERT_PAGE_SIZE = 50;

// v0.10.0 (P0-1): mute helpers — 1h/24h buttons build an ISO timestamp via
// Date.toISOString() (trailing "Z" is tolerated by parse_iso_utc on the
// backend), and the muted badge shows the local deadline + remaining hours.
function isoPlusHours(hours) {
  return new Date(Date.now() + hours * 3600 * 1000).toISOString();
}

function fmtMuteUntil(until) {
  const d = new Date(until);
  if (isNaN(d.getTime())) return until;
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  const remH = Math.max(0, Math.ceil((d.getTime() - Date.now()) / 3600000));
  return mm + "-" + dd + " " + hh + ":" + mi + " · 剩余 " + remH + "h";
}

function isRuleMuted(r) {
  return r.mute_until && new Date(r.mute_until).getTime() > Date.now();
}

function muteCell(r) {
  if (isRuleMuted(r)) {
    return '<span class="muted-badge" title="' + esc(r.mute_until) + '">静默中 · 至 ' +
      fmtMuteUntil(r.mute_until) + "</span> " +
      '<button class="danger" data-alert-unmute="' + esc(r.name) + '">取消</button>';
  }
  return '<button class="action" data-alert-mute="' + esc(r.name) + '" data-hours="1">静默1h</button> ' +
    '<button class="action" data-alert-mute="' + esc(r.name) + '" data-hours="24">静默24h</button>';
}

// v0.10.0 (P0-2): fetch + embed the SVG trend into #trendSvg (same store
// aggregation as the events table, so chart and list can never disagree).
async function loadTrend(rule) {
  const params = new URLSearchParams({ days: 14 });
  if (rule) params.set("rule", rule);
  const data = await api("/api/alert-trend?" + params.toString());
  const svgEl = $("#trendSvg");
  if (svgEl) svgEl.innerHTML = data.svg;
  const totalEl = $("#trendTotal");
  if (totalEl) {
    totalEl.textContent = "近 14 天共 " + data.total + " 条" +
      (rule ? "（规则 " + rule + "）" : "");
  }
}

async function renderAlerts() {
  const [alertsData, eventsData] = await Promise.all([
    api("/api/alerts"),
    api("/api/alert-events?limit=" + ALERT_PAGE_SIZE + "&offset=" + alertPage * ALERT_PAGE_SIZE),
  ]);
  const alerts = alertsData.alerts || [];
  const events = eventsData.events || [];
  const total = eventsData.total || 0;

  const ruleTable =
    '<div class="card"><h3 style="margin-bottom:10px">告警规则（alerts.yaml）</h3>' +
    '<table><thead><tr><th>名称</th><th>类型</th><th>阈值</th><th>基线</th><th>状态</th><th>静默</th><th>操作</th></tr></thead><tbody>' +
    (alerts.length ? alerts.map((r) =>
      "<tr><td><strong>" + esc(r.name) + "</strong></td><td>" + esc(r.type) + "</td><td>" +
      '<span class="badge ' + sevClass(r.severity) + '">' + esc(r.severity) + "</span></td><td>" +
      esc(r.baseline || "all") + "</td><td>" + (r.enabled ? "启用" : "停用") +
      "</td><td>" + muteCell(r) +
      "</td><td>" +
      '<button class="action" data-alert-toggle="' + esc(r.name) + '" data-enable="' + (r.enabled ? "false" : "true") + '">' +
      (r.enabled ? "停用" : "启用") + "</button> " +
      '<button class="action" data-alert-test="' + esc(r.name) + '">测试发送</button>' +
      '<span class="alert-feedback"></span>' +
      "</td></tr>"
    ).join("") : '<tr><td colspan="7" class="muted">暂无告警规则（使用 <code>cfgdrift alert add</code> 添加）</td></tr>') +
    "</tbody></table></div>";

  // v0.10.0 (P0-2): trend card above the events table (rule dropdown mirrors
  // the alerts list; empty data renders the SVG empty-state, never an error).
  const trendCard =
    '<div class="card" id="trendCard">' +
    '<h3 style="margin-bottom:10px">近 14 天告警趋势</h3>' +
    '<div class="form-row">' +
    '<label>规则：<select id="trendRule"><option value="">全部规则</option>' +
    alerts.map((r) => '<option value="' + esc(r.name) + '">' + esc(r.name) + "</option>").join("") +
    "</select></label>" +
    '<span class="muted" id="trendTotal"></span>' +
    "</div>" +
    '<div id="trendSvg"></div>' +
    "</div>";

  const eventTable =
    '<div class="card"><h3 style="margin-bottom:10px">告警事件（最近 ' + ALERT_PAGE_SIZE + " 条 / 共 " + total + " 条）</h3>" +
    '<div class="form-row">' +
    '<input id="alertFilterRule" placeholder="规则名筛选" value="' + esc(currentAlertFilter.rule || "") + '">' +
    '<select id="alertFilterStatus"><option value="">全部状态</option><option value="sent"' + (currentAlertFilter.status === "sent" ? " selected" : "") + '>sent</option><option value="failed"' + (currentAlertFilter.status === "failed" ? " selected" : "") + '>failed</option></select>' +
    '<select id="alertFilterSeverity"><option value="">全部严重度</option><option>CRITICAL</option><option>WARN</option><option>INFO</option><option>NONE</option></select>' +
    '<button class="action" id="alertFilterApply">筛选</button>' +
    "</div>" +
    '<table><thead><tr><th>ID</th><th>规则</th><th>基线</th><th>严重度</th><th>状态</th><th>目标</th><th>次数</th><th>时间</th><th>错误</th><th>操作</th></tr></thead><tbody>' +
    (events.length ? events.map((ev) => {
      const statusBadge = ev.status === "sent"
        ? '<span class="badge b-info">sent</span>'
        : '<span class="badge b-critical">failed</span>';
      // v0.9.0 (P0-2): events produced by a retry carry the「重试」badge and
      // failed rows get a one-click retry button.
      const retryBadge = Number(ev.retried) === 1 ? ' <span class="badge b-warn">重试</span>' : "";
      const retryBtn = ev.status === "failed"
        ? '<button class="danger" data-retry="' + ev.id + '">重试</button>'
        : "";
      // v0.10.0 (P0-1): event-level ack — display-only, persists via
      // POST /api/alert-events/{id}/ack; acked rows show ✓已确认.
      const ackCell = Number(ev.acked) === 1
        ? '<span class="badge b-info">✓已确认</span>'
        : '<button class="action" data-ack="' + ev.id + '">ack</button>';
      return (
        "<tr><td>#" + ev.id + "</td><td>" + esc(ev.rule) + "</td><td>" + esc(ev.baseline) + "</td><td>" +
        '<span class="badge ' + sevClass(ev.severity) + '">' + esc(ev.severity) + "</span></td><td>" +
        statusBadge + retryBadge +
        "</td><td>" + esc(ev.target || "-") + "</td><td>" + ev.attempts + "</td><td>" + fmtTime(ev.created_at) + "</td><td>" +
        (ev.error ? '<span class="event-error">' + esc(ev.error) + "</span>" : "-") +
        "</td><td>" + ackCell + " " + retryBtn + "</td></tr>"
      );
    }).join("") : '<tr><td colspan="10" class="muted">暂无告警事件</td></tr>') +
    "</tbody></table>" +
    '<div class="pager">' +
    '<button id="alertPrev" ' + (alertPage === 0 ? "disabled" : "") + ">上一页</button>" +
    "<span>第 " + (alertPage + 1) + " 页</span>" +
    '<button id="alertNext" ' + ((alertPage + 1) * ALERT_PAGE_SIZE >= total ? "disabled" : "") + ">下一页</button>" +
    "</div></div>";

  $("#view-alerts").innerHTML = "<h2>告警管理</h2>" + ruleTable + trendCard + eventTable;

  // v0.10.0 (P0-2): trend rule dropdown -> reload the SVG (all rules default).
  $("#trendRule").addEventListener("change", () => {
    loadTrend($("#trendRule").value).catch(() => { /* keep dashboard alive */ });
  });
  loadTrend("").catch(() => { /* keep dashboard alive */ });

  $("#alertFilterApply").addEventListener("click", () => {
    currentAlertFilter.rule = $("#alertFilterRule").value.trim();
    currentAlertFilter.status = $("#alertFilterStatus").value;
    currentAlertFilter.severity = $("#alertFilterSeverity").value;
    alertPage = 0;
    renderAlerts();
  });
  $("#alertPrev").addEventListener("click", () => {
    if (alertPage > 0) { alertPage -= 1; renderAlerts(); }
  });
  $("#alertNext").addEventListener("click", () => {
    if ((alertPage + 1) * ALERT_PAGE_SIZE < total) { alertPage += 1; renderAlerts(); }
  });

  // v0.9.0 (P0-2): enable/disable toggle (PUT) — persists to alerts.yaml.
  document.querySelectorAll("#view-alerts [data-alert-toggle]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api("/api/alerts/" + encodeURIComponent(btn.dataset.alertToggle) + "/enabled", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: btn.dataset.enable === "true" }),
        });
        renderAlerts();
      } catch (e) {
        alert("切换失败：" + e.message);
      }
    });
  });
  // v0.9.0 (P0-2): connectivity test (POST) — instant feedback, no event row.
  document.querySelectorAll("#view-alerts [data-alert-test]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const fb = btn.parentNode.querySelector(".alert-feedback");
      if (fb) { fb.textContent = "发送中…"; fb.style.color = "var(--muted)"; }
      try {
        const data = await api("/api/alerts/" + encodeURIComponent(btn.dataset.alertTest) + "/test", { method: "POST" });
        if (fb) {
          fb.textContent = data.sent ? "已发送 cfgdrift.test" : ("失败：" + (data.error || "unknown"));
          fb.style.color = data.sent ? "var(--info)" : "var(--critical)";
        }
      } catch (e) {
        if (fb) { fb.textContent = "测试失败：" + e.message; fb.style.color = "var(--critical)"; }
      }
    });
  });
  // v0.9.0 (P0-2): retry a failed event (POST) — creates a new retried row.
  document.querySelectorAll("#view-alerts [data-retry]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api("/api/alert-events/" + btn.dataset.retry + "/retry", { method: "POST" });
        renderAlerts();
      } catch (e) {
        alert("重试失败：" + e.message);
      }
    });
  });
  // v0.10.0 (P0-1): mute 1h/24h (PUT) and unmute (DELETE) — alerts.yaml is
  // written through the same path as `cfgdrift alert mute/unmute`.
  document.querySelectorAll("#view-alerts [data-alert-mute]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const until = isoPlusHours(Number(btn.dataset.hours));
      try {
        await api("/api/alerts/" + encodeURIComponent(btn.dataset.alertMute) + "/mute", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ until }),
        });
        renderAlerts();
      } catch (e) {
        alert("静默失败：" + e.message);
      }
    });
  });
  document.querySelectorAll("#view-alerts [data-alert-unmute]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api("/api/alerts/" + encodeURIComponent(btn.dataset.alertUnmute) + "/mute", {
          method: "DELETE",
        });
        renderAlerts();
      } catch (e) {
        alert("取消失败：" + e.message);
      }
    });
  });
  // v0.10.0 (P0-1): event-level ack (POST) — display-only, persisted.
  document.querySelectorAll("#view-alerts [data-ack]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api("/api/alert-events/" + btn.dataset.ack + "/ack", { method: "POST" });
        renderAlerts();
      } catch (e) {
        alert("ack 失败：" + e.message);
      }
    });
  });
}

const currentAlertFilter = { rule: "", status: "", severity: "" };

// ---------------------------------------------------------------------------
// Line-snippet modal (v0.4.0)
// ---------------------------------------------------------------------------

async function openSnippet(root, file, line) {
  const url = "/api/file-snippet?root=" + encodeURIComponent(root) +
    "&file=" + encodeURIComponent(file) + "&line=" + encodeURIComponent(line);
  const data = await api(url);
  const rows = (data.snippet || []).map((r) =>
    '<span class="src-line' + (r.line === data.line ? " hl" : "") + '">' + r.line + "</span>" +
    esc(r.text)
  ).join("\n");
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML =
    '<div class="modal">' +
    '<div class="modal-head"><h3>' + esc(file) + ":" + data.line + "</h3>" +
    '<button class="modal-close">&times;</button></div>' +
    '<div class="modal-body"><pre>' + rows + "</pre></div></div>";
  document.body.appendChild(overlay);
  overlay.querySelector(".modal-close").addEventListener("click", () => overlay.remove());
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) overlay.remove();
  });
}

// ---------------------------------------------------------------------------
// Constraints view (v0.7.0, C-09 / C-10)
// ---------------------------------------------------------------------------

let cvPage = 0;
const CV_PAGE_SIZE = 50;

async function renderConstraints() {
  const [data, candData] = await Promise.all([
    api("/api/constraints"),
    api("/api/constraint-candidates"),
  ]);
  const constraints = data.constraints || [];
  const candidates = candData.candidates || [];

  // v0.11.0 (P0-3): mined-candidate card above the constraint table —
  // empty state with a mining guide when no candidates exist; promoted ones
  // show a ✓已转正 badge with the promote button disabled.
  const candRows = candidates.map((c) => {
    const m = c.metrics || {};
    const keys = (c.constraint && c.constraint.keys || []).join(", ") || "-";
    const examples = c.constraint && c.constraint.allowed
      ? (c.constraint.allowed.slice(0, 3).map((v) => JSON.stringify(v)).join(", "))
      : (c.constraint && c.constraint.min != null && c.constraint.max != null
          ? "[" + c.constraint.min + ", " + c.constraint.max + "]"
          : "-");
    const json = JSON.stringify(c.constraint);
    const promoted = c.status === "promoted";
    const actionCell = promoted
      ? '<span class="badge b-info">✓已转正</span>'
      : '<button class="action" data-promote="' + esc(c.id) + '">转正</button>';
    return (
      '<div class="cv">' +
      '<span class="cv-id">' + esc(c.id) + "</span> " +
      '<span class="badge b-info">' + esc(c.kind) + "</span> " +
      '<span class="muted">support=' + (m.support != null ? m.support : "-") +
      " · confidence=" + (m.confidence != null ? m.confidence : "-") + "</span>" +
      '<div class="muted">keys: ' + esc(keys) + "</div>" +
      '<div class="muted">示例: ' + esc(examples) + "</div>" +
      actionCell + " " +
      '<button class="action" data-copyrule="' + esc(c.id) + '" data-rule="' +
      esc(json).replace(/"/g, "&quot;") + '">复制命令</button>' +
      "</div>"
    );
  }).join("");
  const candCard =
    '<div class="card"><h3 style="margin-bottom:10px">挖掘候选（mined_candidates.yaml）</h3>' +
    (candidates.length
      ? candRows
      : '<p class="muted">暂无候选 · 运行 <code>cfgdrift constraint mine</code> 生成</p>') +
    "</div>";

  const table =
    '<div class="card"><h3 style="margin-bottom:10px">一致性约束（生效视角）</h3>' +
    '<table><thead><tr><th>ID</th><th>类型</th><th>键</th><th>严重度</th><th>来源</th><th>状态</th><th>操作</th></tr></thead><tbody>' +
    (constraints.length ? constraints.map((c) => {
      const isUser = c.source === "user";
      const toggleBtn = isUser
        ? '<button class="action" data-cid="' + esc(c.id) + '" data-enabled="' + (c.enabled ? "false" : "true") + '">' +
          (c.enabled ? "禁用" : "启用") + "</button>"
        : '<span class="muted">内置</span>';
      return (
        "<tr><td><strong>" + esc(c.id) + "</strong></td><td>" + esc(c.type) + "</td><td>" +
        esc((c.keys || []).join(", ") || "-") + "</td><td>" +
        '<span class="badge ' + sevClass(c.severity) + '">' + esc(c.severity) + "</span></td><td>" +
        esc(c.source) + "</td><td>" + (c.enabled ? "启用" : "停用") +
        "</td><td>" + toggleBtn + "</td></tr>"
      );
    }).join("") : '<tr><td colspan="7" class="muted">暂无约束</td></tr>') +
    "</tbody></table></div>";

  $("#view-constraints").innerHTML =
    "<h2>约束</h2>" + candCard + table + '<div id="cvEvents"></div>';

  document.querySelectorAll("#view-constraints [data-cid]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api("/api/constraints/" + encodeURIComponent(btn.dataset.cid) + "/enabled", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: btn.dataset.enabled === "true" }),
        });
        renderConstraints();
      } catch (e) {
        alert("切换失败：" + e.message);
      }
    });
  });

  // v0.11.0 (P0-3): promote a candidate (confirm dialog) then refresh so the
  // constraint table shows the new rule and the candidate card drops it.
  document.querySelectorAll("#view-constraints [data-promote]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!window.confirm("确认将该候选写入 constraints.yaml ？（默认停用，可在约束列表启用）")) return;
      try {
        await api("/api/constraint-candidates/" + encodeURIComponent(btn.dataset.promote) + "/promote", {
          method: "POST",
        });
        renderConstraints();
      } catch (e) {
        alert("转正失败：" + e.message);
      }
    });
  });

  // v0.11.0 (P0-3): copy a legal `cfgdrift constraint add --rule '<json>'`
  // command to the clipboard as a CLI-side path.
  document.querySelectorAll("#view-constraints [data-copyrule]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const cmd = "cfgdrift constraint add --rule '" + btn.dataset.rule + "'";
      try {
        await navigator.clipboard.writeText(cmd);
      } catch (e) {
        // clipboard may be unavailable in plain http contexts; fall back.
        window.prompt("复制以下命令：", cmd);
      }
    });
  });

  await renderConstraintEvents();
}

async function renderConstraintEvents() {
  const data = await api(
    "/api/constraint-events?limit=" + CV_PAGE_SIZE + "&offset=" + cvPage * CV_PAGE_SIZE
  );
  const events = data.events || [];
  const total = data.total || 0;

  $("#cvEvents").innerHTML =
    '<div class="card"><h3 style="margin-bottom:10px">约束违反（最近 ' + CV_PAGE_SIZE + " 条 / 共 " + total + " 条）</h3>" +
    '<table><thead><tr><th>ID</th><th>约束</th><th>类型</th><th>严重度</th><th>文件</th><th>键</th><th>消息</th><th>时间</th></tr></thead><tbody>' +
    (events.length ? events.map((ev) =>
      "<tr><td>#" + ev.id + "</td><td>" + esc(ev.constraint_id) + "</td><td>" + esc(ev.kind) + "</td><td>" +
      '<span class="badge ' + sevClass(ev.severity) + '">' + esc(ev.severity) + "</span></td><td>" +
      esc(ev.file || "-") + "</td><td>" + esc((ev.keys || []).join(", ") || "-") + "</td><td>" +
      esc(ev.detail || "-") + "</td><td>" + fmtTime(ev.created_at) + "</td></tr>"
    ).join("") : '<tr><td colspan="8" class="muted">暂无约束违反</td></tr>') +
    "</tbody></table>" +
    '<div class="pager">' +
    '<button id="cvPrev" ' + (cvPage === 0 ? "disabled" : "") + ">上一页</button>" +
    "<span>第 " + (cvPage + 1) + " 页</span>" +
    '<button id="cvNext" ' + ((cvPage + 1) * CV_PAGE_SIZE >= total ? "disabled" : "") + ">下一页</button>" +
    "</div></div>";

  $("#cvPrev").addEventListener("click", () => {
    if (cvPage > 0) { cvPage -= 1; renderConstraintEvents(); }
  });
  $("#cvNext").addEventListener("click", () => {
    if ((cvPage + 1) * CV_PAGE_SIZE < total) { cvPage += 1; renderConstraintEvents(); }
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
  compare: renderCompare,
  baselines: renderBaselines,
  rules: renderRules,
  alerts: renderAlerts,
  constraints: renderConstraints,
};

let currentView = "overview";

function switchView(name) {
  currentView = name;
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

// Delegate snippet clicks (event delegation survives re-renders).
document.addEventListener("click", (e) => {
  // v0.11.0 (P0-4): severity-pie slice click -> jump to the timeline with a
  // preset severity filter (same slice again cancels the filter).
  const slice = e.target.closest("#severitySvg [data-sev]");
  if (slice) {
    const sev = slice.dataset.sev;
    timelineState.severity = (timelineState.severity === sev ? "" : sev);
    timelineState.page = 0;
    switchView("timeline");
    return;
  }
  const link = e.target.closest(".line-link");
  if (link) {
    openSnippet(
      link.dataset.snippetRoot || "",
      link.dataset.snippetFile || "",
      link.dataset.snippetLine || "1"
    ).catch((err) => alert("无法加载代码片段：" + err.message));
  }
});

// v0.4.0: poll the overview (daemon status card) every 30s while visible.
setInterval(() => {
  if (currentView === "overview") {
    renderOverview().catch(() => { /* keep the dashboard alive */ });
  }
}, 30000);

switchView("overview");
