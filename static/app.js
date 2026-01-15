function $(id) {
  return document.getElementById(id);
}

function fmtPct(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${Number(v).toFixed(2)}%`;
}

function fmtX(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${Number(v).toFixed(2)}×`;
}

function logoUrlForTicker(ticker) {
  const t = String(ticker || "").trim().toUpperCase();
  if (!t) return null;
  // FMP serves ticker logos from a stable static URL. No API key required.
  return `https://financialmodelingprep.com/image-stock/${encodeURIComponent(t)}.png`;
}

function setTickerLogo(ticker) {
  const img = $("tickerLogo");
  if (!img) return;
  const src = logoUrlForTicker(ticker);
  if (!src) {
    img.classList.add("hidden");
    img.removeAttribute("src");
    img.removeAttribute("alt");
    return;
  }
  img.src = src;
  img.alt = `${String(ticker || "").toUpperCase()} logo`;
  img.classList.remove("hidden");
  img.onerror = () => img.classList.add("hidden");
}

function fmtNum(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(2) : "—";
}

function fmtMoney(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  const sign = n > 0 ? "-" : ""; // losses are positive; display as -$ for trader intuition
  const abs = Math.abs(n);
  return `${sign}$${abs.toFixed(2)}`;
}

function fmtSignedPct(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}%`;
}

function fmt0(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(0) : "—";
}

function downloadBlob(filename, blob) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function toCsv(rows) {
  const esc = (v) => {
    const s = String(v ?? "");
    if (s.includes(",") || s.includes('"') || s.includes("\n")) return `"${s.replaceAll('"', '""')}"`;
    return s;
  };
  if (!rows || !rows.length) return "";
  const cols = Object.keys(rows[0]);
  const head = cols.join(",");
  const body = rows.map(r => cols.map(c => esc(r[c])).join(",")).join("\n");
  return `${head}\n${body}\n`;
}

function _mdTable(headers, rows) {
  const esc = (v) => String(v ?? "").replaceAll("\n", " ").replaceAll("|", "\\|");
  const head = `| ${headers.map(esc).join(" | ")} |`;
  const bar = `| ${headers.map(() => "---").join(" | ")} |`;
  const body = (rows || []).map((r) => `| ${headers.map((h) => esc(r[h])).join(" | ")} |`).join("\n");
  return [head, bar, body].filter(Boolean).join("\n");
}

function buildEngine1OnePageMarkdown({ sanitizedPayload, riskChecks, levels, uiState }) {
  const p = sanitizedPayload || {};
  const t = String(p?.ticker || "—").toUpperCase();
  const tech = p?.technicals || {};
  const asOf = String(tech?.asOfDate || p?.regime?.asOfDate || "—").slice(0, 10);
  const px = tech?.narrative?.priceUsed ?? tech?.livePrice ?? tech?.lastDailyClose;
  const pxTxt = Number.isFinite(Number(px)) ? Number(px).toFixed(2) : "—";

  const summary = p?.summary || {};
  const baseline = p?.baseline || {};
  const regime = p?.regime || {};
  const quarters = p?.quarters || {};
  const events = Array.isArray(p?.events) ? p.events : [];

  const kv = [];
  const add = (k, v) => kv.push({ key: k, value: (v === null || v === undefined || v === "" ? "—" : String(v)) });

  add("engine1.ticker", t);
  add("engine1.asOfDate", asOf);
  add("engine1.priceUsed", pxTxt);
  add("engine1.ui.k", uiState?.k);
  add("engine1.ui.n", uiState?.n);
  add("engine1.ui.years", uiState?.years);

  add("engine1.summary.breach_rate_pct", summary?.breach_rate_pct);
  add("engine1.summary.avg_above_breach_pct", summary?.avg_above_breach_pct);
  add("engine1.summary.events_used", summary?.events_used);
  add("engine1.summary.events_found", summary?.events_found);
  add("engine1.baseline.avg_ratio_realized_to_implied", baseline?.avg_ratio_realized_to_implied);

  add("engine1.regime.label", regime?.label);
  add("engine1.regime.tailMultiplier", regime?.tailMultiplier);
  add("engine1.regime.tradeGate", regime?.guidance?.tradeGate);

  // Levels highlights
  const heat = levels?.levels?.gexHeatmap || null;
  const hm = heat?.metrics || {};
  const hs = heat?.stability || {};
  add("engine1.levels.gexHeatmap.enabled", heat?.enabled);
  add("engine1.levels.gexHeatmap.stability.label", hs?.label);
  add("engine1.levels.gexHeatmap.metrics.downsideDistancePts", hm?.downsideDistancePts);
  add("engine1.levels.gexHeatmap.metrics.upsideDistancePts", hm?.upsideDistancePts);
  add("engine1.levels.gexHeatmap.metrics.downsideDistanceEm", hm?.downsideDistanceEm);
  add("engine1.levels.gexHeatmap.metrics.upsideDistanceEm", hm?.upsideDistanceEm);

  const lines = [];
  lines.push(`# ${t} — Engine 1 (One Page)`);
  lines.push("");
  lines.push(`If you need a number, reference it by the **Key** in the Key/Value index below (stable keys).`);
  lines.push("");
  lines.push("## Key/Value index");
  lines.push(_mdTable(["key", "value"], kv));
  lines.push("");

  lines.push("## Risk checks (sanitized; no verdict/state)");
  lines.push("```json");
  lines.push(JSON.stringify(riskChecks || {}, null, 2));
  lines.push("```");
  lines.push("");

  lines.push("## Quarter seasonality");
  const qRows = ["Q1", "Q2", "Q3", "Q4"].map((q) => {
    const r = quarters?.[q] || {};
    const s = r?.seasonality || {};
    return {
      quarter: q,
      recommendation: r?.recommendation ?? "",
      breach_delta_pp: s?.breach_delta_pp ?? "",
      ratio_delta: s?.ratio_delta ?? "",
      overshoot_delta_pp: s?.overshoot_delta_pp ?? "",
      avg_ratio_realized_to_implied: r?.avg_ratio_realized_to_implied ?? "",
      max_ratio_realized_to_implied: r?.max_ratio_realized_to_implied ?? "",
    };
  });
  lines.push(_mdTable(Object.keys(qRows[0]), qRows));
  lines.push("");

  lines.push("## Earnings Events (all rows; advanced columns included)");
  if (events.length) {
    const eRows = events.map((e) => ({
      earnDate: e?.earnDate ?? "",
      anncTod: e?.anncTod ?? "",
      timing: e?.timing ?? "",
      pricingDateUsed: e?.pricingDateUsed ?? "",
      impErnMv: e?.impErnMv ?? "",
      impliedMovePct: e?.impliedMovePct ?? "",
      closeDateUsed: e?.closeDateUsed ?? "",
      closePx: e?.closePx ?? "",
      openDateUsed: e?.openDateUsed ?? "",
      openPx: e?.openPx ?? "",
      realizedMovePct: e?.realizedMovePct ?? "",
      signedMovePct: e?.signedMovePct ?? "",
      breachSide: e?.breachSide ?? "",
      dirOvershootPct: (e?.upOvershootPct ?? e?.downOvershootPct) ?? "",
      breach: e?.breach ?? "",
      regime: e?.regimeAtEvent?.label ?? "",
      gate: e?.regimeAtEvent?.tradeGate ?? "",
      aboveBreachPct: e?.aboveBreachPct ?? "",
    }));
    lines.push(_mdTable(Object.keys(eRows[0]), eRows));
  } else {
    lines.push("_No rows._");
  }
  lines.push("");

  return lines.join("\n");
}

function _scrubVerdictText(s) {
  // Remove explicit decisioning words without destroying surrounding content.
  // Use word boundaries to avoid mangling tickers like GOOG.
  let t = String(s ?? "");
  const reps = [
    [/GO\/NO-?GO/gi, ""],
    [/\bNO-?GO\b/gi, ""],
    [/\bGO\b/gi, ""],
    [/\bPASS\b/gi, ""],
    [/\bFAIL\b/gi, ""],
    [/\bMISSING\b/gi, ""],
    [/All checks passed/gi, ""],
    [/One or more checks failed\s*\/\s*missing/gi, ""],
  ];
  for (const [re, r] of reps) t = t.replace(re, r);
  t = t.replace(/\s{2,}/g, " ").replace(/\s+([,.;:])/g, "$1").trim();
  return t;
}

function sanitizeRiskChecks(go) {
  const g = (go && typeof go === "object") ? go : {};
  const checks = Array.isArray(g.checks) ? g.checks : [];
  const warns = Array.isArray(g.warnings) ? g.warnings : [];

  const cleanChecks = checks.map((c) => {
    const v = (c && typeof c.value === "object") ? c.value : (c?.value ?? null);
    const notes = Array.isArray(v?.notes) ? v.notes.map(_scrubVerdictText).filter(Boolean) : [];
    const out = {
      id: c?.id ?? null,
      label: c?.label ?? null,
      code: c?.code ?? null,
      // Intentionally omit: state (PASS/FAIL/MISSING)
      explain: _scrubVerdictText(c?.explain ?? ""),
      value: (v && typeof v === "object") ? { ...v, notes } : v,
    };
    // Ensure state is removed even if present.
    if (out.value && typeof out.value === "object") delete out.value.state;
    return out;
  });

  const cleanWarnings = warns.map((w) => ({
    id: w?.id ?? null,
    label: _scrubVerdictText(w?.label ?? w?.id ?? ""),
    // omit any state-like markers
  }));

  return {
    checks: cleanChecks,
    warnings: cleanWarnings,
    notes: Array.isArray(g?.notes) ? g.notes.map(_scrubVerdictText).filter(Boolean) : [],
  };
}

function sanitizeEngine1Payload(payload) {
  // Remove goNoGo entirely from exported Engine1 JSON.
  if (!payload || typeof payload !== "object") return payload;
  const out = JSON.parse(JSON.stringify(payload));
  delete out.goNoGo;
  return out;
}

function _engine1ExportFileNameBase(payload) {
  const t = String(payload?.ticker || "TICKER").toUpperCase();
  const asOf = String(payload?.technicals?.asOfDate || payload?.regime?.asOfDate || "").slice(0, 10) || "asof";
  return `engine1-export-${t}-${asOf}`;
}

async function _ensureEngine1LevelsPayload(ticker) {
  if (typeof lastEngine1LevelsPayload !== "undefined" && lastEngine1LevelsPayload) return lastEngine1LevelsPayload;
  const t = String(ticker || lastEngine1Ticker || $("ticker")?.value || "").trim().toUpperCase();
  if (!t) return null;
  const v = engine1GammaState?.view || "weekly";
  const url =
    `/api/levels?ticker=${encodeURIComponent(t)}`
    + `&view=${encodeURIComponent(v)}`
    + `&points=90&window_days=180&include_heatmap=1`
    + `&heatmap_view=${encodeURIComponent(engine1GexState?.view || "composite")}`
    + `&heatmap_mode=${encodeURIComponent(engine1GexState?.mode || "slope")}`
    + `&slope_window=5&flip_adjacent_n=5`;
  try {
    const p = await fetchJson(url);
    lastEngine1LevelsPayload = p;
    return p;
  } catch {
    return null;
  }
}

function buildEngine1SnapshotMarkdown({ payload, levels, uiState, riskChecks }) {
  const t = String(payload?.ticker || "—").toUpperCase();
  const tech = payload?.technicals || {};
  const asOf = String(tech?.asOfDate || payload?.regime?.asOfDate || "—").slice(0, 10);
  const px = tech?.narrative?.priceUsed ?? tech?.livePrice ?? tech?.lastDailyClose;
  const pxTxt = Number.isFinite(Number(px)) ? Number(px).toFixed(2) : "—";

  const summary = payload?.summary || {};
  const baseline = payload?.baseline || {};
  const regime = payload?.regime || {};
  const quarters = payload?.quarters || {};
  const events = Array.isArray(payload?.events) ? payload.events : [];

  const lines = [];
  lines.push(`# ${t} — Engine 1 Export`);
  lines.push("");
  lines.push(`- generatedAt: ${new Date().toISOString()}`);
  lines.push(`- asOf: ${asOf}`);
  lines.push(`- priceUsed: ${pxTxt}`);
  lines.push(`- url: ${String(uiState?.url || "")}`);
  lines.push("");

  lines.push("## UI state");
  lines.push("```json");
  lines.push(JSON.stringify(uiState || {}, null, 2));
  lines.push("```");
  lines.push("");

  lines.push("## Summary");
  lines.push(`- breach_rate_pct: ${summary?.breach_rate_pct ?? "—"}`);
  lines.push(`- avg_above_breach_pct: ${summary?.avg_above_breach_pct ?? "—"}`);
  lines.push(`- events_used: ${summary?.events_used ?? "—"} (found ${summary?.events_found ?? "—"})`);
  lines.push("");

  lines.push("## Baseline");
  lines.push(`- avg_ratio_realized_to_implied: ${baseline?.avg_ratio_realized_to_implied ?? "—"}`);
  lines.push("");

  lines.push("## Regime");
  if (regime?.label) lines.push(`- label: ${regime.label}`);
  if (regime?.guidance) lines.push(`- guidance: ${JSON.stringify(regime.guidance)}`);
  lines.push("");

  lines.push("## Risk checks (sanitized)");
  lines.push("```json");
  lines.push(JSON.stringify(riskChecks || {}, null, 2));
  lines.push("```");
  lines.push("");

  lines.push("## Quarter seasonality");
  const qRows = ["Q1", "Q2", "Q3", "Q4"].map((q) => {
    const r = quarters?.[q] || {};
    const s = r?.seasonality || {};
    return {
      quarter: q,
      recommendation: r?.recommendation ?? "",
      breach_delta_pp: s?.breach_delta_pp ?? "",
      ratio_delta: s?.ratio_delta ?? "",
      overshoot_delta_pp: s?.overshoot_delta_pp ?? "",
      avg_ratio_realized_to_implied: r?.avg_ratio_realized_to_implied ?? "",
      max_ratio_realized_to_implied: r?.max_ratio_realized_to_implied ?? "",
    };
  });
  lines.push(_mdTable(Object.keys(qRows[0]), qRows));
  lines.push("");

  lines.push("## Earnings Events (all rows; advanced columns included)");
  if (events.length) {
    const eRows = events.map((e) => ({
      earnDate: e?.earnDate ?? "",
      anncTod: e?.anncTod ?? "",
      timing: e?.timing ?? "",
      pricingDateUsed: e?.pricingDateUsed ?? "",
      impliedMovePct: e?.impliedMovePct ?? "",
      realizedMovePct: e?.realizedMovePct ?? "",
      signedMovePct: e?.signedMovePct ?? "",
      breachSide: e?.breachSide ?? "",
      dirOvershootPct: (e?.upOvershootPct ?? e?.downOvershootPct) ?? "",
      breach: e?.breach ?? "",
      regime: e?.regimeAtEvent?.label ?? "",
      gate: e?.regimeAtEvent?.tradeGate ?? "",
      aboveBreachPct: e?.aboveBreachPct ?? "",
    }));
    lines.push(_mdTable(Object.keys(eRows[0]), eRows));
  } else {
    lines.push("_No events rows._");
  }
  lines.push("");

  lines.push("## Levels payload summary");
  if (levels) {
    const heat = levels?.levels?.gexHeatmap || null;
    const keep = {
      schemaVersion: levels?.schemaVersion,
      ticker: levels?.ticker,
      priceSeriesPoints: Array.isArray(levels?.priceSeries) ? levels.priceSeries.length : 0,
      view: levels?.levels?.view,
      symbolUsed: levels?.levels?.symbolUsed,
      expiry: levels?.levels?.expiry,
      spot: levels?.levels?.spot,
      gammaFlipStrike: levels?.levels?.gammaFlipStrike,
      heatmap: heat ? { enabled: heat.enabled, metrics: heat.metrics, stability: heat.stability, boundaries: heat.boundaries } : null,
      warnings: levels?.levels?.warnings,
      notes: levels?.levels?.notes,
    };
    lines.push("```json");
    lines.push(JSON.stringify(keep, null, 2));
    lines.push("```");
  } else {
    lines.push("_Levels payload unavailable._");
  }
  lines.push("");

  return lines.join("\n");
}

async function exportEngine1LLMBundle() {
  const status = $("status");
  const payload = lastPayload;
  if (!payload) {
    if (status) status.textContent = "Export: run Engine 1 first (no payload yet).";
    return;
  }
  const t = String(payload?.ticker || $("ticker")?.value || "").trim().toUpperCase();

  try {
    if (status) status.textContent = "Exporting…";
    const levels = await _ensureEngine1LevelsPayload(t);

    const uiState = {
      engine: "engine1",
      url: String(window.location?.href || ""),
      ticker: t,
      k: String($("k")?.value || ""),
      n: 20,
      years: 5,
      showAdvancedCols: true, // export always includes advanced columns
      earningsExpanded: true, // export includes all rows
      gammaView: String(engine1GammaState?.view || ""),
      gammaLayers: { ...(engine1GammaState?.layers || {}) },
      heatmapView: String(engine1GexState?.view || ""),
      heatmapMode: String(engine1GexState?.mode || ""),
    };

    const riskChecks = sanitizeRiskChecks(payload?.goNoGo || null);
    const sanitizedPayload = sanitizeEngine1Payload(payload);
    const base = _engine1ExportFileNameBase(payload);

    const zip = window.ZipStore ? new window.ZipStore() : null;
    if (!zip) throw new Error("ZIP module missing (ZipStore not loaded).");

    const md = buildEngine1SnapshotMarkdown({ payload: sanitizedPayload, levels, uiState, riskChecks });
    zip.addText("snapshot.md", md);
    const onePageMd = buildEngine1OnePageMarkdown({ sanitizedPayload, riskChecks, levels, uiState });
    zip.addText("one_page.md", onePageMd);
    zip.addText("payload.engine1.json", JSON.stringify(sanitizedPayload, null, 2));
    zip.addText("risk_checks.json", JSON.stringify(riskChecks, null, 2));
    zip.addText("ui_state.json", JSON.stringify(uiState, null, 2));
    if (levels) zip.addText("payload.levels.json", JSON.stringify(levels, null, 2));

    // Earnings events CSV (all rows + advanced fields)
    const events = Array.isArray(payload?.events) ? payload.events : [];
    const eventRows = events.map((e) => ({
      earnDate: e?.earnDate ?? "",
      anncTod: e?.anncTod ?? "",
      timing: e?.timing ?? "",
      pricingDateUsed: e?.pricingDateUsed ?? "",
      impErnMv: e?.impErnMv ?? "",
      impliedMovePct: e?.impliedMovePct ?? "",
      closeDateUsed: e?.closeDateUsed ?? "",
      closePx: e?.closePx ?? "",
      openDateUsed: e?.openDateUsed ?? "",
      openPx: e?.openPx ?? "",
      realizedMovePct: e?.realizedMovePct ?? "",
      signedMovePct: e?.signedMovePct ?? "",
      breachSide: e?.breachSide ?? "",
      dirOvershootPct: (e?.upOvershootPct ?? e?.downOvershootPct) ?? "",
      breach: e?.breach ?? "",
      regimeLabel: e?.regimeAtEvent?.label ?? "",
      regimeTailMultiplier: e?.regimeAtEvent?.tailMultiplier ?? "",
      regimeTradeGate: e?.regimeAtEvent?.tradeGate ?? "",
      aboveBreachPct: e?.aboveBreachPct ?? "",
      notes: Array.isArray(e?.notes) ? e.notes.join("; ") : "",
    }));
    zip.addText("tables/earnings_events.csv", toCsv(eventRows));

    // Quarter seasonality CSV
    const quarters = payload?.quarters || {};
    const qRows = ["Q1", "Q2", "Q3", "Q4"].map((q) => {
      const r = quarters?.[q] || {};
      const s = r?.seasonality || {};
      return {
        quarter: q,
        recommendation: r?.recommendation ?? "",
        breach_delta_pp: s?.breach_delta_pp ?? "",
        ratio_delta: s?.ratio_delta ?? "",
        overshoot_delta_pp: s?.overshoot_delta_pp ?? "",
        avg_ratio_realized_to_implied: r?.avg_ratio_realized_to_implied ?? "",
        max_ratio_realized_to_implied: r?.max_ratio_realized_to_implied ?? "",
      };
    });
    zip.addText("tables/quarter_seasonality.csv", toCsv(qRows));

    // Risk checks CSV (one row per check; no state)
    const rcRows = (riskChecks?.checks || []).map((c) => {
      const v = (c && typeof c.value === "object") ? c.value : {};
      const flat = { id: c?.id ?? "", label: c?.label ?? "", code: c?.code ?? "", explain: c?.explain ?? "" };
      for (const [k, val] of Object.entries(v || {})) {
        if (k === "notes") continue;
        if (typeof val === "object") continue;
        flat[`value.${k}`] = val;
      }
      flat["value.notes"] = Array.isArray(v?.notes) ? v.notes.join(" | ") : "";
      return flat;
    });
    zip.addText("tables/risk_checks.csv", toCsv(rcRows));

    downloadBlob(`${base}.zip`, zip.toBlob());
    if (status) status.textContent = `Exported: ${base}.zip`;
  } catch (e) {
    if (status) status.textContent = `Export error: ${String(e?.message || e)}`;
  }
}

async function exportEngine1OnePageOnly() {
  const status = $("status");
  const payload = lastPayload;
  if (!payload) {
    if (status) status.textContent = "Export: run Engine 1 first (no payload yet).";
    return;
  }
  const t = String(payload?.ticker || $("ticker")?.value || "").trim().toUpperCase();

  try {
    if (status) status.textContent = "Exporting one-page…";
    const levels = await _ensureEngine1LevelsPayload(t);
    const riskChecks = sanitizeRiskChecks(payload?.goNoGo || null);
    const sanitizedPayload = sanitizeEngine1Payload(payload);

    const uiState = {
      engine: "engine1",
      url: String(window.location?.href || ""),
      ticker: t,
      k: String($("k")?.value || ""),
      n: 20,
      years: 5,
      showAdvancedCols: true,
      earningsExpanded: true,
      gammaView: String(engine1GammaState?.view || ""),
      gammaLayers: { ...(engine1GammaState?.layers || {}) },
      heatmapView: String(engine1GexState?.view || ""),
      heatmapMode: String(engine1GexState?.mode || ""),
    };

    const base = _engine1ExportFileNameBase(payload);
    const md = buildEngine1OnePageMarkdown({ sanitizedPayload, riskChecks, levels, uiState });
    const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
    downloadBlob(`${base}-one_page.md`, blob);
    if (status) status.textContent = `Exported: ${base}-one_page.md`;
  } catch (e) {
    if (status) status.textContent = `Export error: ${String(e?.message || e)}`;
  }
}

let _tooltipsGlobalBound = false;
function renderEngine1DecisionPanel(payload) {
  const host = $("e1DecisionSection");
  if (!host) return;

  const t = String(payload?.ticker || "").toUpperCase() || "—";
  const tech = payload?.technicals || null;
  const nar = tech?.narrative || null;
  const barDate = String(tech?.barDateUsed || tech?.asOfDate || "").slice(0, 10);
  const price = Number(nar?.priceUsed ?? tech?.livePrice ?? tech?.lastDailyClose);

  const s = payload?.summary || {};
  const br = Number(s?.breach_rate_pct);
  const os = Number(s?.avg_above_breach_pct);
  const breaches = (s?.breaches !== null && s?.breaches !== undefined) ? Number(s.breaches) : null;
  const used = (s?.events_used !== null && s?.events_used !== undefined) ? Number(s.events_used) : null;
  const found = (s?.events_found !== null && s?.events_found !== undefined) ? Number(s.events_found) : null;

  const b = payload?.baseline || {};
  const avgRatio = (b?.avg_ratio_realized_to_implied !== null && b?.avg_ratio_realized_to_implied !== undefined)
    ? Number(b.avg_ratio_realized_to_implied)
    : null;

  const rg = payload?.regime || {};
  const gate = String(rg?.guidance?.tradeGate || "");
  const gateTxt = gate === "NO_TRADE" ? "No Trade" : gate === "CAUTION" ? "Caution" : gate === "OK" ? "OK" : "—";
  const label = String(rg?.label || "—");

  // Expected Move data
  const em = payload?.expectedMove || {};
  const emPct = em?.expectedMovePct;
  const emDollars = em?.expectedMoveDollars;
  const emExpiry = String(em?.expiry || "").slice(0, 10);
  const emSource = String(em?.source || "").toLowerCase();
  const emSourceLabel = emSource === "live" ? "Live" : emSource === "eod" ? "EOD" : emSource === "impermnv" ? "EM" : emSource ? emSource : "—";

  // Strike Targets data
  const st = payload?.strikeTargets || null;
  const stWhite = st?.whitePts;
  const stBlue = st?.bluePts;
  const stRed = st?.redPts;

  const chips = [];
  if (gateTxt !== "—") chips.push(`Gate: ${gateTxt}`);
  if (label && label !== "—") chips.push(`Regime: ${label}`);
  if (payload?.eventRisk?.label) chips.push(`Event risk: ${String(payload.eventRisk.label)}`);
  const chipHtml = chips.slice(0, 3).map((c) => `<span class="taChip">${escapeHtml(c)}</span>`).join("");

  const dots = Array.from({ length: 5 }).map((_, i) => `<span class="taDot ${i < 3 ? "isOn" : ""}"></span>`).join("");

  host.classList.toggle("hidden", !t || t === "—");
  if (!t || t === "—") return;

  const go = payload?.goNoGo || null;
  const goStatus = String(go?.status || "").toUpperCase();
  const goPassed = go?.passed === true && goStatus === "GO";
  const goLabel = goPassed ? "GO" : "NO-GO";
  const goCls = goPassed ? "isGo" : "isNo";

  const pxTxt = Number.isFinite(price) ? price.toFixed(2) : "—";
  const metaRight = [
    `EOD: ${escapeHtml(barDate || "—")}`,
    `Price: <span class="mono">${escapeHtml(pxTxt)}</span>`,
    (Number.isFinite(breaches) && Number.isFinite(used) && Number.isFinite(found)) ? `${Number(breaches)} breaches / ${Number(used)} usable (found ${Number(found)})` : "",
  ].filter(Boolean).join(" • ");
  host.innerHTML = `
    <div class="taPanel">
      <div class="taHeader">
        <div class="taHeaderRow">
          <div class="taHeaderTitle">
            ${escapeHtml(t)} — Engine 1
            <button class="goPill ${goCls}" type="button" id="e1GoNoGoBtn" aria-label="GO/NO-GO details" aria-haspopup="dialog">${escapeHtml(goLabel)}</button>
          </div>
          <div class="taHeaderMeta">${metaRight}</div>
        </div>
        <div class="taHeaderRow taHeaderRow--sub">
          <div class="taBiasPill taBiasPill--neu">RISK CHECK</div>
          <div class="taConf" title="Confidence dots (heuristic)">${dots}</div>
          <div class="taChips">${chipHtml}</div>
          <div class="taHeaderActions">
            <button class="taActionBtn" type="button" id="e1ExportLLM">Export (LLM)</button>
            <button class="taActionBtn" type="button" id="e1ExportOnePage">Export One-Page (LLM)</button>
          </div>
        </div>
      </div>

      <div class="taGrid" aria-label="Engine 1 instrument cards">
        <div class="taCard">
          <div class="taCardTop"><div class="taCardTitle">Breach rate</div></div>
          <div class="taCardState mono">${Number.isFinite(br) ? escapeHtml(br.toFixed(2)) + "%" : "—"}</div>
          <div class="taCardInterp">Share of usable events that breached (k-controlled).</div>
        </div>
        <div class="taCard">
          <div class="taCardTop"><div class="taCardTitle">Avg overshoot if breached</div></div>
          <div class="taCardState mono">${Number.isFinite(os) ? escapeHtml(os.toFixed(2)) + "%" : "—"}</div>
          <div class="taCardInterp">Tail severity conditional on breach.</div>
        </div>
        <div class="taCard">
          <div class="taCardTop"><div class="taCardTitle">Avg realized / implied</div></div>
          <div class="taCardState mono">${Number.isFinite(avgRatio) ? escapeHtml(avgRatio.toFixed(2)) + "×" : "—"}</div>
          <div class="taCardInterp">Baseline ratio across usable events.</div>
        </div>
        <div class="taCard">
          <div class="taCardTop"><div class="taCardTitle">Events used</div></div>
          <div class="taCardState mono">${Number.isFinite(used) && Number.isFinite(found) ? `${escapeHtml(String(used))} / ${escapeHtml(String(found))}` : "—"}</div>
          <div class="taCardInterp">Usable / Found in lookback window.</div>
        </div>
        <div class="taCard">
          <div class="taCardTop"><div class="taCardTitle">Regime</div></div>
          <div class="taCardState">${escapeHtml(label)}</div>
          <div class="taCardInterp">Market + single-name stress overlay.</div>
        </div>
        <div class="taCard">
          <div class="taCardTop"><div class="taCardTitle">Trade gate</div></div>
          <div class="taCardState">${escapeHtml(gateTxt)}</div>
          <div class="taCardInterp">OK / Caution / No trade (risk-first).</div>
        </div>
        <div class="taCard">
          <div class="taCardTop">
            <div class="taCardTitle">Expected Move</div>
            <span class="info" title="Risk-neutral expected absolute move to near-dated expiry. Computed via ATM-forward straddle (gold standard). Uses live data when market is open, EOD otherwise.">ⓘ</span>
          </div>
          <div class="taCardState mono">${Number.isFinite(emPct) ? escapeHtml(emPct.toFixed(2)) + "%" : "—"}</div>
          <div class="taCardInterp">${Number.isFinite(emDollars) ? `$${escapeHtml(emDollars.toFixed(2))} pts` : "—"} · ${emExpiry ? `Exp: ${escapeHtml(emExpiry)}` : ""} · ${emSourceLabel}</div>
        </div>
        <div class="taCard">
          <div class="taCardTop">
            <div class="taCardTitle">Strike Targets (EM)</div>
            <span class="info" title="Wing strike distances based on expected move. White = 2× EM pts, Blue = 1.5× White, Red = 2× White. Use for short-strike targeting.">ⓘ</span>
          </div>
          <div class="emTargetGrid">
            <div class="emRow emBox--white"><span class="k">1.0× EM</span><span class="v mono">${Number.isFinite(stWhite) ? escapeHtml(stWhite.toFixed(2)) + " pts" : "—"}</span></div>
            <div class="emRow emBox--blue"><span class="k">1.5× EM</span><span class="v mono">${Number.isFinite(stBlue) ? escapeHtml(stBlue.toFixed(2)) + " pts" : "—"}</span></div>
            <div class="emRow emBox--red"><span class="k">2.0× EM</span><span class="v mono">${Number.isFinite(stRed) ? escapeHtml(stRed.toFixed(2)) + " pts" : "—"}</span></div>
          </div>
          <div class="taCardInterp">Wing distance from spot.</div>
        </div>
      </div>

      <div id="actionSummary" class="taCardInterp muted" style="margin-top:10px;" aria-live="polite">—</div>
    </div>
  `;

  // GO/NO-GO modal (centered)
  const goBtn = $("e1GoNoGoBtn");
  if (goBtn) {
    goBtn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      openGoNoGoModal(go);
    });
  }

  const expBtn = $("e1ExportLLM");
  if (expBtn) {
    expBtn.addEventListener("click", async () => {
      try { await exportEngine1LLMBundle(); } catch { /* ignore */ }
    });
  }

  const oneBtn = $("e1ExportOnePage");
  if (oneBtn) {
    oneBtn.addEventListener("click", async () => {
      try { await exportEngine1OnePageOnly(); } catch { /* ignore */ }
    });
  }
}

// --- GO/NO-GO Modal (centered) ---
let _goModalBound = false;
function ensureGoNoGoModal() {
  let overlay = document.getElementById("goNoGoOverlay");
  if (overlay) return overlay;
  overlay = document.createElement("div");
  overlay.id = "goNoGoOverlay";
  overlay.className = "goModalOverlay hidden";
  overlay.innerHTML = `
    <div class="goModal" role="dialog" aria-modal="true" aria-label="GO/NO-GO details">
      <button class="goModalClose" type="button" aria-label="Close">×</button>
      <div class="goModalHead">
        <div class="goModalTitle">GO/NO-GO</div>
        <div class="goModalVerdict" id="goNoGoVerdict">—</div>
      </div>
      <div class="goModalSub muted" id="goNoGoSub">—</div>
      <div class="goCols" id="goNoGoCols"></div>
      <div class="goModalFoot" id="goNoGoFoot"></div>
    </div>
  `;
  document.body.appendChild(overlay);

  if (!_goModalBound) {
    _goModalBound = true;
    overlay.addEventListener("click", (ev) => {
      const t = ev.target;
      if (!t) return;
      if (t === overlay) closeGoNoGoModal();
      if (t.closest && t.closest(".goModalClose")) closeGoNoGoModal();
    });
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") closeGoNoGoModal();
    });
  }
  return overlay;
}

function closeGoNoGoModal() {
  const overlay = document.getElementById("goNoGoOverlay");
  if (!overlay) return;
  overlay.classList.add("hidden");
}

function openGoNoGoModal(go) {
  const overlay = ensureGoNoGoModal();
  const status = String(go?.status || "NO_GO").toUpperCase();
  const passed = (go?.passed === true && status === "GO");
  const checks = Array.isArray(go?.checks) ? go.checks : [];
  const warns = Array.isArray(go?.warnings) ? go.warnings : [];

  const verdictEl = document.getElementById("goNoGoVerdict");
  const subEl = document.getElementById("goNoGoSub");
  const colsEl = document.getElementById("goNoGoCols");
  const footEl = document.getElementById("goNoGoFoot");
  if (!verdictEl || !subEl || !colsEl || !footEl) return;

  verdictEl.textContent = passed ? "GO" : "NO-GO";
  verdictEl.classList.toggle("isGo", passed);
  verdictEl.classList.toggle("isNo", !passed);
  subEl.textContent = passed ? "All checks passed" : "One or more checks failed / missing";

  const by = { FAIL: [], MISSING: [], PASS: [] };
  for (const c of checks) {
    const st = String(c?.state || "MISSING").toUpperCase();
    const k = (st === "PASS" || st === "FAIL") ? st : "MISSING";
    by[k].push(c);
  }

  const renderCol = (title, st) => {
    const items = by[st] || [];
    const icon = st === "PASS" ? "✅" : st === "FAIL" ? "❌" : "⚠️";
    const cls = st === "PASS" ? "isPass" : st === "FAIL" ? "isFail" : "isMissing";
    const rows = items.map((c) => {
      const label = String(c?.label || c?.id || "—");
      const code = c?.code ? String(c.code) : "";
      const metrics = goMetricsLine(c);
      const explain = (c?.explain !== null && c?.explain !== undefined) ? String(c.explain) : "";
      const notes = Array.isArray(c?.value?.notes) ? c.value.notes : [];
      const isLiq = String(c?.id || "") === "SN_LIQUIDITY";
      const clean = (xs) => xs.map((x) => String(x ?? "").trim()).filter(Boolean);
      let noteLines = clean(notes.slice(0, isLiq ? 4 : 3));
      if (isLiq && notes.length > 4) {
        const tail = clean(notes.slice(-4));
        // Merge unique while preserving order.
        for (const t of tail) {
          if (!noteLines.includes(t)) noteLines.push(t);
        }
      }
      const showExplain = (!metrics || !metrics.trim()) || (String(c?.state || "").toUpperCase() !== "PASS");
      return `
        <div class="goRow">
          <div class="goRowTop">
            <span class="goRowLabel">${escapeHtml(label)}</span>
          </div>
          <div class="goRowMeta">
            ${code ? `<span class="mono goRowCode">${escapeHtml(code)}</span>` : ""}
            ${metrics ? `<span class="mono goRowMetrics">${escapeHtml(metrics)}</span>` : ""}
            ${showExplain && explain ? `<div class="goRowExplain muted">${escapeHtml(explain)}</div>` : ""}
            ${showExplain && noteLines.length ? noteLines.map((s) => `<div class="goRowExplain muted">${escapeHtml(s)}</div>`).join("") : ""}
          </div>
        </div>
      `;
    }).join("");
    return `
      <div class="goCol ${cls}">
        <div class="goColHead">
          <span class="goColIcon" aria-hidden="true">${icon}</span>
          <span class="goColTitle">${escapeHtml(title)}</span>
          <span class="goColCount mono">${items.length}</span>
        </div>
        <div class="goColBody">${rows || `<div class="muted">—</div>`}</div>
      </div>
    `;
  };

  colsEl.innerHTML = [
    renderCol("Fail", "FAIL"),
    renderCol("Missing", "MISSING"),
    renderCol("Pass", "PASS"),
  ].join("");

  footEl.innerHTML = warns.length
    ? `<div class="goWarnTitle">Warnings</div>${warns.slice(0, 5).map(w => `<div class="goWarnLine muted">${escapeHtml(String(w?.label || w?.id || "warning"))}</div>`).join("")}`
    : `<div class="muted">—</div>`;

  overlay.classList.remove("hidden");
}

function goMetricsLine(c) {
  const id = String(c?.id || "");
  const v = c?.value || {};
  // Avoid JS gotcha: Number(null) === 0. Treat null/undefined/"" as "missing".
  const num = (x) => (x === null || x === undefined || x === "" ? Number.NaN : Number(x));
  try {
    if (id === "SN_IV_ELEVATED") {
      const iv = num(v?.currentIv30Pct);
      const n = num(v?.sampleN);
      const p = num(v?.percentile01);
      const z = num(v?.z);
      const parts = [];
      if (Number.isFinite(iv)) parts.push(`IV=${iv.toFixed(2)}%`);
      if (Number.isFinite(p)) parts.push(`pctl=${p.toFixed(2)}`);
      if (Number.isFinite(z)) parts.push(`z=${z.toFixed(2)}`);
      if (Number.isFinite(n)) parts.push(`n=${n.toFixed(0)}`);
      return parts.join(" · ");
    }
    if (id === "SN_EM_RICHNESS") {
      const em = num(v?.expectedMovePct);
      const med = num(v?.realizedMedianPct);
      const n = num(v?.nUsed);
      const r = num(v?.ratio);
      const parts = [];
      if (Number.isFinite(em)) parts.push(`EM=${em.toFixed(2)}%`);
      if (Number.isFinite(med)) parts.push(`med=${med.toFixed(2)}%`);
      if (Number.isFinite(r)) parts.push(`ratio=${r.toFixed(2)}×`);
      if (Number.isFinite(n)) parts.push(`n=${n.toFixed(0)}`);
      return parts.join(" · ");
    }
    if (id === "SN_TAIL_P90_RICHNESS") {
      const em = num(v?.expectedMovePct);
      const p90 = num(v?.p90RealizedPct);
      const n = num(v?.nUsed);
      const r = num(v?.ratio);
      const parts = [];
      if (Number.isFinite(em)) parts.push(`EM=${em.toFixed(2)}%`);
      if (Number.isFinite(p90)) parts.push(`p90=${p90.toFixed(2)}%`);
      if (Number.isFinite(r)) parts.push(`ratio=${r.toFixed(2)}×`);
      if (Number.isFinite(n)) parts.push(`n=${n.toFixed(0)}`);
      return parts.join(" · ");
    }
    if (id === "SN_LIQUIDITY") {
      const dvol = num(v?.avgDollarVol20d);
      const exp = v?.expiry ? String(v.expiry).slice(5) : "";
      const src = v?.underlyingSource ? String(v.underlyingSource) : "";
      const agg = v?.deltaBandAgg || {};
      const put = agg?.put || {};
      const call = agg?.call || {};
      const covP = num(put?.coverage);
      const covC = num(call?.coverage);
      const sprP = num(put?.medianSpread);
      const sprC = num(call?.medianSpread);
      const oiP = num(put?.sumOI);
      const oiC = num(call?.sumOI);
      const volP = num(put?.sumVol);
      const volC = num(call?.sumVol);
      const parts = [];
      if (Number.isFinite(dvol)) parts.push(`$vol20=${Math.round(dvol/1e6)}M`);
      if (exp) parts.push(`exp=${exp}`);
      if (src) parts.push(`src=${src}`);
      if (Number.isFinite(covP) || Number.isFinite(covC)) {
        const a = Number.isFinite(covP) ? covP.toFixed(2) : "—";
        const b = Number.isFinite(covC) ? covC.toFixed(2) : "—";
        parts.push(`cov(P,C)=${a},${b}`);
      }
      if (Number.isFinite(sprP) || Number.isFinite(sprC)) {
        const a = Number.isFinite(sprP) ? sprP.toFixed(2) : "—";
        const b = Number.isFinite(sprC) ? sprC.toFixed(2) : "—";
        parts.push(`spr50(P,C)=${a},${b}`);
      }
      if (Number.isFinite(oiP) || Number.isFinite(oiC) || Number.isFinite(volP) || Number.isFinite(volC)) {
        const a = Number.isFinite(oiP) ? oiP.toFixed(0) : "—";
        const b = Number.isFinite(oiC) ? oiC.toFixed(0) : "—";
        const c0 = Number.isFinite(volP) ? volP.toFixed(0) : "—";
        const d0 = Number.isFinite(volC) ? volC.toFixed(0) : "—";
        parts.push(`OI(P,C)=${a},${b}`);
        parts.push(`Vol(P,C)=${c0},${d0}`);
      }
      return parts.join(" · ");
    }
    if (id === "MACRO_GAMMA") {
      const sign = String(v?.netGammaSign || "—");
      const b = String(v?.magnitudeBucket || "—");
      const sym = String(v?.symbolUsed || "SPX");
      return `${sym} · ${sign} · ${b}`;
    }
    if (id === "SN_INDEX_SENSITIVITY") {
      const c20 = num(v?.corr20);
      const b20 = num(v?.beta20);
      const sens = v?.sensitive === true;
      const parts = [];
      if (Number.isFinite(c20)) parts.push(`corr20=${c20.toFixed(2)}`);
      if (Number.isFinite(b20)) parts.push(`beta20=${b20.toFixed(2)}`);
      if (sens) parts.push("tighten");
      return parts.join(" · ");
    }
    if (id === "MACRO_RV_ACCEL") {
      const r5 = num(v?.rv5Jump);
      const r20 = num(v?.rv20Jump);
      const parts = [];
      if (Number.isFinite(r5)) parts.push(`rv5=${r5.toFixed(2)}×`);
      if (Number.isFinite(r20)) parts.push(`rv20=${r20.toFixed(2)}×`);
      return parts.join(" · ");
    }
    if (id === "MACRO_GAMMA_FLIP") {
      const m = num(v?.minFlipEm);
      const cut = num(v?.cutoffEm);
      if (Number.isFinite(m) && Number.isFinite(cut)) return `minFlip=${m.toFixed(2)}×EM · cut=${cut.toFixed(2)}`;
      if (Number.isFinite(m)) return `minFlip=${m.toFixed(2)}×EM`;
    }
    if (id === "MACRO_FORCED_FLOWS") {
      const hi = Array.isArray(v?.high) ? v.high.length : 0;
      const win = Array.isArray(v?.windowTradingDays) ? v.windowTradingDays.length : null;
      if (win !== null) return `HIGH=${hi} · window=${win}d`;
      return `HIGH=${hi}`;
    }
    return "";
  } catch {
    return "";
  }
}

function _pickMeaningfulClusters(sideClusters, spot, strikeStep) {
  const xs = Array.isArray(sideClusters) ? sideClusters : [];
  if (!xs.length) return [];

  const top = xs[0];
  const topTotal = Number(top?.totalOI || 0);
  const topPeak = Number(top?.peakStrike ?? top?.maxStrike);
  const topDist = (Number.isFinite(spot) && Number.isFinite(topPeak)) ? Math.abs(topPeak - spot) : Number.POSITIVE_INFINITY;

  const out = [top];
  for (let i = 1; i < xs.length && out.length < 3; i++) {
    const c = xs[i];
    const total = Number(c?.totalOI || 0);
    const peak = Number(c?.peakStrike ?? c?.maxStrike);
    const dist = (Number.isFinite(spot) && Number.isFinite(peak)) ? Math.abs(peak - spot) : Number.POSITIVE_INFINITY;

    const bigEnough = (topTotal > 0) ? (total / topTotal) >= 0.6 : false;
    const closerToSpot = dist + 1e-9 < topDist;
    const separated = (Number.isFinite(strikeStep) && strikeStep > 0 && Number.isFinite(peak) && Number.isFinite(topPeak))
      ? Math.abs(peak - topPeak) >= 2 * strikeStep
      : false;

    if (bigEnough || closerToSpot || separated) out.push(c);
  }
  return out;
}

function _fmtClusterLine(c) {
  const peak = c?.peakStrike ?? c?.maxStrike;
  return `${fmt0(peak)} (${fmt0(c?.totalOI)}) · range=${fmt0(c?.minStrike)}–${fmt0(c?.maxStrike)} · n=${fmt0(c?.nStrikes)}`;
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function initTooltips() {
  const wraps = Array.from(document.querySelectorAll(".tipWrap"));
  const closeAll = () => {
    wraps.forEach(w => {
      w.classList.remove("isOpen");
      const b = w.querySelector(".tipBtn");
      if (b) b.setAttribute("aria-expanded", "false");
    });
  };

  wraps.forEach((w) => {
    if (w && w.dataset && w.dataset.tipInit === "1") return;
    const btn = w.querySelector(".tipBtn");
    if (!btn) return;
    if (w && w.dataset) w.dataset.tipInit = "1";
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const isOpen = w.classList.contains("isOpen");
      closeAll();
      if (!isOpen) {
        w.classList.add("isOpen");
        btn.setAttribute("aria-expanded", "true");
      }
    });
  });

  if (!_tooltipsGlobalBound) {
    _tooltipsGlobalBound = true;
    document.addEventListener("click", (ev) => {
      const t = ev.target;
      if (t && t.closest && t.closest(".tipWrap")) return;
      closeAll();
    });

    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") closeAll();
    });
  }
}

// --- Engine 1: Live Gamma Visuals (Dealer Gamma Map + Weekly Gamma Risk Heat-Map) ---
let lastEngine1LevelsPayload = null;
let lastEngine1Ticker = null;

const engine1GammaState = {
  view: "weekly", // weekly|nearest
  layers: { putWall: true, callWall: true, clusters: true, gammaPeaks: true, gammaFlip: true },
};

const engine1GexState = {
  view: "composite", // composite|raw
  mode: "slope", // net|slope
};

function e1Clamp(x, lo, hi) {
  const n = Number(x);
  if (!Number.isFinite(n)) return lo;
  return Math.max(Number(lo), Math.min(Number(hi), n));
}

function e1FmtDateShort(iso) {
  const s = String(iso || "").slice(0, 10);
  return s || "—";
}

function e1FmtNum(x, d = 2) {
  const n = Number(x);
  if (!Number.isFinite(n)) return "—";
  return n.toFixed(d);
}

function e1Fmt2(x) {
  const n = Number(x);
  return Number.isFinite(n) ? n.toFixed(2) : "—";
}

function e1FmtMoneyShort(x) {
  const n = Number(x);
  if (!Number.isFinite(n)) return "—";
  const s = n < 0 ? "-" : "+";
  const a = Math.abs(n);
  if (a >= 1e12) return `${s}$${(a / 1e12).toFixed(2)}T`;
  if (a >= 1e9) return `${s}$${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `${s}$${(a / 1e3).toFixed(2)}K`;
  return `${s}$${a.toFixed(0)}`;
}

function initEngine1GammaVizUI() {
  // Gamma map view toggles
  const weeklyBtn = $("e1GammaViewWeekly");
  const nearestBtn = $("e1GammaViewNearest");

  const setGammaView = (v) => {
    engine1GammaState.view = (v === "nearest") ? "nearest" : "weekly";
    if (weeklyBtn) {
      const on = engine1GammaState.view === "weekly";
      weeklyBtn.classList.toggle("isOn", on);
      weeklyBtn.setAttribute("aria-pressed", on ? "true" : "false");
    }
    if (nearestBtn) {
      const on = engine1GammaState.view === "nearest";
      nearestBtn.classList.toggle("isOn", on);
      nearestBtn.setAttribute("aria-pressed", on ? "true" : "false");
    }
    loadEngine1Levels(lastEngine1Ticker);
  };

  if (weeklyBtn) weeklyBtn.addEventListener("click", () => setGammaView("weekly"));
  if (nearestBtn) nearestBtn.addEventListener("click", () => setGammaView("nearest"));

  // Gamma map overlay layer toggles
  const legend = document.querySelector(".gammaLegend--e1");
  if (legend) {
    legend.addEventListener("click", (ev) => {
      const t = ev.target;
      if (!t || !t.closest) return;
      const btn = t.closest("button[data-layer]");
      if (!btn) return;
      const k = String(btn.getAttribute("data-layer") || "");
      if (!k) return;
      const cur = !!engine1GammaState.layers[k];
      engine1GammaState.layers[k] = !cur;
      btn.classList.toggle("isOn", !cur);
      btn.setAttribute("aria-pressed", (!cur) ? "true" : "false");
      renderEngine1GammaMap(lastEngine1LevelsPayload);
    });
  }

  // Heatmap toggles
  const btnComp = $("e1GexViewComposite");
  const btnRaw = $("e1GexViewRaw");
  const btnNet = $("e1GexModeNet");
  const btnSlope = $("e1GexModeSlope");

  const syncHeatButtons = () => {
    if (btnComp) { btnComp.classList.toggle("isOn", engine1GexState.view === "composite"); btnComp.setAttribute("aria-pressed", engine1GexState.view === "composite" ? "true" : "false"); }
    if (btnRaw) { btnRaw.classList.toggle("isOn", engine1GexState.view === "raw"); btnRaw.setAttribute("aria-pressed", engine1GexState.view === "raw" ? "true" : "false"); }
    if (btnNet) { btnNet.classList.toggle("isOn", engine1GexState.mode === "net"); btnNet.setAttribute("aria-pressed", engine1GexState.mode === "net" ? "true" : "false"); }
    if (btnSlope) { btnSlope.classList.toggle("isOn", engine1GexState.mode === "slope"); btnSlope.setAttribute("aria-pressed", engine1GexState.mode === "slope" ? "true" : "false"); }
  };
  syncHeatButtons();

  const setHeatView = (v) => { engine1GexState.view = (v === "raw") ? "raw" : "composite"; syncHeatButtons(); loadEngine1Levels(lastEngine1Ticker); };
  const setHeatMode = (m) => { engine1GexState.mode = (m === "net") ? "net" : "slope"; syncHeatButtons(); loadEngine1Levels(lastEngine1Ticker); };

  if (btnComp) btnComp.addEventListener("click", () => setHeatView("composite"));
  if (btnRaw) btnRaw.addEventListener("click", () => setHeatView("raw"));
  if (btnNet) btnNet.addEventListener("click", () => setHeatMode("net"));
  if (btnSlope) btnSlope.addEventListener("click", () => setHeatMode("slope"));

  window.addEventListener("resize", () => {
    renderEngine1GammaMap(lastEngine1LevelsPayload);
    renderEngine1GexHeatmap(lastEngine1LevelsPayload);
  });
}

async function loadEngine1Levels(ticker) {
  const t = String(ticker || $("ticker")?.value || "").trim().toUpperCase();
  lastEngine1Ticker = t || null;
  const chart = $("e1GammaChart");
  const meta = $("e1GammaMeta");
  const note = $("e1GammaNote");
  if (!chart) return;

  if (!t) {
    renderEngine1GammaMap(null);
    renderEngine1GexHeatmap(null);
    return;
  }

  try {
    if (meta) meta.textContent = "Loading…";
    if (note) note.textContent = "—";
    const v = engine1GammaState.view;
    const url =
      `/api/levels?ticker=${encodeURIComponent(t)}`
      + `&view=${encodeURIComponent(v)}`
      + `&points=90&window_days=180&include_heatmap=1`
      + `&heatmap_view=${encodeURIComponent(engine1GexState.view)}`
      + `&heatmap_mode=${encodeURIComponent(engine1GexState.mode)}`
      + `&slope_window=5&flip_adjacent_n=5`;
    const payload = await fetchJson(url);
    lastEngine1LevelsPayload = payload;
    renderEngine1GammaMap(payload);
    renderEngine1GexHeatmap(payload);
  } catch (e) {
    lastEngine1LevelsPayload = null;
    if (meta) meta.textContent = "Dealer Gamma Map unavailable";
    if (note) note.textContent = String(e?.message || e || "Error");
    chart.innerHTML = `<div class="muted" style="padding:14px;">${escapeHtml(String(e?.message || e || "Failed to load."))}</div>`;
    renderEngine1GexHeatmap(null);
  }
}

function renderEngine1GexHeatmap(payload) {
  const wrap = $("e1GexHeatmap");
  const meta = $("e1GexMeta");
  const note = $("e1GexNote");
  const tip = $("e1GexHeatTip");
  const downPtsEl = $("e1GexDownPts");
  const downEmEl = $("e1GexDownEm");
  const upPtsEl = $("e1GexUpPts");
  const upEmEl = $("e1GexUpEm");
  const stabEl = $("e1GexStability");
  if (!wrap) return;

  const heat = payload?.levels?.gexHeatmap || null;
  const enabled = !!heat?.enabled;
  const spot = Number(heat?.spot);
  const band = Number(heat?.bandPct);
  const wmode = String(heat?.weightingMode || "");
  const denom = Number(heat?.scaleDenom);
  const ivUsed = Number(heat?.atmIvUsedPct);

  // Metrics strip
  const m = heat?.metrics || {};
  if (downPtsEl) downPtsEl.textContent = Number.isFinite(Number(m?.downsideDistancePts)) ? e1Fmt2(m.downsideDistancePts) : "—";
  if (upPtsEl) upPtsEl.textContent = Number.isFinite(Number(m?.upsideDistancePts)) ? e1Fmt2(m.upsideDistancePts) : "—";
  if (downEmEl) downEmEl.textContent = Number.isFinite(Number(m?.downsideDistanceEm)) ? e1Fmt2(m.downsideDistanceEm) : "—";
  if (upEmEl) upEmEl.textContent = Number.isFinite(Number(m?.upsideDistanceEm)) ? e1Fmt2(m.upsideDistanceEm) : "—";
  const st = heat?.stability || {};
  if (stabEl) {
    const lab = String(st?.label || "—");
    stabEl.textContent = lab;
    stabEl.classList.toggle("isStable", lab === "Stable");
    stabEl.classList.toggle("isAsym", lab === "Asymmetric");
    stabEl.classList.toggle("isFragile", lab === "Fragile");
    const rs = Array.isArray(st?.reasons) ? st.reasons.filter(Boolean) : [];
    stabEl.title = rs.join("\n");
  }

  const hideTip = () => { if (tip) tip.classList.add("hidden"); };
  const showTip = (html, x, y) => {
    if (!tip) return;
    tip.innerHTML = html;
    tip.classList.remove("hidden");
    const box = wrap.getBoundingClientRect();
    const left = e1Clamp(x - box.left + 12, 8, box.width - 260);
    const top = e1Clamp(y - box.top + 12, 8, box.height - 140);
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
  };

  // Determine which dataset to render
  let yLabels = [];
  let strikes = [];
  let mat = [];
  let rowMeta = [];

  const raw = heat?.raw || {};
  const comp = heat?.composite || {};
  if (engine1GexState.view === "raw") {
    const expiries = Array.isArray(raw?.expiries) ? raw.expiries : [];
    strikes = Array.isArray(raw?.strikes) ? raw.strikes : [];
    const net = Array.isArray(raw?.netDollarGex) ? raw.netDollarGex : [];
    const slope = Array.isArray(raw?.slopeNetDollarGex) ? raw.slopeNetDollarGex : [];
    mat = (engine1GexState.mode === "slope") ? slope : net;
    yLabels = expiries.map((e) => String(e).slice(5));
    rowMeta = expiries.map((e) => ({ expiry: String(e) }));
  } else {
    const buckets = Array.isArray(comp?.buckets) ? comp.buckets : [];
    strikes = Array.isArray(comp?.strikes) ? comp.strikes : [];
    yLabels = buckets.map((b) => String(b?.label || b?.key || "—"));
    rowMeta = buckets.map((b) => ({ key: b?.key, effectiveDte: b?.effectiveDte, expectedMovePts: b?.expectedMovePts }));
    mat = buckets.map((b) => (engine1GexState.mode === "slope") ? (b?.slopeNetDollarGex || []) : (b?.netDollarGex || []));
  }

  if (!payload || !enabled || !yLabels.length || !strikes.length || !mat.length) {
    wrap.innerHTML = `<div class="muted" style="padding:14px;">Run Engine 1 to load the heat map.</div>`;
    if (meta) meta.textContent = "—";
    if (note) {
      const err = heat?.error ? `Heatmap unavailable (${String(heat.error)}).` : "—";
      note.textContent = payload && !enabled ? err : "—";
    }
    hideTip();
    return;
  }

  // Compute max abs for scaling (ignore nulls)
  let maxAbs = 0;
  for (let i = 0; i < mat.length; i++) {
    const row = Array.isArray(mat[i]) ? mat[i] : [];
    for (let j = 0; j < row.length; j++) {
      const v0 = Number(row[j]);
      if (!Number.isFinite(v0)) continue;
      const v = (Number.isFinite(denom) && denom > 0) ? (v0 / denom) : v0; // normalization is render-only
      maxAbs = Math.max(maxAbs, Math.abs(v));
    }
  }
  if (!Number.isFinite(maxAbs) || maxAbs <= 0) maxAbs = 1;

  const w = Math.max(320, wrap.clientWidth || 640);
  const pad = { l: 74, r: 10, t: 10, b: 26 };
  const rows = yLabels.length;
  const cols = strikes.length;
  const cellH = (engine1GexState.view === "composite") ? 64 : 16;
  const cellW = Math.max(6, Math.floor((w - pad.l - pad.r) / Math.max(1, cols)));
  const h = pad.t + pad.b + rows * cellH;

  const xForCol = (c) => pad.l + c * cellW;
  const yForRow = (r) => pad.t + r * cellH;

  const scale = (v) => {
    const n = Number(v);
    if (!Number.isFinite(n)) return 0;
    const nn = (Number.isFinite(denom) && denom > 0) ? (n / denom) : n;
    const a = Math.abs(nn);
    const t = Math.log10(1 + a / 1e6);
    const tMax = Math.log10(1 + maxAbs / 1e6);
    const u = tMax > 0 ? (t / tMax) : 0;
    return (nn < 0 ? -u : u);
  };

  const colorFor = (v) => {
    const n = Number(v);
    if (!Number.isFinite(n)) return "rgba(120,120,130,0.10)";
    const t = scale(n);
    const a = Math.min(1, Math.abs(t));
    const hue = (t < 0) ? 210 : 20;
    const sat = 72;
    const light = 82 - (a * 34);
    const alpha = 0.95;
    return `hsla(${hue}, ${sat}%, ${light}%, ${alpha})`;
  };

  if (meta) {
    const b = Number.isFinite(band) ? `${Math.round(band * 100)}%` : "—";
    const ivTxt = Number.isFinite(ivUsed) ? `${ivUsed.toFixed(2)}%` : "—";
    meta.textContent = `spot=${Number.isFinite(spot) ? e1FmtNum(spot, 2) : "—"} · band=±${b} · iv=${ivTxt} · mode=${wmode || "—"} · rows=${rows} · cols=${cols}`;
  }
  if (note) {
    const warns = Array.isArray(heat?.warnings) ? heat.warnings.filter(Boolean) : [];
    const notes = Array.isArray(heat?.notes) ? heat.notes.filter(Boolean) : [];
    note.textContent = warns[0] || notes[0] || "Live, informational only.";
  }

  const tickEvery = Math.max(1, Math.round(cols / 6));
  const xTicks = strikes.map((s, i) => ({ s: Number(s), i })).filter(t => (t.i % tickEvery) === 0);

  const bnds = heat?.boundaries || {};
  const downB = Number(bnds?.downsideAccelerationBoundaryStrike);
  const upB = Number(bnds?.upsideAccelerationBoundaryStrike);
  const xForStrike = (k) => {
    const kk = Number(k);
    if (!Number.isFinite(kk)) return null;
    let best = null;
    let bestD = null;
    for (let i = 0; i < strikes.length; i++) {
      const s = Number(strikes[i]);
      if (!Number.isFinite(s)) continue;
      const d = Math.abs(s - kk);
      if (best === null || bestD === null || d < bestD) {
        best = i;
        bestD = d;
      }
    }
    return best === null ? null : (xForCol(best) + (cellW / 2));
  };
  const xDown = xForStrike(downB);
  const xUp = xForStrike(upB);
  const xSpot = xForStrike(spot);

  wrap.innerHTML = `
    <svg class="gexSvg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="Weekly gamma risk heat map">
      <rect x="0" y="0" width="${w}" height="${h}" class="gexBg"></rect>
      ${yLabels.map((lab, r) => `<text x="${pad.l - 8}" y="${yForRow(r) + 12}" class="gexAxis gexAxis--y" text-anchor="end">${escapeHtml(lab)}</text>`).join("")}
      ${xTicks.map(t => `<text x="${xForCol(t.i) + 2}" y="${h - 10}" class="gexAxis gexAxis--x">${escapeHtml(fmt0(t.s))}</text>`).join("")}
      ${xSpot === null ? "" : `<line x1="${xSpot}" x2="${xSpot}" y1="${pad.t}" y2="${pad.t + rows * cellH}" class="gexSpot"></line>
        <text x="${xSpot + 6}" y="${pad.t + 34}" class="gexSpotLabel">Spot</text>`}
      ${xDown === null ? "" : `<line x1="${xDown}" x2="${xDown}" y1="${pad.t}" y2="${pad.t + rows * cellH}" class="gexBoundary gexBoundary--down"></line>
        <text x="${xDown + 6}" y="${pad.t + 10}" class="gexBoundaryLabel">Downside acceleration boundary</text>`}
      ${xUp === null ? "" : `<line x1="${xUp}" x2="${xUp}" y1="${pad.t}" y2="${pad.t + rows * cellH}" class="gexBoundary gexBoundary--up"></line>
        <text x="${xUp + 6}" y="${pad.t + 22}" class="gexBoundaryLabel">Upside acceleration boundary</text>`}
      ${mat.map((row, r) => {
        const rr = Array.isArray(row) ? row : [];
        return rr.map((v, c) => {
          const x = xForCol(c);
          const y = yForRow(r);
          const fill = colorFor(v);
          return `<rect class="gexCell" data-r="${r}" data-c="${c}" x="${x}" y="${y}" width="${cellW}" height="${cellH - 1}" rx="2" ry="2" fill="${fill}"></rect>`;
        }).join("");
      }).join("")}
    </svg>
  `;

  const svg = wrap.querySelector("svg");
  if (!svg) return;

  svg.addEventListener("mouseleave", () => hideTip());
  svg.addEventListener("mousemove", (ev) => {
    const box = svg.getBoundingClientRect();
    const mx = ev.clientX - box.left;
    const my = ev.clientY - box.top;
    const col = Math.floor((mx - pad.l) / cellW);
    const row = Math.floor((my - pad.t) / cellH);
    if (row < 0 || row >= rows || col < 0 || col >= cols) {
      hideTip();
      return;
    }
    const rowInfo = rowMeta[row] || {};
    const rowLabel = yLabels[row];
    const strike = strikes[col];
    const v = (Array.isArray(mat[row]) ? mat[row][col] : null);
    const vNum = Number(v);
    const valTxt = Number.isFinite(vNum) ? e1FmtMoneyShort(vNum) : "—";
    const eff = rowInfo?.effectiveDte;
    const emPts = rowInfo?.expectedMovePts;
    const extra = (eff !== undefined && eff !== null) ? `effectiveDTE=${escapeHtml(String(eff))} · EM=${escapeHtml(String(emPts ?? "—"))} pts` : "";
    const html = `
      <div class="chartTipTitle">${escapeHtml(engine1GexState.mode === "slope" ? "GEX slope (Δ per strike)" : "Net $GEX")}</div>
      <div class="chartTipBody mono">${escapeHtml(String(rowLabel))} · strike ${escapeHtml(fmt0(strike))}</div>
      <div class="chartTipDivider"></div>
      <div class="chartTipBody mono">${escapeHtml(valTxt)}</div>
      ${extra ? `<div class="chartTipBody muted">${extra}</div>` : ""}
      <div class="chartTipBody muted">spot=${escapeHtml(Number.isFinite(spot) ? e1FmtNum(spot, 2) : "—")} · band=±${escapeHtml(Number.isFinite(band) ? String(Math.round(band * 100)) : "—")}% · normalization=${Number.isFinite(denom) && denom > 0 ? "on (render-only)" : "off"}</div>
    `;
    showTip(html, ev.clientX, ev.clientY);
  });
}

function renderEngine1GammaMap(payload) {
  const chart = $("e1GammaChart");
  const tip = $("e1GammaTooltip");
  const meta = $("e1GammaMeta");
  const note = $("e1GammaNote");
  if (!chart) return;

  const tkr = String(payload?.ticker || lastEngine1Ticker || "").trim().toUpperCase() || "—";

  // Empty/initial state
  if (!payload || typeof payload !== "object") {
    chart.innerHTML = `<div class="muted" style="padding:14px;">Run Engine 1 to load the map.</div>`;
    if (meta) meta.textContent = "—";
    if (note) note.textContent = "—";
    if (tip) tip.classList.add("hidden");
    return;
  }

  const series = Array.isArray(payload?.priceSeries) ? payload.priceSeries : [];
  const levels = payload?.levels || {};
  const enabled = !!levels?.enabled;

  const expiry = levels?.expiry ? String(levels.expiry).slice(0, 10) : "—";
  const spot = Number(levels?.spot);
  const sym = levels?.symbolUsed ? String(levels.symbolUsed) : "—";
  const bandPct = Number(levels?.bandPct);

  if (meta) {
    const b = Number.isFinite(bandPct) ? `${Math.round(bandPct * 100)}%` : "—";
    meta.textContent = `expiry=${expiry} · spot=${Number.isFinite(spot) ? e1FmtNum(spot, 2) : "—"} · band=±${b} · src=${sym}`;
  }

  const notes = Array.isArray(levels?.notes) ? levels.notes.filter(Boolean) : [];
  const warns = Array.isArray(levels?.warnings) ? levels.warnings.filter(Boolean) : [];
  if (note) note.textContent = notes[0] || (warns[0] || "Live, informational only.");

  if (!enabled || !series.length) {
    const msg = !series.length ? "No price series returned." : "Live levels unavailable (missing live chain).";
    chart.innerHTML = `<div class="muted" style="padding:14px;">${escapeHtml(msg)}</div>`;
    if (tip) tip.classList.add("hidden");
    return;
  }

  // --- Build overlay items from backend payload ---
  const oi = levels?.oiClusters || {};
  const dg = levels?.dealerGamma || {};
  const flip = Number(levels?.gammaFlipStrike);

  const overlayLines = [];

  const putWall = oi?.putWall;
  const callWall = oi?.callWall;
  if (engine1GammaState.layers.putWall && putWall && Number.isFinite(Number(putWall?.peakStrike ?? putWall?.centerStrike))) {
    const y = Number(putWall?.peakStrike ?? putWall?.centerStrike);
    overlayLines.push({ kind: "putWall", y, title: "Put wall", detail: `strike ${fmt0(y)} · totalOI ${fmt0(putWall?.totalOI)} · range ${fmt0(putWall?.minStrike)}–${fmt0(putWall?.maxStrike)}` });
  }
  if (engine1GammaState.layers.callWall && callWall && Number.isFinite(Number(callWall?.peakStrike ?? callWall?.centerStrike))) {
    const y = Number(callWall?.peakStrike ?? callWall?.centerStrike);
    overlayLines.push({ kind: "callWall", y, title: "Call wall", detail: `strike ${fmt0(y)} · totalOI ${fmt0(callWall?.totalOI)} · range ${fmt0(callWall?.minStrike)}–${fmt0(callWall?.maxStrike)}` });
  }

  if (engine1GammaState.layers.clusters) {
    const mk = (c, side) => {
      const peak = Number(c?.peakStrike ?? c?.centerStrike);
      const lo = Number(c?.minStrike);
      const hi = Number(c?.maxStrike);
      const total = Number(c?.totalOI);
      const sideLabel = side === "P" ? "Put cluster" : "Call cluster";
      const detail = `peak ${fmt0(peak)} · totalOI ${fmt0(total)} · band ${fmt0(lo)}–${fmt0(hi)} · n ${fmt0(c?.nStrikes)}`;
      if (Number.isFinite(lo)) overlayLines.push({ kind: "cluster", y: lo, title: sideLabel, detail });
      if (Number.isFinite(hi)) overlayLines.push({ kind: "cluster", y: hi, title: sideLabel, detail });
    };
    (Array.isArray(oi?.putClusters) ? oi.putClusters : []).slice(0, 3).forEach(c => mk(c, "P"));
    (Array.isArray(oi?.callClusters) ? oi.callClusters : []).slice(0, 3).forEach(c => mk(c, "C"));
  }

  if (engine1GammaState.layers.gammaPeaks) {
    const tops = Array.isArray(dg?.topGammaStrikes) ? dg.topGammaStrikes : [];
    tops.slice(0, 5).forEach((tt) => {
      const y = Number(tt?.strike);
      if (!Number.isFinite(y)) return;
      const side = String(tt?.side || "");
      const title = "Gamma peak";
      const detail = `strike ${fmt0(y)} · side ${escapeHtml(side)} · gex ${fmt0(tt?.gex)}`;
      overlayLines.push({ kind: "gammaPeak", y, title, detail });
    });
  }

  if (engine1GammaState.layers.gammaFlip && Number.isFinite(flip)) {
    overlayLines.push({ kind: "gammaFlip", y: flip, title: "Gamma flip", detail: `~${fmt0(flip)} (best-effort proxy)` });
  }

  // --- Render SVG chart ---
  const w = Math.max(320, chart.clientWidth || 640);
  const h = 260;
  const pad = { l: 10, r: 10, t: 10, b: 10 };
  const pw = w - pad.l - pad.r;
  const ph = h - pad.t - pad.b;

  const closes = series.map(p => Number(p?.close)).filter(Number.isFinite);
  const lvlYs = overlayLines.map(o => Number(o?.y)).filter(Number.isFinite);
  let yMin = Math.min(...closes, ...(lvlYs.length ? lvlYs : [Number.POSITIVE_INFINITY]));
  let yMax = Math.max(...closes, ...(lvlYs.length ? lvlYs : [Number.NEGATIVE_INFINITY]));
  if (!Number.isFinite(yMin) || !Number.isFinite(yMax) || yMin === yMax) {
    yMin = (Number.isFinite(spot) ? spot * 0.98 : 0);
    yMax = (Number.isFinite(spot) ? spot * 1.02 : 1);
  }
  const yPad = (yMax - yMin) * 0.06;
  yMin -= yPad;
  yMax += yPad;

  const xForIdx = (i) => pad.l + (pw * (i / Math.max(1, series.length - 1)));
  const yForVal = (v) => pad.t + (ph * (1 - ((v - yMin) / (yMax - yMin))));

  const pts = series.map((p, i) => {
    const y = yForVal(Number(p?.close));
    return `${xForIdx(i)},${y}`;
  }).join(" ");

  chart.innerHTML = `
    <svg class="gammaSvg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="Close chart">
      <rect x="0" y="0" width="${w}" height="${h}" class="gammaBg"></rect>
      <polyline points="${pts}" class="gammaPrice"></polyline>
      ${overlayLines.map((o, idx) => {
        const y = yForVal(Number(o.y));
        return `<line x1="${pad.l}" x2="${w - pad.r}" y1="${y}" y2="${y}" class="gammaLine gammaLine--${escapeHtml(o.kind)}" data-idx="${idx}"></line>`;
      }).join("")}
      <line x1="${pad.l}" x2="${pad.l}" y1="${pad.t}" y2="${h - pad.b}" class="gammaCross gammaCross--v hidden"></line>
      <line x1="${pad.l}" x2="${w - pad.r}" y1="${pad.t}" y2="${pad.t}" class="gammaCross gammaCross--h hidden"></line>
      <circle cx="${pad.l}" cy="${pad.t}" r="3" class="gammaDot hidden"></circle>
    </svg>
  `;

  const svg = chart.querySelector("svg");
  if (!svg) return;
  const vLine = svg.querySelector(".gammaCross--v");
  const hLine = svg.querySelector(".gammaCross--h");
  const dot = svg.querySelector(".gammaDot");

  const clearHover = () => {
    const lines = Array.from(svg.querySelectorAll(".gammaLine"));
    lines.forEach(l => l.classList.remove("isHover"));
    if (tip) tip.classList.add("hidden");
  };

  const showTip = (html, x, y) => {
    if (!tip) return;
    tip.innerHTML = html;
    tip.classList.remove("hidden");
    const box = chart.getBoundingClientRect();
    const left = e1Clamp(x - box.left + 12, 8, box.width - 240);
    const top = e1Clamp(y - box.top + 12, 8, box.height - 120);
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
  };

  svg.addEventListener("mouseleave", () => {
    clearHover();
    [vLine, hLine, dot].forEach(el => el && el.classList.add("hidden"));
  });

  svg.addEventListener("mousemove", (ev) => {
    const box = svg.getBoundingClientRect();
    const mx = ev.clientX - box.left;
    const my = ev.clientY - box.top;
    const inPlot = (mx >= pad.l && mx <= (w - pad.r) && my >= pad.t && my <= (h - pad.b));
    if (!inPlot) {
      clearHover();
      [vLine, hLine, dot].forEach(el => el && el.classList.add("hidden"));
      return;
    }

    const idx = Math.round(((mx - pad.l) / pw) * (series.length - 1));
    const i = e1Clamp(idx, 0, series.length - 1);
    const pt = series[Number(i)] || {};
    const px = xForIdx(Number(i));
    const py = yForVal(Number(pt?.close));

    if (vLine) {
      vLine.setAttribute("x1", String(px));
      vLine.setAttribute("x2", String(px));
      vLine.classList.remove("hidden");
    }
    if (hLine) {
      hLine.setAttribute("y1", String(py));
      hLine.setAttribute("y2", String(py));
      hLine.classList.remove("hidden");
    }
    if (dot) {
      dot.setAttribute("cx", String(px));
      dot.setAttribute("cy", String(py));
      dot.classList.remove("hidden");
    }

    const lines = Array.from(svg.querySelectorAll(".gammaLine"));
    lines.forEach(l => l.classList.remove("isHover"));

    let bestIdx = null;
    let bestDist = null;
    overlayLines.forEach((o, j) => {
      const yy = yForVal(Number(o.y));
      const d = Math.abs(yy - my);
      if (d <= 6 && (bestDist === null || d < bestDist)) {
        bestDist = d;
        bestIdx = j;
      }
    });

    const priceHtml = `
      <div class="chartTipTitle">${escapeHtml(tkr)}</div>
      <div class="chartTipBody mono">${escapeHtml(e1FmtDateShort(pt?.date))} · ${escapeHtml(e1FmtNum(pt?.close, 2))}</div>
    `;

    if (bestIdx !== null) {
      const o = overlayLines[bestIdx];
      const lineEl = svg.querySelector(`.gammaLine[data-idx="${bestIdx}"]`);
      if (lineEl) lineEl.classList.add("isHover");
      const html = `
        ${priceHtml}
        <div class="chartTipDivider"></div>
        <div class="chartTipTitle">${escapeHtml(o.title)}</div>
        <div class="chartTipBody">${escapeHtml(o.detail)}</div>
      `;
      showTip(html, ev.clientX, ev.clientY);
    } else {
      showTip(priceHtml, ev.clientX, ev.clientY);
    }
  });
}

function pill(text, kind) {
  const cls = kind ? `pill ${kind}` : "pill";
  return `<span class="${cls}">${escapeHtml(text)}</span>`;
}

let lastPayload = null;
let isBusy = false;
let earningsExpanded = false;
let bufferModePref = null; // "symmetric" | "asymmetric" | null (auto)
let showAdvancedCols = false;
let tradeBuilderExpanded = false;
let mcEnabledPref = false;
let mcEventOverride = { date: null, timing: "AUTO" };

const tradeBuilderState = {
  mode: "auto", // auto | equal_delta | equal_premium
  symmetry: "auto", // auto | symmetric | manual
  target_delta: 0.10,
  target_premium: 0.50,
  wing_width: 5,
  put_mult: null,
  call_mult: null,
};

const NEAR_BREACH_THRESHOLD = 0.9;

// -----------------------------------------------------------------------------
// PERFORMANCE OPTIMIZATION: Client-side response cache with stale-while-revalidate
// This avoids re-fetching data the user has already seen.
// -----------------------------------------------------------------------------
const _apiCache = new Map();
const API_CACHE_TTL_MS = 5 * 60 * 1000; // 5 minutes fresh TTL
const API_CACHE_STALE_TTL_MS = 30 * 60 * 1000; // 30 minutes stale-but-usable TTL

function _getCacheKey(url) {
  // Normalize URL for consistent caching
  return url.split("?")[0] + "?" + new URLSearchParams(url.split("?")[1] || "").toString();
}

function _getCached(url) {
  const key = _getCacheKey(url);
  const entry = _apiCache.get(key);
  if (!entry) return null;
  const age = Date.now() - entry.ts;
  return {
    data: entry.data,
    isFresh: age < API_CACHE_TTL_MS,
    isStale: age < API_CACHE_STALE_TTL_MS,
  };
}

function _setCache(url, data) {
  const key = _getCacheKey(url);
  _apiCache.set(key, { data, ts: Date.now() });
  // Prune old entries (keep cache size bounded)
  if (_apiCache.size > 100) {
    const oldest = [..._apiCache.entries()].sort((a, b) => a[1].ts - b[1].ts);
    for (let i = 0; i < 20; i++) {
      _apiCache.delete(oldest[i][0]);
    }
  }
}

async function fetchJson(url) {
  const res = await fetch(url, { headers: { "Accept": "application/json" } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = body?.detail || `Request failed (${res.status})`;
    throw new Error(msg);
  }
  // Cache successful responses
  _setCache(url, body);
  return body;
}

/**
 * Fetch with cache-first strategy for instant perceived performance.
 * Returns { data, fromCache, isRefreshing } 
 * If cached data exists, returns it immediately and optionally refreshes in background.
 */
async function fetchJsonCached(url, { forceRefresh = false, onRefresh = null } = {}) {
  const cached = _getCached(url);
  
  // If we have fresh cached data and not forcing refresh, return it
  if (cached?.isFresh && !forceRefresh) {
    return { data: cached.data, fromCache: true, isRefreshing: false };
  }
  
  // If we have stale cached data, return it immediately but refresh in background
  if (cached?.isStale && !forceRefresh) {
    // Background refresh
    fetchJson(url).then((freshData) => {
      if (onRefresh) onRefresh(freshData);
    }).catch(() => {});
    
    return { data: cached.data, fromCache: true, isRefreshing: true };
  }
  
  // No cache or force refresh: fetch fresh data
  const data = await fetchJson(url);
  return { data, fromCache: false, isRefreshing: false };
}

function recBadge(rec) {
  const r = String(rec || "");
  if (r === "Tight") return pill("Tight", "good");
  if (r === "Standard") return pill("Standard", "neutral");
  if (r === "Wide") return pill("Wide", "warn");
  if (r.startsWith("Avoid")) return pill(r, "bad");
  return escapeHtml(r || "—");
}

function regimeBadge(label, tm) {
  const l = String(label || "");
  const mult = (tm === null || tm === undefined) ? null : `${Number(tm).toFixed(2)}×`;
  let badge = null;
  if (l === "Stress") badge = pill("Stress", "bad");
  else if (l === "Elevated") badge = pill("Elevated", "warn");
  else if (l === "Calm") badge = pill("Calm", "neutral");
  else if (l === "Normal") badge = pill("Normal", "neutral");
  else badge = "—";
  const multSpan = mult ? `<span class="regimeMult mono">${escapeHtml(mult)}</span>` : "";
  return `<span class="regimeCellInner">${badge}${multSpan}</span>`;
}

function gateBadge(gate) {
  const g = String(gate || "");
  if (g === "NO_TRADE") return pill("No Trade", "bad");
  if (g === "CAUTION") return pill("Caution", "warn");
  if (g === "OK") return pill("OK", "neutral");
  return "—";
}

function fmtSignedPP(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const n = Number(v);
  const sign = n > 0 ? "+" : n < 0 ? "" : "";
  return `${sign}${n.toFixed(2)}pp`;
}

function deltaClass(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "";
  const n = Number(v);
  if (n > 0) return "pos";
  if (n < 0) return "neg";
  return "zero";
}

function _normRec(rec) {
  if (!rec) return null;
  const r = String(rec);
  if (r.startsWith("Avoid")) return "Avoid";
  return r;
}

function _fmtQuarterList(qs) {
  const order = ["Q1", "Q2", "Q3", "Q4"];
  const idx = qs.map(q => order.indexOf(q)).filter(i => i >= 0).sort((a, b) => a - b);
  const uniq = [...new Set(idx)].map(i => order[i]);
  if (uniq.length === 0) return "";
  if (uniq.length === 1) return uniq[0];
  // range if contiguous; else comma-separated
  const isContig = uniq.every((q, i) => i === 0 || order.indexOf(q) === order.indexOf(uniq[i - 1]) + 1);
  if (isContig) return `${uniq[0]}–${uniq[uniq.length - 1]}`;
  return uniq.join(", ");
}

function buildActionSummary(quarters) {
  const qs = quarters || {};
  const groups = { Tight: [], Standard: [], Wide: [], Avoid: [] };
  for (const q of ["Q1", "Q2", "Q3", "Q4"]) {
    const rec = _normRec(qs?.[q]?.recommendation);
    if (!rec) continue;
    if (groups[rec]) groups[rec].push(q);
  }

  const parts = [];
  if (groups.Tight.length) parts.push(`Tight wings favored in ${_fmtQuarterList(groups.Tight)}`);
  if (groups.Standard.length) parts.push(`Standard in ${_fmtQuarterList(groups.Standard)}`);
  if (groups.Wide.length) parts.push(`Wider wings in ${_fmtQuarterList(groups.Wide)}`);
  if (groups.Avoid.length) parts.push(`Avoid ${_fmtQuarterList(groups.Avoid)} due to tail risk`);
  return parts.length ? parts.join(" · ") : "—";
}

function _quarterFromDateStr(dateStr) {
  const s = String(dateStr || "");
  const m = Number(s.slice(5, 7));
  if (!m || Number.isNaN(m)) return null;
  const q = Math.floor((m - 1) / 3) + 1;
  return `Q${q}`;
}

function _baseWingFactorFromRec(rec) {
  const r = (String(rec || "Standard").startsWith("Avoid")) ? "Avoid" : String(rec || "Standard");
  if (r === "Tight") return 0.5;
  if (r === "Standard") return 1.0;
  if (r === "Wide") return 1.5;
  return null;
}

function buildBufferTarget(payload) {
  const rg = payload?.regime || {};
  const gate = rg?.guidance?.tradeGate || "OK";
  const asOf = rg?.asOfDate;
  const qk = _quarterFromDateStr(asOf) || null;
  const qRecRaw = qk ? payload?.quarters?.[qk]?.recommendation : null;
  const qRecStr = String(qRecRaw || "");
  const isAvoid = qRecStr.startsWith("Avoid");

  // Muted / no-trade display rules.
  if (gate === "NO_TRADE" || isAvoid) {
    return { primary: "—", secondary: null, subtext: "No trade", muted: true };
  }

  // Canonical "final" multiplier for strike selection is the model's base wing multiple
  // (quarter seasonality × regime tail multiplier). Asymmetry (put/call) is handled
  // separately via wingRecommendation.putWingMultiple / callWingMultiple.
  const wr = payload?.wingRecommendation || null;
  const baseWing = (wr && wr.baseWingMultiple !== null && wr.baseWingMultiple !== undefined)
    ? Number(wr.baseWingMultiple)
    : null;
  if (baseWing !== null && Number.isFinite(baseWing)) {
    return {
      primary: `~${baseWing.toFixed(2)}× EM`,
      secondary: null,
      subtext: "Symmetric base wing (quarter × regime).",
      muted: false,
    };
  }

  // Fallback if wingRecommendation is missing/incomplete: compute from quarter+regime inputs.
  const base = _baseWingFactorFromRec(qRecRaw);
  const tm = (rg.tailMultiplier === null || rg.tailMultiplier === undefined) ? null : Number(rg.tailMultiplier);
  if (base === null || tm === null || Number.isNaN(tm)) {
    return { primary: "—", secondary: null, subtext: "Unavailable: missing quarter/regime inputs.", muted: false };
  }
  const finalWing = Number(base) * Number(tm);
  if (!Number.isFinite(finalWing)) {
    return { primary: "—", secondary: null, subtext: "Unavailable: invalid quarter/regime inputs.", muted: false };
  }
  return {
    primary: `~${finalWing.toFixed(2)}× EM`,
    secondary: null,
    subtext: "Symmetric base wing (quarter × regime).",
    muted: false,
  };
}

function _wingRecLabel(label) {
  const l = String(label || "");
  if (l === "WIDEN_PUTS_TIGHTEN_CALLS") return "Widen puts / tighten calls";
  if (l === "WIDEN_CALLS_TIGHTEN_PUTS") return "Widen calls / tighten puts";
  if (l === "SYMMETRIC") return "Symmetric";
  if (l === "NO_TRADE") return "No trade (gate)";
  return l || "—";
}

function renderBufferTarget(payload) {
  const wing = payload?.wingRecommendation || null;
  const hasAsymNumbers = wing && wing.putWingMultiple !== null && wing.putWingMultiple !== undefined
    && wing.callWingMultiple !== null && wing.callWingMultiple !== undefined;
  const canDefaultAsym = hasAsymNumbers && String(wing.confidence || "") !== "LOW";

  const symBtn = $("bufferModeSymmetric");
  const asymBtn = $("bufferModeAsymmetric");
  if (asymBtn) asymBtn.disabled = !hasAsymNumbers;

  let mode = bufferModePref;
  if (!mode) mode = canDefaultAsym ? "asymmetric" : "symmetric";
  if (mode === "asymmetric" && !hasAsymNumbers) mode = "symmetric";

  if (symBtn) symBtn.classList.toggle("isActive", mode === "symmetric");
  if (asymBtn) asymBtn.classList.toggle("isActive", mode === "asymmetric");

  const bt = buildBufferTarget(payload);
  const btGrid = $("bufferTargetGrid");
  const btSub = $("bufferTargetSubtext");
  const btCard = $("bufferTargetCard");

  if (btGrid) {
    if (bt.muted) {
      btGrid.innerHTML = `<div class="k">Target</div><div class="v mono">—</div>`;
    } else if (mode === "asymmetric" && hasAsymNumbers) {
      btGrid.innerHTML = `
        <div class="k">Put</div><div class="v mono">~${Number(wing.putWingMultiple).toFixed(2)}× EM</div>
        <div class="k">Call</div><div class="v mono">~${Number(wing.callWingMultiple).toFixed(2)}× EM</div>
      `;
      if (btSub) btSub.textContent = "Final wing multipliers (includes TAS asymmetry). Multiply by EM to target short strikes.";
    } else {
      btGrid.innerHTML = `<div class="k">Target</div><div class="v mono">${escapeHtml(bt.primary)}</div>`;
      if (btSub) btSub.textContent = "Final symmetric wing multiplier. Multiply by EM to target short strikes.";
    }
  }
  if (btSub && !btSub.textContent) btSub.textContent = bt.subtext;
  if (btCard) btCard.classList.toggle("isMuted", !!bt.muted);
}

function renderSkewWings(payload) {
  const sec = $("skewWingsSection");
  if (!sec) return;

  const s = payload?.summary || {};
  const wr = payload?.wingRecommendation || null;
  const sk = payload?.skewOverlay?.current || null;

  const hasDirectional =
    s.upBreachRatePct !== undefined ||
    s.downBreachRatePct !== undefined ||
    s.avgUpOvershootPct !== undefined ||
    s.avgDownOvershootPct !== undefined;

  const dirCard = $("directionalTailCard");
  const dirGrid = $("directionalTailGrid");
  const dirFoot = $("directionalTailFootnote");

  if (dirCard) dirCard.classList.toggle("hidden", !hasDirectional);
  if (hasDirectional && dirGrid) {
    const tailBias = s.tailBias || "—";
    const biasBadge =
      tailBias === "DOWN" ? pill("DOWN", "bad") : tailBias === "UP" ? pill("UP", "warn") : pill("NEUTRAL", "neutral");
    dirGrid.innerHTML = `
      <div class="k">Down breach rate</div><div class="v mono">${fmtPct(s.downBreachRatePct)}</div>
      <div class="k">Up breach rate</div><div class="v mono">${fmtPct(s.upBreachRatePct)}</div>
      <div class="k">Avg overshoot (Down)</div><div class="v mono">${fmtPct(s.avgDownOvershootPct)}</div>
      <div class="k">Avg overshoot (Up)</div><div class="v mono">${fmtPct(s.avgUpOvershootPct)}</div>
      <div class="k">Tail bias</div><div class="v">${biasBadge}</div>
    `;
  }
  if (hasDirectional && dirFoot) {
    dirFoot.textContent = `Based on usable events (n=${s.events_used ?? "—"}).`;
  }

  const skewCard = $("skewSnapshotCard");
  const skewGrid = $("skewSnapshotGrid");
  const skewNotes = $("skewSnapshotNotes");
  const hasSkew = !!sk;
  if (skewCard) skewCard.classList.toggle("hidden", !hasSkew);
  if (hasSkew && skewGrid) {
    const q = String(sk.skewQuality || "MISSING");
    const qBadge =
      q === "OK" ? pill("OK", "good") : q === "PARTIAL" ? pill("PARTIAL", "warn") : pill("MISSING", "neutral");
    const rr25 = sk.rr25;
    const rrLabel = rr25 === null || rr25 === undefined || Number.isNaN(rr25) ? "—" : `${Number(rr25).toFixed(4)}`;
    const rrHint = rr25 < 0 ? "Put skew (downside priced)" : rr25 > 0 ? "Call skew (upside priced)" : "—";
    skewGrid.innerHTML = `
      <div class="k">RR25</div><div class="v mono">${escapeHtml(rrLabel)}</div>
      <div class="k">As of</div><div class="v mono">${escapeHtml(sk.asOfDate || "—")}</div>
      <div class="k">Quality</div><div class="v">${qBadge}</div>
      <div class="k">Read</div><div class="v muted">${escapeHtml(rrHint)}</div>
    `;
  }
  if (hasSkew && skewNotes) {
    if (String(sk.skewQuality || "") === "MISSING") skewNotes.textContent = sk.notes || "Skew unavailable";
    else skewNotes.textContent = sk.notes || "—";
  }

  const wingCard = $("wingBuilderCard");
  const wingGrid = $("wingBuilderGrid");
  const wingWhy = $("wingBuilderRationale");
  const hasWing = !!wr;

  if (wingCard) wingCard.classList.toggle("hidden", !hasWing);
  if (hasWing && wingGrid) {
    const conf = String(wr.confidence || "—");
    const confBadge =
      conf === "HIGH" ? pill("HIGH", "good") : conf === "MED" ? pill("MED", "warn") : pill("LOW", "neutral");
    const mode = String(wr.structureMode || "");
    const modeTxt =
      mode === "AUTO_EQUAL_PREMIUM" ? "Auto: Equal Premium" : mode === "AUTO_EQUAL_DELTA" ? "Auto: Equal Delta" : (mode || "—");
    wingGrid.innerHTML = `
      <div class="k">Recommendation</div><div class="v">${escapeHtml(_wingRecLabel(wr.recommendationLabel))}</div>
      <div class="k">Structure mode</div><div class="v">${escapeHtml(modeTxt)}</div>
      <div class="k">Confidence</div><div class="v">${confBadge}</div>
      <div class="k">TAS</div><div class="v mono">${wr.tas !== null && wr.tas !== undefined ? Number(wr.tas).toFixed(3) : "—"}</div>
      <div class="k">Base wing</div><div class="v mono">${wr.baseWingMultiple !== null && wr.baseWingMultiple !== undefined ? `${Number(wr.baseWingMultiple).toFixed(2)}× EM` : "—"}</div>
      <div class="k">Put wing</div><div class="v mono">${wr.putWingMultiple !== null && wr.putWingMultiple !== undefined ? `${Number(wr.putWingMultiple).toFixed(2)}× EM` : "—"}</div>
      <div class="k">Call wing</div><div class="v mono">${wr.callWingMultiple !== null && wr.callWingMultiple !== undefined ? `${Number(wr.callWingMultiple).toFixed(2)}× EM` : "—"}</div>
    `;
  }

  const gate = (payload?.regime?.guidance || {})?.tradeGate;
  const isNoTrade = gate === "NO_TRADE";
  if (wingCard) wingCard.classList.toggle("isMuted", isNoTrade);
  if (hasWing && wingWhy) {
    if (isNoTrade) wingWhy.textContent = "No Trade (Regime Gate).";
    else wingWhy.textContent = (wr.structureRationale || wr.rationale || "—");
  }

  const meta = $("skewWingsMeta");
  if (meta) meta.textContent = (hasDirectional || hasWing || hasSkew) ? "Directional tails + skew snapshot + wing multipliers" : "—";

  // Hide entire section if nothing to show (graceful degradation).
  const showSection = hasDirectional || hasWing || hasSkew;
  sec.classList.toggle("hidden", !showSection);
}

function renderMonteCarlo(payload) {
  const sec = $("mcSection");
  if (!sec) return;
  const mc = payload?.monteCarlo || null;
  const ne = payload?.nextEvent || null;
  const opt = payload?.monteCarloOptimization || null;

  const hasAnyMc = !!mc;
  const hasMc = !!(mc && mc.nSims && Number(mc.nSims) > 0);
  const show = mcEnabledPref || hasAnyMc;
  sec.classList.toggle("hidden", !show);
  if (!show) return;

  const meta = $("mcMeta");
  const note = $("mcNote");
  const bEither = $("mcBreachEither");
  const bBreak = $("mcBreachBreakout");
  const expLoss = $("mcExpLoss");
  const cvar = $("mcCvar");
  const wingOpt = $("mcWingOpt");
  const wingOptNote = $("mcWingOptNote");

  const nSims = hasAnyMc ? Number(mc.nSims || 0) : 0;
  const cond = hasAnyMc ? (mc?.pool?.conditioningUsed || "—") : "—";
  const poolN = hasAnyMc ? (mc?.pool?.sizeUsed ?? "—") : "—";

  const anchorDate = ne?.pricingDatePlanned || "—";
  const anchorImp = ne?.impliedMovePctPlanned;
  const anchorTxt = (anchorImp !== null && anchorImp !== undefined) ? `${Number(anchorImp).toFixed(2)}%` : "—";

  const src = ne?.source ? String(ne.source) : null;
  const conf = ne?.confidence ? String(ne.confidence) : null;
  const prov = src ? ` · src=${src}${conf ? `/${conf}` : ""}` : "";
  if (meta) meta.textContent = hasAnyMc ? `n=${nSims} · conditioning=${cond} · pool=${poolN} · anchor=${anchorDate} (${anchorTxt})${prov}` : "MC requested (waiting for data)…";

  const notes = [];
  if (hasAnyMc && Array.isArray(mc.notes)) notes.push(...mc.notes);
  if (ne && Array.isArray(ne.notes) && ne.notes.length) notes.push(...ne.notes.map(s => `Next event: ${s}`));
  if (ne && (ne.rawTime || ne.dateConfirmed !== null && ne.dateConfirmed !== undefined)) {
    const rt = ne.rawTime ? `time=${ne.rawTime}` : null;
    const dc = (ne.dateConfirmed === true) ? "date_confirmed=1" : (ne.dateConfirmed === false) ? "date_confirmed=0" : null;
    const bits = [rt, dc].filter(Boolean).join(", ");
    if (bits) notes.push(`Next event meta: ${bits}.`);
  }
  if (!hasAnyMc) notes.push("MC unavailable: server did not return monteCarlo output. Ensure MC toggle is on and backend supports mc=1.");
  if (note) note.textContent = notes.length ? notes.join(" ") : "Simulates close→open earnings gap only (no intraday path).";

  if (!hasMc) {
    if (bEither) bEither.textContent = "—";
    if (bBreak) bBreak.textContent = "Put — · Call —";
    if (expLoss) expLoss.textContent = "—";
    if (cvar) cvar.textContent = "—";
    if (wingOpt) wingOpt.textContent = "—";
    if (wingOptNote) wingOptNote.textContent = "MC enabled, but no simulation results available.";
    return;
  }

  const bp = mc?.breachProb || {};
  const either = Number(bp.either);
  const put = Number(bp.put);
  const call = Number(bp.call);
  function fmtProbPct(p) {
    if (!Number.isFinite(p)) return "—";
    const pct = p * 100;
    // If we observed zero breaches, show an empirical upper bound if available.
    if (pct === 0) {
      const ub = mc?.diagnostics?.breachProbUpperBoundPct;
      const ubn = Number(ub);
      if (Number.isFinite(ubn) && ubn > 0) return `<${ubn.toFixed(2)}%`;
    }
    if (pct > 0 && pct < 0.1) return "<0.1%";
    return `${pct.toFixed(1)}%`;
  }
  if (bEither) bEither.textContent = fmtProbPct(either);
  if (bBreak) bBreak.textContent = `Put ${fmtProbPct(put)} · Call ${fmtProbPct(call)}`;

  const el = mc?.expectedLoss || {};
  if (expLoss) expLoss.textContent = fmtMoney(el.total);

  const cv = mc?.cvar95 || {};
  if (cvar) cvar.textContent = fmtMoney(cv.total);

  if (!opt) {
    if (wingOpt) wingOpt.textContent = "—";
    if (wingOptNote) wingOptNote.textContent = "Optimization not enabled.";
    return;
  }

  const mode = String(opt.mode || "");
  const pm = opt.optimalPutMultiple;
  const cm = opt.optimalCallMultiple;
  if (pm !== null && pm !== undefined && cm !== null && cm !== undefined) {
    if (wingOpt) wingOpt.textContent = `Put ${Number(pm).toFixed(2)}× · Call ${Number(cm).toFixed(2)}×`;
  } else {
    if (wingOpt) wingOpt.textContent = "—";
  }
  const optNotes = Array.isArray(opt.notes) ? opt.notes.join(" ") : "";
  if (wingOptNote) wingOptNote.textContent = mode ? `${mode}. ${optNotes}` : (optNotes || "—");
}

function _eventRiskBadge(label) {
  const l = String(label || "");
  if (l === "HIGH") return pill("HIGH", "bad");
  if (l === "MED") return pill("MED", "warn");
  if (l === "LOW") return pill("LOW", "neutral");
  return escapeHtml(l || "—");
}

function renderEventRisk(payload) {
  const sec = $("eventRiskSection");
  if (!sec) return;
  const er = payload?.eventRisk || null;
  const show = !!(er && (er.enabled || er.score01 !== null && er.score01 !== undefined));
  sec.classList.toggle("hidden", !show);
  if (!show) return;

  const meta = $("eventRiskMeta");
  const scoreEl = $("eventRiskScore");
  const labelEl = $("eventRiskLabel");
  const driversEl = $("eventRiskDrivers");
  const notesEl = $("eventRiskNotes");
  const impactEl = $("eventRiskImpact");
  const impactNotesEl = $("eventRiskImpactNotes");
  const explainEl = $("eventRiskExplain");

  const asOf = er?.asOfDate || "—";
  const windowAnchor = er?.windowAnchorDate || asOf || "—";
  const earn = er?.earnDateNext || "—";
  const w = er?.window || null;
  const wTxt = w ? `${w.start || "—"}→${w.end || "—"}` : "—";
  if (meta) meta.textContent = `asOf=${asOf} · today=${windowAnchor} · earn=${earn} · window=${wTxt}`;

  const s = (er?.score01 === null || er?.score01 === undefined) ? null : Number(er.score01);
  if (scoreEl) scoreEl.textContent = (s !== null && Number.isFinite(s)) ? `${s.toFixed(3)}` : "—";
  if (labelEl) {
    const badge = _eventRiskBadge(er?.label);
    labelEl.innerHTML = `${badge} <span class="eventRiskMuted">Low &lt;0.33 · Med &lt;0.66 · High ≥0.66</span>`;
  }
  if (explainEl) {
    explainEl.textContent = "Composite score (0–1) summarizing near-term event risk around the earnings window. Use it as a guardrail, not a signal.";
  }

  const c = er?.components || {};
  const macro = c?.macroProximity || {};
  const head = c?.headlineShock || {};
  const rat = c?.analystCluster || {};
  const opt = c?.optionsActivity || {};

  const macroN = macro.countHighImpactUS ?? "—";
  const macroTop = Array.isArray(macro.top) ? macro.top : [];

  const newsN = head.newsCount3d ?? "—";
  const wiimN = head.wiimCount3d ?? "—";
  const ratN = rat.ratingsCount7d ?? "—";
  const ratActions = Array.isArray(rat.actions) ? rat.actions.filter(Boolean) : [];
  const optEnabled = (opt && opt.enabled === true);
  const optMode = opt?.mode ? String(opt.mode) : "";
  const optScore = (opt?.score01 === null || opt?.score01 === undefined) ? null : Number(opt.score01);
  const optTop = Array.isArray(opt?.topStrikesByVol) ? opt.topStrikesByVol.filter(Boolean) : [];

  const driverItems = [];
  driverItems.push(`<li><strong>Macro</strong>: ${escapeHtml(String(macroN))} high-impact US events (importance ≥3) in window${macroTop.length ? `.<br/><span class="eventRiskMuted">${escapeHtml(macroTop.join(" · "))}</span>` : ""}</li>`);
  driverItems.push(`<li><strong>News</strong>: ${escapeHtml(String(newsN))} headlines (3d) · <strong>WIIM</strong>: ${escapeHtml(String(wiimN))} (3d)</li>`);
  driverItems.push(`<li><strong>Analyst ratings</strong>: ${escapeHtml(String(ratN))} (7d)${ratActions.length ? `.<br/><span class="eventRiskMuted">${escapeHtml(ratActions.join(" · "))}</span>` : ""}</li>`);
  // Unusual options is optional: show only if backend provided a usable signal/proxy.
  if (optEnabled) {
    const modeTxt = optMode ? ` (${optMode === "orats_live_strikes_proxy" ? "live proxy" : optMode})` : "";
    const scoreTxt = (optScore !== null && Number.isFinite(optScore)) ? optScore.toFixed(3) : "—";
    const extra = optTop.length ? `.<br/><span class="eventRiskMuted">${escapeHtml(optTop.join(" · "))}</span>` : "";
    driverItems.push(`<li><strong>Unusual options</strong>${escapeHtml(modeTxt)}: score ${escapeHtml(scoreTxt)}${extra}</li>`);
  }
  if (driversEl) driversEl.innerHTML = `<ul class="eventRiskList">${driverItems.join("")}</ul>`;

  const notes = [];
  if (Array.isArray(er?.notes) && er.notes.length) notes.push(...er.notes);
  if (Array.isArray(er?.sources) && er.sources.length) notes.push(`Sources: ${er.sources.join(", ")}`);
  if (notesEl) notesEl.textContent = notes.length ? notes.join(" ") : "—";

  // Impact this run: show whether it actually adjusted regime/MC.
  const impacts = [];
  const rgAdj = payload?.regime?.eventRiskAdjustment || null;
  if (rgAdj && rgAdj.enabled) {
    const bump = rgAdj.tailMultiplierBumpPct;
    const gate = payload?.regime?.guidance?.tradeGate || payload?.regime?.tradeGate || "—";
    impacts.push(`Regime: +${Number(bump || 0).toFixed(1)}% tail · gate=${gate}`);
  } else {
    impacts.push("Regime: no adjustment");
  }
  const mc = payload?.monteCarlo || null;
  const mcNotes = Array.isArray(mc?.notes) ? mc.notes.join(" ") : "";
  if (mcNotes.includes("Event-risk widening applied")) impacts.push("MC: wing widening applied");
  else impacts.push("MC: no widening");
  if (impactEl) impactEl.innerHTML = `<div class="eventRiskKicker">${escapeHtml(impacts.join(" · "))}</div>`;

  const impactNotes = [];
  if (rgAdj && rgAdj.enabled && rgAdj.thresholds) {
    impactNotes.push(`Thresholds: caution≥${rgAdj.thresholds.caution} · high≥${rgAdj.thresholds.high}.`);
  }
  if (impactNotesEl) impactNotesEl.textContent = impactNotes.length ? impactNotes.join(" ") : "—";
}

function _bestUnderlyingPrice(payload) {
  const tb = payload?.tradeBuilder;
  if (tb && tb.underlyingPrice !== null && tb.underlyingPrice !== undefined) {
    const p = Number(tb.underlyingPrice);
    if (Number.isFinite(p) && p > 0) return p;
  }
  const cur = payload?.current;
  if (cur && cur.stockPrice !== null && cur.stockPrice !== undefined) {
    const p = Number(cur.stockPrice);
    if (Number.isFinite(p) && p > 0) return p;
  }
  const events = Array.isArray(payload?.events) ? payload.events : [];
  for (const e of events) {
    const px = Number(e?.closePx);
    if (Number.isFinite(px) && px > 0) return px;
  }
  return null;
}

function _bestImpliedMovePct(payload) {
  const cur = payload?.current;
  if (cur && cur.impliedMovePct !== null && cur.impliedMovePct !== undefined) {
    const imp = Number(cur.impliedMovePct);
    if (Number.isFinite(imp) && imp > 0) return imp;
  }
  const events = Array.isArray(payload?.events) ? payload.events : [];
  for (const e of events) {
    const imp = Number(e?.impliedMovePct);
    if (Number.isFinite(imp) && imp > 0) return imp;
  }
  const s = payload?.summary || {};
  const avg = Number(s.avg_implied_all_pct);
  return Number.isFinite(avg) && avg > 0 ? avg : null;
}

function renderTradeBuilder(payload) {
  const panel = $("tradeBuilderPanel");
  if (!panel || panel.classList.contains("hidden")) return;

  const putOut = $("tbPutOut");
  const callOut = $("tbCallOut");
  const notes = $("tradeBuilderNotes");

  const tb = payload?.tradeBuilder || null;
  const hasStrikes = !!(tb?.put?.shortStrike && tb?.call?.shortStrike);

  if (hasStrikes) {
    const put = tb.put || {};
    const call = tb.call || {};
    const price = Number(tb.underlyingPrice);
    const credit = tb.totalCredit;
    if (putOut) {
      putOut.innerHTML = `
        <div class="k">Short strike</div><div class="v mono">${put.shortStrike ?? "—"}</div>
        <div class="k">Long strike</div><div class="v mono">${put.longStrike ?? "—"}</div>
        <div class="k">Short delta</div><div class="v mono">${put.shortDelta !== null && put.shortDelta !== undefined ? Number(put.shortDelta).toFixed(3) : "—"}</div>
        <div class="k">Short mid</div><div class="v mono">${put.shortMid !== null && put.shortMid !== undefined ? `$${Number(put.shortMid).toFixed(2)}` : "—"}</div>
        <div class="k">Wing credit</div><div class="v mono">${put.credit !== null && put.credit !== undefined ? `$${Number(put.credit).toFixed(2)}` : "—"}</div>
      `;
    }
    if (callOut) {
      callOut.innerHTML = `
        <div class="k">Short strike</div><div class="v mono">${call.shortStrike ?? "—"}</div>
        <div class="k">Long strike</div><div class="v mono">${call.longStrike ?? "—"}</div>
        <div class="k">Short delta</div><div class="v mono">${call.shortDelta !== null && call.shortDelta !== undefined ? Number(call.shortDelta).toFixed(3) : "—"}</div>
        <div class="k">Short mid</div><div class="v mono">${call.shortMid !== null && call.shortMid !== undefined ? `$${Number(call.shortMid).toFixed(2)}` : "—"}</div>
        <div class="k">Wing credit</div><div class="v mono">${call.credit !== null && call.credit !== undefined ? `$${Number(call.credit).toFixed(2)}` : "—"}</div>
      `;
    }
    const noteLines = Array.isArray(tb.notes) ? tb.notes : [];
    const head = credit !== null && credit !== undefined ? `Estimated total credit: $${Number(credit).toFixed(2)}.` : "Credit unavailable.";
    const mid = (Number.isFinite(price) && price > 0) ? `Assumed price $${price.toFixed(2)}.` : "";
    if (notes) notes.textContent = [head, mid, `Expiration ${tb.expiration || "—"}.`, ...noteLines].filter(Boolean).join(" ");
    return;
  }

  const price = _bestUnderlyingPrice(payload);
  const impPct = _bestImpliedMovePct(payload);
  const wr = payload?.wingRecommendation || null;
  const gate = (payload?.regime?.guidance || {})?.tradeGate;

  const baseMult = (wr && wr.baseWingMultiple !== null && wr.baseWingMultiple !== undefined) ? Number(wr.baseWingMultiple) : null;
  const recPut = (wr && wr.putWingMultiple !== null && wr.putWingMultiple !== undefined) ? Number(wr.putWingMultiple) : null;
  const recCall = (wr && wr.callWingMultiple !== null && wr.callWingMultiple !== undefined) ? Number(wr.callWingMultiple) : null;

  let putMult = baseMult;
  let callMult = baseMult;
  if (tradeBuilderState.symmetry === "auto") {
    if (recPut !== null && recCall !== null && String(wr?.confidence || "") !== "LOW") {
      putMult = recPut;
      callMult = recCall;
    }
  } else if (tradeBuilderState.symmetry === "symmetric") {
    putMult = baseMult;
    callMult = baseMult;
  } else if (tradeBuilderState.symmetry === "manual") {
    const pm = Number(tradeBuilderState.put_mult);
    const cm = Number(tradeBuilderState.call_mult);
    if (Number.isFinite(pm) && pm > 0) putMult = pm;
    if (Number.isFinite(cm) && cm > 0) callMult = cm;
  }

  if (!price || !impPct || !putMult || !callMult) {
    if (putOut) putOut.innerHTML = `<div class="k">Target</div><div class="v">—</div>`;
    if (callOut) callOut.innerHTML = `<div class="k">Target</div><div class="v">—</div>`;
    if (notes) notes.textContent = "Insufficient data to compute distance targets (need price + implied move + wing multipliers).";
    return;
  }

  const em$ = price * (impPct / 100.0);
  const putDist$ = em$ * putMult;
  const callDist$ = em$ * callMult;
  const width$ = Number(tradeBuilderState.wing_width) || 5;

  const putShort = price - putDist$;
  const putLong = putShort - width$;
  const callShort = price + callDist$;
  const callLong = callShort + width$;

  const putDistPct = (putDist$ / price) * 100.0;
  const callDistPct = (callDist$ / price) * 100.0;

  if (putOut) {
    putOut.innerHTML = `
      <div class="k">Target distance</div><div class="v mono">$${putDist$.toFixed(2)} (${putDistPct.toFixed(2)}%)</div>
      <div class="k">Short strike (est.)</div><div class="v mono">${putShort.toFixed(2)}</div>
      <div class="k">Long strike (width)</div><div class="v mono">${putLong.toFixed(2)} (−$${width$.toFixed(0)})</div>
      <div class="k">Wing multiple used</div><div class="v mono">${putMult.toFixed(2)}× EM</div>
    `;
  }
  if (callOut) {
    callOut.innerHTML = `
      <div class="k">Target distance</div><div class="v mono">$${callDist$.toFixed(2)} (${callDistPct.toFixed(2)}%)</div>
      <div class="k">Short strike (est.)</div><div class="v mono">${callShort.toFixed(2)}</div>
      <div class="k">Long strike (width)</div><div class="v mono">${callLong.toFixed(2)} (+$${width$.toFixed(0)})</div>
      <div class="k">Wing multiple used</div><div class="v mono">${callMult.toFixed(2)}× EM</div>
    `;
  }

  const extra = gate === "NO_TRADE" ? "No Trade (Regime Gate)." : "Chain-based strike selection not enabled; showing distance targets only.";
  const cur = payload?.current || null;
  const src = cur?.source ? `source=${cur.source}` : "";
  const asOf = cur?.asOfDate ? `asOf=${cur.asOfDate}` : "";
  const meta = [src, asOf].filter(Boolean).join(", ");
  if (notes) notes.textContent = `${extra} Assumed price $${price.toFixed(2)}; implied move ${impPct.toFixed(2)}%.${meta ? ` (${meta})` : ""}`;
}

function renderEarningsTable(events) {
  const tbody = $("tbody");
  tbody.innerHTML = "";
  const table = document.querySelector(".dataTable");
  if (table) table.classList.toggle("showAdvanced", !!showAdvancedCols);

  const rows = Array.isArray(events) ? events : [];
  const shown = earningsExpanded ? rows : rows.slice(0, 3);

  for (const e of shown) {
    const breach = e.breach;
    let breachCell = "—";
    if (breach === true) breachCell = `<span class="breachCell"><span class="breachDot breachDot--bad" title="Breach"></span><span class="breachLabel">Breach</span></span>`;
    if (breach === false) breachCell = `<span class="breachCell"><span class="breachDot breachDot--good" title="No breach"></span><span class="breachLabel">No</span></span>`;

    const notes = Array.isArray(e.notes) ? e.notes.join("; ") : "";
    const implied = e.impliedMovePct;
    const realized = e.realizedMovePct;
    const ratio = (implied && implied > 0 && realized !== null && realized !== undefined) ? (Number(realized) / Number(implied)) : null;
    const rge = e.regimeAtEvent || null;
    const regCell = rge ? regimeBadge(rge.label, rge.tailMultiplier) : "—";
    const gateCell = rge ? gateBadge(rge.tradeGate) : "—";

    const tr = document.createElement("tr");
    if (breach === true) tr.classList.add("row--breach");
    else if (ratio !== null && ratio >= NEAR_BREACH_THRESHOLD) tr.classList.add("row--near");

    tr.innerHTML = `
      <td>${escapeHtml(e.earnDate ?? "")}</td>
      <td class="ref">${escapeHtml(e.anncTod ?? "")}</td>
      <td class="ref">${escapeHtml(e.timing ?? "")}</td>
      <td class="ref">${escapeHtml(e.pricingDateUsed ?? "")}</td>
      <td class="num">${escapeHtml(e.impErnMv ?? "")}</td>
      <td class="num">${fmtPct(e.impliedMovePct)}</td>
      <td>${escapeHtml(e.closeDateUsed ?? "")}</td>
      <td class="num">${fmtNum(e.closePx)}</td>
      <td>${escapeHtml(e.openDateUsed ?? "")}</td>
      <td class="num">${fmtNum(e.openPx)}</td>
      <td class="num">${fmtPct(e.realizedMovePct)}</td>
      <td class="num advCol">${fmtSignedPct(e.signedMovePct)}</td>
      <td class="advCol">${escapeHtml(e.breachSide ?? "—")}</td>
      <td class="num advCol">${fmtPct(e.upOvershootPct ?? e.downOvershootPct)}</td>
      <td>${breachCell}</td>
      <td class="regimeCell">${regCell}</td>
      <td class="gateCell">${gateCell}</td>
      <td class="num">${fmtPct(e.aboveBreachPct)}</td>
      <td class="muted">${escapeHtml(notes)}</td>
    `;
    tbody.appendChild(tr);
  }
}

// --- Earnings Hold Risk Panel ---
function renderHoldRisk(payload) {
  const section = $("holdRiskSection");
  if (!section) return;

  const hr = payload?.earningsHoldRisk;
  if (!hr) {
    section.classList.add("hidden");
    return;
  }

  section.classList.remove("hidden");

  // Meta info
  const meta = $("holdRiskMeta");
  if (meta) {
    const parts = [];
    if (hr.lookback) parts.push(hr.lookback.replace("_", " "));
    if (hr.em_source) parts.push(`EM: ${hr.em_source.replace(/_/g, " ")}`);
    meta.textContent = parts.length ? parts.join(" · ") : "—";
  }

  // Helper to format rate
  const fmtRate = (r) => {
    if (r === null || r === undefined) return "—";
    return (Number(r) * 100).toFixed(1) + "%";
  };

  // Helper to get risk class
  const riskClass = (r) => {
    if (r === null || r === undefined) return "";
    const pct = Number(r);
    if (pct <= 0.10) return "holdRiskRow--low";
    if (pct <= 0.25) return "holdRiskRow--med";
    return "holdRiskRow--high";
  };

  // Helper to format deviation
  const fmtDev = (d) => (d === null || d === undefined) ? "—" : `${Number(d).toFixed(2)}× EM`;

  // Render unconditional rates
  const uncondEl = $("holdRiskUnconditional");
  const uncondNoteEl = $("holdRiskUnconditionalNote");
  if (uncondEl) {
    const uncond = hr.unconditional || {};
    const ec = uncond.earnings_close || {};
    const nc = uncond.next_day_close || {};
    const maxDev = uncond.max_observed_deviation || {};
    uncondEl.innerHTML = `
      <div class="holdRiskSubhead">Earnings Close</div>
      <div class="holdRiskRow ${riskClass(ec["1.0"])}"><span class="hrLabel">1.0× EM</span><span class="hrValue">${fmtRate(ec["1.0"])}</span></div>
      <div class="holdRiskRow ${riskClass(ec["1.5"])}"><span class="hrLabel">1.5× EM</span><span class="hrValue">${fmtRate(ec["1.5"])}</span></div>
      <div class="holdRiskRow ${riskClass(ec["2.0"])}"><span class="hrLabel">2.0× EM</span><span class="hrValue">${fmtRate(ec["2.0"])}</span></div>
      <div class="holdRiskSubhead">Next Day Close</div>
      <div class="holdRiskRow ${riskClass(nc["1.0"])}"><span class="hrLabel">1.0× EM</span><span class="hrValue">${fmtRate(nc["1.0"])}</span></div>
      <div class="holdRiskRow ${riskClass(nc["1.5"])}"><span class="hrLabel">1.5× EM</span><span class="hrValue">${fmtRate(nc["1.5"])}</span></div>
      <div class="holdRiskRow ${riskClass(nc["2.0"])}"><span class="hrLabel">2.0× EM</span><span class="hrValue">${fmtRate(nc["2.0"])}</span></div>
      <div class="holdRiskSubhead">Max Observed Deviation</div>
      <div class="holdRiskRow holdRiskRow--maxdev"><span class="hrLabel">Earnings Close</span><span class="hrValue">${fmtDev(maxDev.earnings_close)}</span></div>
      <div class="holdRiskRow holdRiskRow--maxdev"><span class="hrLabel">Next Day Close</span><span class="hrValue">${fmtDev(maxDev.next_day_close)}</span></div>
    `;
  }
  if (uncondNoteEl) {
    const n = hr.sample_size?.unconditional ?? 0;
    uncondNoteEl.textContent = `${n} events`;
  }

  // Render conditional (flat open) rates
  const condEl = $("holdRiskConditional");
  const condNoteEl = $("holdRiskConditionalNote");
  if (condEl) {
    const cond = hr.conditional_flat_open || {};
    const ec = cond.earnings_close || {};
    const nc = cond.next_day_close || {};
    const maxDev = cond.max_observed_deviation || {};
    condEl.innerHTML = `
      <div class="holdRiskSubhead">Earnings Close</div>
      <div class="holdRiskRow ${riskClass(ec["1.0"])}"><span class="hrLabel">1.0× EM</span><span class="hrValue">${fmtRate(ec["1.0"])}</span></div>
      <div class="holdRiskRow ${riskClass(ec["1.5"])}"><span class="hrLabel">1.5× EM</span><span class="hrValue">${fmtRate(ec["1.5"])}</span></div>
      <div class="holdRiskRow ${riskClass(ec["2.0"])}"><span class="hrLabel">2.0× EM</span><span class="hrValue">${fmtRate(ec["2.0"])}</span></div>
      <div class="holdRiskSubhead">Next Day Close</div>
      <div class="holdRiskRow ${riskClass(nc["1.0"])}"><span class="hrLabel">1.0× EM</span><span class="hrValue">${fmtRate(nc["1.0"])}</span></div>
      <div class="holdRiskRow ${riskClass(nc["1.5"])}"><span class="hrLabel">1.5× EM</span><span class="hrValue">${fmtRate(nc["1.5"])}</span></div>
      <div class="holdRiskRow ${riskClass(nc["2.0"])}"><span class="hrLabel">2.0× EM</span><span class="hrValue">${fmtRate(nc["2.0"])}</span></div>
      <div class="holdRiskSubhead">Max Observed Deviation</div>
      <div class="holdRiskRow holdRiskRow--maxdev"><span class="hrLabel">Earnings Close</span><span class="hrValue">${fmtDev(maxDev.earnings_close)}</span></div>
      <div class="holdRiskRow holdRiskRow--maxdev"><span class="hrLabel">Next Day Close</span><span class="hrValue">${fmtDev(maxDev.next_day_close)}</span></div>
    `;
  }
  if (condNoteEl) {
    const n = hr.sample_size?.flat_open ?? 0;
    const gate = hr.flat_open_gate ?? 0.25;
    condNoteEl.textContent = `${n} events (gap ≤${(gate * 100).toFixed(0)}% EM)`;
  }

  // Render drift rates
  const driftEl = $("holdRiskDrift");
  const driftNoteEl = $("holdRiskDriftNote");
  if (driftEl) {
    const drift = hr.drift || {};
    const ei = drift.earnings_intraday || {};
    const nd = drift.next_day || {};
    driftEl.innerHTML = `
      <div class="holdRiskSubhead">Intraday (EO→EC)</div>
      <div class="holdRiskRow ${riskClass(ei["1.0"])}"><span class="hrLabel">1.0× EM</span><span class="hrValue">${fmtRate(ei["1.0"])}</span></div>
      <div class="holdRiskRow ${riskClass(ei["1.5"])}"><span class="hrLabel">1.5× EM</span><span class="hrValue">${fmtRate(ei["1.5"])}</span></div>
      <div class="holdRiskRow ${riskClass(ei["2.0"])}"><span class="hrLabel">2.0× EM</span><span class="hrValue">${fmtRate(ei["2.0"])}</span></div>
      <div class="holdRiskSubhead">Next Day (EC→NC)</div>
      <div class="holdRiskRow ${riskClass(nd["1.0"])}"><span class="hrLabel">1.0× EM</span><span class="hrValue">${fmtRate(nd["1.0"])}</span></div>
      <div class="holdRiskRow ${riskClass(nd["1.5"])}"><span class="hrLabel">1.5× EM</span><span class="hrValue">${fmtRate(nd["1.5"])}</span></div>
      <div class="holdRiskRow ${riskClass(nd["2.0"])}"><span class="hrLabel">2.0× EM</span><span class="hrValue">${fmtRate(nd["2.0"])}</span></div>
    `;
  }
  if (driftNoteEl) {
    driftNoteEl.textContent = "Post-event drift from baseline";
  }
}

function renderQuarterCards(quarters) {
  const host = $("quarterCards");
  if (!host) return;
  host.innerHTML = "";
  const qs = quarters || {};

  for (const q of ["Q1", "Q2", "Q3", "Q4"]) {
    const row = qs[q] || {};
    const rec = row.recommendation;
    const season = row.seasonality || {};
    const breachDeltaPP = season.breach_delta_pp;
    const avgRatio = row.avg_ratio_realized_to_implied;
    const maxRatio = row.max_ratio_realized_to_implied;

    const tooltipParts = [];
    if (season.ratio_delta !== null && season.ratio_delta !== undefined) tooltipParts.push(`ratio_delta: ${season.ratio_delta}`);
    if (season.overshoot_delta_pp !== null && season.overshoot_delta_pp !== undefined) tooltipParts.push(`overshoot_delta_pp: ${season.overshoot_delta_pp}`);
    if (season.z_breach !== null && season.z_breach !== undefined) tooltipParts.push(`z_breach: ${season.z_breach}`);
    const tooltip = tooltipParts.length ? tooltipParts.join(" | ") : "—";

    const card = document.createElement("div");
    card.className = "quarterCard";
    card.innerHTML = `
      <div class="quarterTop">
        <div class="quarterHeading">
          <div class="quarterLabel">${escapeHtml(q)}</div>
          <div>${recBadge(rec)}</div>
        </div>
      </div>
      <div class="kv">
        <div class="k">Breach Δ vs baseline</div>
        <div class="v" title="${escapeHtml(tooltip)}"><span class="delta ${deltaClass(breachDeltaPP)}">${escapeHtml(fmtSignedPP(breachDeltaPP))}</span></div>
        <div class="k">Avg realized / implied</div>
        <div class="v mono">${escapeHtml(fmtX(avgRatio))}</div>
        <div class="k">Max realized / implied</div>
        <div class="v mono">${escapeHtml(fmtX(maxRatio))}</div>
      </div>
    `;
    host.appendChild(card);
  }
}

function render(payload) {
  $("results").classList.remove("hidden");
  lastPayload = payload;
  earningsExpanded = false;
  const toggle = $("earningsToggle");
  if (toggle) toggle.textContent = "Show earnings history";

  // Summary metrics are now displayed in the scan-first Risk Check panel (no duplicate Summary section).
  const s = payload.summary || {};

  // Scan-first decision header (instrument panel style)
  try { renderEngine1DecisionPanel(payload); } catch { /* ignore */ }

  const rg = payload.regime || {};
  const asOf = $("regimeAsOf");
  if (asOf) asOf.textContent = rg.asOfDate ? `Latest ORATS EOD: ${rg.asOfDate}` : "—";
  const rl = $("regimeLabel");
  if (rl) rl.textContent = rg.label || "—";
  const tm = $("tailMultiplier");
  if (tm) tm.textContent = (rg.tailMultiplier === null || rg.tailMultiplier === undefined) ? "—" : `${Number(rg.tailMultiplier).toFixed(2)}×`;
  const tg = $("tradeGate");
  if (tg) {
    const gate = rg?.guidance?.tradeGate;
    if (gate === "NO_TRADE") tg.innerHTML = pill("No Trade", "bad");
    else if (gate === "CAUTION") tg.innerHTML = pill("Caution", "warn");
    else if (gate === "OK") tg.innerHTML = pill("OK", "neutral");
    else tg.textContent = "—";
  }
  // Market dealer gamma (live, informational only)
  const mgMain = $("marketGammaMain");
  const mgNote = $("marketGammaNote");
  const mgOi = $("marketOiNote");
  const mg = payload?.marketDealerGamma || null;
  if (mgMain && mgNote && mgOi) {
    const dg = mg?.dealerGamma || null;
    const oi = mg?.oiClusters || null;
    const enabled = !!(mg && mg.enabled && dg && dg.netGammaSign);
    if (!enabled) {
      mgMain.textContent = "—";
      const notes = Array.isArray(mg?.notes) ? mg.notes.filter(Boolean) : [];
      const warn = Array.isArray(mg?.warnings) ? mg.warnings.filter(Boolean) : [];
      mgNote.textContent = (notes[0] || warn[0] || "Live context unavailable (market closed, entitlement gap, or no live chain).");
      mgOi.textContent = "—";
    } else {
      const sign = String(dg.netGammaSign || "—");
      const bucket = String(dg.magnitudeBucket || "—");
      const exp = String(mg.expiry || "—");
      const sym = String(mg.symbolUsed || "SPX");
      mgMain.textContent = `${sym.toUpperCase()} · ${sign.toUpperCase()} · ${bucket.toUpperCase()}`;
      const warn = mg.warning ? ` ${String(mg.warning)}` : "";
      mgNote.textContent = `expiry=${exp} · band=±${Math.round(Number(dg.bandPct || 0.05) * 100)}% · weighting=${String(dg.weightingMode || "—")}.${warn}`;
      const putWall = oi && typeof oi === "object" ? oi.putWall : null;
      const callWall = oi && typeof oi === "object" ? oi.callWall : null;
      const putStrike = putWall && (putWall.peakStrike ?? putWall.maxStrike);
      const callStrike = callWall && (callWall.peakStrike ?? callWall.maxStrike);
      const putTxt = putWall && Number.isFinite(Number(putStrike)) ? `${Number(putStrike).toFixed(0)} (${Number(putWall.totalOI || 0).toFixed(0)})` : "—";
      const callTxt = callWall && Number.isFinite(Number(callStrike)) ? `${Number(callStrike).toFixed(0)} (${Number(callWall.totalOI || 0).toFixed(0)})` : "—";
      mgOi.textContent = `OI walls: put=${putTxt} | call=${callTxt}`;
    }
  }

  // Ticker dealer gamma (live, informational only)
  const tgCard = $("tickerGammaCard");
  const tgLabel = $("tickerGammaLabel");
  const tgMain = $("tickerGammaMain");
  const tgNote = $("tickerGammaNote");
  const tgOi = $("tickerOiNote");
  const tgd = payload?.tickerDealerGamma || null;
  if (tgLabel) tgLabel.textContent = `${String(payload?.ticker || "Ticker").toUpperCase()} Dealer Gamma (Live)`;
  if (tgCard && tgMain && tgNote && tgOi) {
    const dg = tgd?.dealerGamma || null;
    const oi = tgd?.oiClusters || null;
    const enabled = !!(tgd && tgd.enabled && dg && dg.netGammaSign);
    if (!enabled) {
      tgMain.textContent = "—";
      const notes = Array.isArray(tgd?.notes) ? tgd.notes.filter(Boolean) : [];
      const warn = Array.isArray(tgd?.warnings) ? tgd.warnings.filter(Boolean) : [];
      tgNote.textContent = (notes[0] || warn[0] || "Live context unavailable (market closed, entitlement gap, or no live chain).");
      tgOi.textContent = "—";
    } else {
      const sign = String(dg.netGammaSign || "—");
      const bucket = String(dg.magnitudeBucket || "—");
      const exp = String(tgd.expiry || "—");
      const sym = String(tgd.symbolUsed || payload?.ticker || "—");
      const earn = tgd.earnDateTarget ? ` · earn=${String(tgd.earnDateTarget)}` : "";
      tgMain.textContent = `${sym.toUpperCase()} · ${sign.toUpperCase()} · ${bucket.toUpperCase()}`;
      const warns = Array.isArray(tgd.warnings) && tgd.warnings.length ? ` · ${tgd.warnings.join(" · ")}` : "";
      const bandMode = tgd.bandMode ? ` · ${String(tgd.bandMode)}` : "";
      tgNote.textContent = `expiry=${exp}${earn} · band=±${Math.round(Number(dg.bandPct || 0.05) * 100)}%${bandMode} · weighting=${String(dg.weightingMode || "—")}${warns}`;
      const putWall = oi && typeof oi === "object" ? oi.putWall : null;
      const callWall = oi && typeof oi === "object" ? oi.callWall : null;
      const putStrike = putWall && (putWall.peakStrike ?? putWall.maxStrike);
      const callStrike = callWall && (callWall.peakStrike ?? callWall.maxStrike);
      const putTxt = putWall && Number.isFinite(Number(putStrike)) ? `${Number(putStrike).toFixed(0)} (${Number(putWall.totalOI || 0).toFixed(0)})` : "—";
      const callTxt = callWall && Number.isFinite(Number(callStrike)) ? `${Number(callStrike).toFixed(0)} (${Number(callWall.totalOI || 0).toFixed(0)})` : "—";
      tgOi.textContent = `OI walls: put=${putTxt} | call=${callTxt}`;
    }
  }

  // OI Clusters cards (Market + Ticker)
  function renderOiClustersCard(prefix, container) {
    const meta = $(`${prefix}OiMeta`);
    const put = $(`${prefix}OiPut`);
    const call = $(`${prefix}OiCall`);
    if (!meta || !put || !call) return;

    const oiObj = container?.oiClusters || null;
    const enabled = !!(container && container.enabled && oiObj && typeof oiObj === "object");
    if (!enabled) {
      meta.textContent = "—";
      put.textContent = "Put: —";
      call.textContent = "Call: —";
      return;
    }

    const spot = Number(oiObj.spot);
    const step = Number(oiObj.strikeStep);
    const band = Number(oiObj.bandPct);
    meta.textContent = `expiry=${String(oiObj.expiry || container.expiry || "—")} · spot=${fmt0(spot)} · band=±${Math.round((Number.isFinite(band) ? band : 0.05) * 100)}% · step=${fmt0(step)}`;

    const puts = _pickMeaningfulClusters(oiObj.putClusters, spot, step).map(_fmtClusterLine);
    const calls = _pickMeaningfulClusters(oiObj.callClusters, spot, step).map(_fmtClusterLine);
    put.textContent = puts.length ? `Put: ${puts.join(" | ")}` : "Put: —";
    call.textContent = calls.length ? `Call: ${calls.join(" | ")}` : "Call: —";
  }

  renderOiClustersCard("market", mg);
  renderOiClustersCard("ticker", tgd);

  const rm = $("regimeMessage");
  if (rm) rm.textContent = rg?.guidance?.message || "—";

  const rv = payload.regimeValidation || {};
  const rvMeta = $("regimeValidationMeta");
  if (rvMeta) rvMeta.textContent = rv.eventsUsed ? `${rv.eventsUsed} usable events` : "—";
  const rvFlagged = $("rvFlagged");
  if (rvFlagged) {
    const b = rv.breaches ?? 0;
    const bf = rv.breachesFlagged ?? 0;
    const pct = (b && b > 0) ? Math.round((bf / b) * 100) : 0;
    rvFlagged.textContent = `Breaches flagged (Caution/No-Trade): ${bf} / ${b} (${pct}%)`;
  }
  const rvMissed = $("rvMissed");
  if (rvMissed) {
    rvMissed.textContent = `Breaches missed (OK): ${rv.breachesMissed ?? 0}`;
  }
  const rvRates = $("rvRates");
  if (rvRates) {
    const br = rv.breachRateByGatePct || {};
    const ok = br.OK ?? "—";
    const ca = br.CAUTION ?? "—";
    const nt = br.NO_TRADE ?? "—";
    rvRates.textContent = `Breach rate by gate: OK ${ok}% · Caution ${ca}% · No-Trade ${nt}%`;
  }

  renderQuarterCards(payload.quarters);
  const a = $("actionSummary");
  if (a) a.textContent = buildActionSummary(payload.quarters);

  renderBufferTarget(payload);
  renderEventRisk(payload);
  renderSkewWings(payload);
  renderMonteCarlo(payload);
  renderTradeBuilder(payload);
  renderHoldRisk(payload);
  try {
    if (typeof window.renderTechnicalsDailyPanel === "function") {
      window.renderTechnicalsDailyPanel(payload, { rootId: "technicalsSection", symbolOverride: payload?.ticker });
    }
  } catch {
    // ignore TA panel errors to avoid breaking core workflow
  }

  const skipped = payload.skipped || [];
  $("skippedSummary").textContent =
    skipped.length > 0 ? `${skipped.length} skipped (see notes column)` : "";

  renderEarningsTable(payload.events || []);
}

function setStatus(text, isError = false) {
  const el = $("status");
  el.textContent = text || "";
  el.classList.toggle("isError", !!isError);
  el.classList.toggle("isRunning", !isError && !!text && String(text).includes("…"));
  el.classList.toggle("isOk", !isError && String(text || "").toUpperCase() === "OK");
}

function setBusy(busy) {
  isBusy = !!busy;
  const btn = $("submit");
  const form = $("form");
  const ticker = $("ticker");
  const k = $("k");
  if (btn) btn.disabled = isBusy;
  if (ticker) ticker.disabled = isBusy;
  if (k) k.disabled = isBusy;
  if (form) form.classList.toggle("isLoading", isBusy);
}

document.addEventListener("DOMContentLoaded", () => {
  // Feature-gated nav links (Engine 2).
  (async () => {
    try {
      const flags = await fetchJson("/api/flags");
      const link = $("engine2Link");
      if (link) link.classList.toggle("hidden", !flags?.ENABLE_ENGINE2_SPX_IC);
    } catch {
      // If flags endpoint fails, keep the link hidden by default.
    }
  })();

  // Query params (calendar deep-links)
  const qs = new URLSearchParams(window.location.search || "");
  const qsTicker = (qs.get("ticker") || "").trim().toUpperCase();
  const qsMc = String(qs.get("mc") || "").trim().toLowerCase();
  const qsAutorun = String(qs.get("autorun") || "").trim().toLowerCase();

  const form = $("form");
  const ticker = $("ticker");
  const kSel = $("k");
  ticker.value = (qsTicker || ticker.value || "AAPL").toUpperCase();
  setTickerLogo(ticker.value);

  ticker.addEventListener("input", () => {
    ticker.value = ticker.value.toUpperCase();
    setTickerLogo(ticker.value);
  });

  async function runCalculation(extraParams = null) {
    setStatus("");
    const t = (ticker.value || "").trim().toUpperCase();
    const k = (kSel?.value || "1.0");
    if (!t) {
      setStatus("Enter a ticker.", true);
      return;
    }

    // Build URL
    let url = `/api/breach?ticker=${encodeURIComponent(t)}&n=20&years=5&k=${encodeURIComponent(k)}`;
    if (extraParams && typeof extraParams === "object") {
      for (const [kk, vv] of Object.entries(extraParams)) {
        if (vv === null || vv === undefined) continue;
        url += `&${encodeURIComponent(kk)}=${encodeURIComponent(String(vv))}`;
      }
    }
    if (mcEnabledPref) {
      // Default-on conditioning for trader relevance; backend will still fail-safe if pools are too small.
      url += `&mc=1&mc_cond_regime=1&mc_cond_quarter=1`;
      if (mcEventOverride?.date) url += `&mc_event_date=${encodeURIComponent(String(mcEventOverride.date))}`;
      if (mcEventOverride?.timing && String(mcEventOverride.timing).toUpperCase() !== "AUTO") {
        url += `&mc_event_timing=${encodeURIComponent(String(mcEventOverride.timing).toUpperCase())}`;
      }
    }

    // PERFORMANCE: Show cached data immediately if available
    const cached = _getCached(url);
    if (cached?.isStale) {
      // Render cached data immediately for perceived instant response
      render(cached.data);
      loadEngine1Levels(t);
      setStatus("Refreshing data...");
    }

    setBusy(true);
    if (!cached?.isStale) {
      setStatus(`Computing with k=${k}…`);
    }

    try {
      const payload = await fetchJson(url);
      render(payload);
      // Async live gamma visuals (do not block Engine 1 RUN completion)
      loadEngine1Levels(t);
      setStatus("");
    } catch (e) {
      // If we showed cached data, don't overwrite with error unless we have no data
      if (!cached?.isStale) {
        setStatus(e?.message || "Error", true);
      } else {
        setStatus("Refresh failed (showing cached data)");
      }
    } finally {
      setBusy(false);
    }
  }

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (isBusy) return;
    await runCalculation();
  });

  // UX: if results are already present, changing breach multiple should immediately recompute.
  if (kSel) {
    kSel.addEventListener("change", async () => {
      if (isBusy) return;
      if (!lastPayload) return; // don't auto-run before first calculation
      await runCalculation();
    });
  }

  const toggle = $("earningsToggle");
  if (toggle) {
    toggle.addEventListener("click", () => {
      earningsExpanded = !earningsExpanded;
      toggle.textContent = earningsExpanded ? "Hide earnings history" : "Show earnings history";
      if (lastPayload) renderEarningsTable(lastPayload.events || []);
    });
  }

  const adv = $("advancedColsToggle");
  if (adv) {
    adv.addEventListener("click", () => {
      showAdvancedCols = !showAdvancedCols;
      adv.textContent = showAdvancedCols ? "Hide advanced columns" : "Show advanced columns";
      if (lastPayload) renderEarningsTable(lastPayload.events || []);
    });
  }

  const symBtn = $("bufferModeSymmetric");
  if (symBtn) {
    symBtn.addEventListener("click", () => {
      bufferModePref = "symmetric";
      if (lastPayload) renderBufferTarget(lastPayload);
    });
  }
  const asymBtn = $("bufferModeAsymmetric");
  if (asymBtn) {
    asymBtn.addEventListener("click", () => {
      bufferModePref = "asymmetric";
      if (lastPayload) renderBufferTarget(lastPayload);
    });
  }

  const tbToggle = $("tradeBuilderToggle");
  const tbPanel = $("tradeBuilderPanel");
  if (tbToggle && tbPanel) {
    tbToggle.addEventListener("click", () => {
      tradeBuilderExpanded = !tradeBuilderExpanded;
      tbPanel.classList.toggle("hidden", !tradeBuilderExpanded);
      tbToggle.textContent = tradeBuilderExpanded ? "Hide trade builder" : "Show trade builder";
      if (lastPayload) renderTradeBuilder(lastPayload);
    });
  }

  function setSegActive(ids, activeId) {
    for (const id of ids) {
      const el = $(id);
      if (el) el.classList.toggle("isActive", id === activeId);
    }
  }

  const modeBtns = ["tbModeAuto", "tbModeDelta", "tbModePremium"];
  const symBtns = ["tbSymAuto", "tbSymSym", "tbSymManual"];

  function syncTradeBuilderControls() {
    setSegActive(
      modeBtns,
      tradeBuilderState.mode === "auto" ? "tbModeAuto" : tradeBuilderState.mode === "equal_delta" ? "tbModeDelta" : "tbModePremium"
    );
    setSegActive(
      symBtns,
      tradeBuilderState.symmetry === "auto" ? "tbSymAuto" : tradeBuilderState.symmetry === "symmetric" ? "tbSymSym" : "tbSymManual"
    );
    const deltaRow = $("tbDeltaRow");
    const premRow = $("tbPremiumRow");
    const manualRow = $("tbManualMultRow");
    if (deltaRow) deltaRow.classList.toggle("hidden", tradeBuilderState.mode !== "equal_delta");
    if (premRow) premRow.classList.toggle("hidden", tradeBuilderState.mode !== "equal_premium");
    if (manualRow) manualRow.classList.toggle("hidden", tradeBuilderState.symmetry !== "manual");
  }

  for (const id of modeBtns) {
    const el = $(id);
    if (!el) continue;
    el.addEventListener("click", () => {
      if (id === "tbModeAuto") tradeBuilderState.mode = "auto";
      if (id === "tbModeDelta") tradeBuilderState.mode = "equal_delta";
      if (id === "tbModePremium") tradeBuilderState.mode = "equal_premium";
      syncTradeBuilderControls();
      if (lastPayload) renderTradeBuilder(lastPayload);
    });
  }
  for (const id of symBtns) {
    const el = $(id);
    if (!el) continue;
    el.addEventListener("click", () => {
      if (id === "tbSymAuto") tradeBuilderState.symmetry = "auto";
      if (id === "tbSymSym") tradeBuilderState.symmetry = "symmetric";
      if (id === "tbSymManual") tradeBuilderState.symmetry = "manual";
      syncTradeBuilderControls();
      if (lastPayload) renderTradeBuilder(lastPayload);
    });
  }

  const deltaPreset = $("tbDeltaPreset");
  if (deltaPreset) {
    deltaPreset.addEventListener("change", () => {
      const v = Number(deltaPreset.value);
      if (Number.isFinite(v)) tradeBuilderState.target_delta = v;
      const inp = $("tbTargetDelta");
      if (inp) inp.value = String(v);
    });
  }
  const deltaInp = $("tbTargetDelta");
  if (deltaInp) {
    deltaInp.addEventListener("input", () => {
      const v = Number(deltaInp.value);
      if (Number.isFinite(v)) tradeBuilderState.target_delta = v;
    });
  }
  const premInp = $("tbTargetPremium");
  if (premInp) {
    premInp.addEventListener("input", () => {
      const v = Number(premInp.value);
      if (Number.isFinite(v)) tradeBuilderState.target_premium = v;
    });
  }
  const putMultInp = $("tbPutMult");
  if (putMultInp) {
    putMultInp.addEventListener("input", () => {
      tradeBuilderState.put_mult = putMultInp.value;
      if (lastPayload) renderTradeBuilder(lastPayload);
    });
  }
  const callMultInp = $("tbCallMult");
  if (callMultInp) {
    callMultInp.addEventListener("input", () => {
      tradeBuilderState.call_mult = callMultInp.value;
      if (lastPayload) renderTradeBuilder(lastPayload);
    });
  }
  const widthSel = $("tbWingWidth");
  if (widthSel) {
    widthSel.addEventListener("change", () => {
      const v = Number(widthSel.value);
      if (Number.isFinite(v)) tradeBuilderState.wing_width = v;
      if (lastPayload) renderTradeBuilder(lastPayload);
    });
  }

  const tbRecalc = $("tradeBuilderRecalc");
  if (tbRecalc) {
    tbRecalc.addEventListener("click", async () => {
      if (isBusy) return;
      const gate = (lastPayload?.regime?.guidance || {})?.tradeGate;
      if (gate === "NO_TRADE") return;
      const extra = {
        mode: tradeBuilderState.mode,
        symmetry: tradeBuilderState.symmetry,
        target_delta: tradeBuilderState.target_delta,
        target_premium: tradeBuilderState.target_premium,
        wing_width: tradeBuilderState.wing_width,
        dte_target: 2,
      };
      await runCalculation(extra);
    });
  }

  syncTradeBuilderControls();

  const mcToggle = $("mcToggle");
  const mcGroup = $("mcOverrideGroup");
  const mcDate = $("mcEventDate");
  const mcTiming = $("mcEventTiming");
  if (mcToggle) {
    mcToggle.addEventListener("change", async () => {
      mcEnabledPref = !!mcToggle.checked;
      if (mcGroup) mcGroup.classList.toggle("hidden", !mcEnabledPref);
      if (!lastPayload) return;
      if (isBusy) return;
      await runCalculation();
    });
  }

  // Apply mc=1 from querystring without triggering a compute until we decide to autorun.
  if (mcToggle && (qsMc === "1" || qsMc === "true" || qsMc === "yes" || qsMc === "on")) {
    mcEnabledPref = true;
    mcToggle.checked = true;
    if (mcGroup) mcGroup.classList.toggle("hidden", false);
  }

  if (mcDate) {
    mcDate.addEventListener("change", async () => {
      mcEventOverride.date = mcDate.value || null;
      if (!mcEnabledPref) return;
      if (!lastPayload || isBusy) return;
      await runCalculation();
    });
  }
  if (mcTiming) {
    mcTiming.addEventListener("change", async () => {
      mcEventOverride.timing = mcTiming.value || "AUTO";
      if (!mcEnabledPref) return;
      if (!lastPayload || isBusy) return;
      await runCalculation();
    });
  }

  // AskRaven removed
  initTooltips();
  try { window.RavenUI?.initInfoTips?.(); } catch { /* ignore */ }
  initEngine1GammaVizUI();

  // Optional auto-run for calendar deep-links: /breach?ticker=...&mc=1&autorun=1
  if (qsAutorun === "1" || qsAutorun === "true" || qsAutorun === "yes" || qsAutorun === "on") {
    // Kick once on page load. Respect "busy" guardrails.
    if (!isBusy) runCalculation();
  }
});


