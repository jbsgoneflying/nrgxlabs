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

  const gapVsCtc = payload?.gapVsCtc;
  if (gapVsCtc) {
    add("engine1.gapVsCtc.gap_1x", gapVsCtc.gap?.["1.0"]);
    add("engine1.gapVsCtc.gap_15x", gapVsCtc.gap?.["1.5"]);
    add("engine1.gapVsCtc.gap_2x", gapVsCtc.gap?.["2.0"]);
    add("engine1.gapVsCtc.session_1x", gapVsCtc.ctc?.["1.0"]);
    add("engine1.gapVsCtc.session_15x", gapVsCtc.ctc?.["1.5"]);
    add("engine1.gapVsCtc.session_2x", gapVsCtc.ctc?.["2.0"]);
  }

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

  const gvcExp = payload?.gapVsCtc;
  if (gvcExp) {
    lines.push("## Gap vs Session Risk");
    lines.push("| Multiple | Gap | Session | Delta |");
    lines.push("|----------|-----|---------|-------|");
    for (const mk of ["1.0", "1.5", "2.0"]) {
      const g = gvcExp.gap?.[mk], s = gvcExp.ctc?.[mk];
      const gT = g != null ? Number(g).toFixed(1) + "%" : "—";
      const sT = s != null ? Number(s).toFixed(1) + "%" : "—";
      const d = (g != null && s != null) ? (Number(s) - Number(g)).toFixed(1) + "pp" : "—";
      lines.push(`| ${mk}× EM | ${gT} | ${sT} | ${d} |`);
    }
    lines.push("");
  }

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

function _e1DataBadgeText(payload) {
  var gng = payload?.goNoGo || {};
  var status = String(gng?.status || "").toUpperCase();
  if (status === "NO_GO") return "LIQUIDITY BLOCK";
  var flagCount = gng?.flagCount || 0;
  if (flagCount > 0) return "DATA OK \u00B7 " + flagCount + " FLAG" + (flagCount > 1 ? "S" : "");
  return "DATA OK";
}
function _e1DataBadgeClass(payload) {
  var status = String((payload?.goNoGo || {})?.status || "").toUpperCase();
  if (status === "NO_GO") return "bad";
  if ((payload?.goNoGo || {})?.flagCount > 0) return "warn";
  return "neu";
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
  const label = String(rg?.label || "—");
  const tailMult = rg?.tailMultiplier;
  const tailTxt = (tailMult !== null && tailMult !== undefined && Number.isFinite(Number(tailMult))) ? ` (${Number(tailMult).toFixed(2)}x)` : "";

  // Expected Move data (ATM-forward straddle calculation)
  const em = payload?.expectedMove || {};
  const emPct = em?.expectedMovePct;
  const emDollars = em?.expectedMoveDollars;
  const emExpiry = String(em?.expiry || "").slice(0, 10);
  const emSource = String(em?.source || "").toLowerCase();
  const emSourceLabel = emSource === "live" ? "Live" : emSource === "eod" ? "EOD" : emSource === "impermnv" ? "EM" : emSource ? emSource : "—";

  // ORATS EM data (impErnMv - used for earnings events calculations)
  const cur = payload?.current || {};
  const nextEv = payload?.nextEvent || {};
  const oratsEmPct = cur?.impliedMovePct ?? nextEv?.impliedMovePctPlanned;
  const oratsEmSource = cur?.source || (nextEv?.impliedMoveSource ? "nextEvent" : null);
  const oratsEmAsOf = String(cur?.asOfDate || "").slice(0, 10);

  // 15-min delayed ORATS EM (from /cores snapshot)
  const delayedEmPct = cur?.delayedImpliedMovePct;
  const delayedUpdatedAt = String(cur?.delayedUpdatedAt || "").trim();
  const delayedTradeDate = String(cur?.delayedTradeDate || "").slice(0, 10);

  // Strike Targets data (using ORATS EM percentages — prefers delayed EM)
  const st = payload?.strikeTargets || null;
  const stWhitePct = st?.whitePct;
  const stBluePct = st?.bluePct;
  const stRedPct = st?.redPct;
  const stEmSource = st?.emSource || "eod";

  // Gap vs Session (CTC) risk comparison data
  const gvc = payload?.gapVsCtc || {};
  const gvcGap = gvc?.gap || {};
  const gvcCtc = gvc?.ctc || {};

  const chips = [];
  if (label && label !== "—") chips.push(`Regime: ${label}${tailTxt}`);
  if (payload?.eventRisk?.label) chips.push(`Event risk: ${String(payload.eventRisk.label)}`);
  const chipHtml = chips.slice(0, 3).map((c) => `<span class="taChip">${escapeHtml(c)}</span>`).join("");

  const dots = Array.from({ length: 5 }).map((_, i) => `<span class="taDot ${i < 3 ? "isOn" : ""}"></span>`).join("");

  host.classList.toggle("hidden", !t || t === "—");
  if (!t || t === "—") return;

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
          </div>
          <div class="taHeaderMeta">${metaRight}</div>
        </div>
        <div class="taHeaderRow taHeaderRow--sub">
          <div class="taBiasPill taBiasPill--${_e1DataBadgeClass(payload)}">${_e1DataBadgeText(payload)}</div>
          <div class="taConf" title="Confidence dots (heuristic)">${dots}</div>
          <div class="taChips">${chipHtml}</div>
          <div class="taHeaderActions">
            <button class="taActionBtn" type="button" id="e1ExportLLM">Export (LLM)</button>
            <button class="taActionBtn" type="button" id="e1ExportOnePage">Export One-Page (LLM)</button>
          </div>
        </div>
      </div>

      <div class="taGrid" aria-label="Engine 1 instrument cards">
        <div class="taCard taCard--wide" style="grid-column: span 2;">
          <div class="taCardTop">
            <div class="taCardTitle">Gap vs Session Risk</div>
            <span class="info" title="Gap = overnight open-to-close move vs EM. Session = prior close to earnings day close (full move including intraday continuation). Delta shows additional risk from holding through the session.">ⓘ</span>
          </div>
          <table class="gvcTable">
            <thead><tr><th></th><th>Gap</th><th>Session</th><th>Δ</th></tr></thead>
            <tbody>
              ${["1.0", "1.5", "2.0"].map(function(mk) {
                var gv = gvcGap[mk], sv = gvcCtc[mk];
                var gTxt = (gv !== null && gv !== undefined) ? Number(gv).toFixed(1) + "%" : "—";
                var sTxt = (sv !== null && sv !== undefined) ? Number(sv).toFixed(1) + "%" : "—";
                var delta = (gv !== null && gv !== undefined && sv !== null && sv !== undefined) ? Number(sv) - Number(gv) : null;
                var dTxt = delta !== null ? (delta > 0 ? "+" : "") + delta.toFixed(1) + "pp" : "—";
                var dCls = delta === null ? "" : delta > 5 ? "gvcDelta--high" : delta > 0 ? "gvcDelta--med" : "gvcDelta--zero";
                return '<tr><td class="gvcLabel">' + mk + '× EM</td><td class="mono">' + gTxt + '</td><td class="mono">' + sTxt + '</td><td class="mono ' + dCls + '">' + dTxt + '</td></tr>';
              }).join("")}
            </tbody>
          </table>
          <div class="taCardInterp">Gap = overnight. Session = close-to-close (PC→EC). ${gvc.sample_gap || 0} gap / ${gvc.sample_ctc || 0} session events.</div>
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
          <div class="taCardTop">
            <div class="taCardTitle">ORATS EM</div>
            <span class="info" title="ORATS implied earnings move (impErnMv). EOD = last close from hist/cores. Delayed = 15-min delayed from /cores snapshot (freshest available). Strike targets are built off the delayed value when available.">ⓘ</span>
          </div>
          <div class="taCardState mono">${Number.isFinite(oratsEmPct) ? escapeHtml(oratsEmPct.toFixed(2)) + "%" : "—"}</div>
          <div class="taCardInterp">${oratsEmAsOf ? `As of: ${escapeHtml(oratsEmAsOf)}` : "—"} · EOD (used for breach history)</div>
          <div style="border-top:1px solid rgba(0,0,0,0.06); margin-top:6px; padding-top:6px;">
            <div class="taCardState mono" style="font-size:0.95em;">${Number.isFinite(delayedEmPct) ? escapeHtml(delayedEmPct.toFixed(2)) + "%" : "—"}</div>
            <div class="taCardInterp">${delayedUpdatedAt ? `Updated: ${escapeHtml(delayedUpdatedAt)}` : delayedTradeDate ? `As of: ${escapeHtml(delayedTradeDate)}` : "—"} · 15-min delayed${Number.isFinite(delayedEmPct) ? " · <strong>Used for strike targets</strong>" : ""}</div>
          </div>
        </div>
        <div class="taCard">
          <div class="taCardTop">
            <div class="taCardTitle">Straddle EM</div>
            <span class="info" title="Risk-neutral expected absolute move to near-dated expiry. Computed via ATM-forward straddle method. Uses live data when market is open, EOD otherwise.">ⓘ</span>
          </div>
          <div class="taCardState mono">${Number.isFinite(emPct) ? escapeHtml(emPct.toFixed(2)) + "%" : "—"}</div>
          <div class="taCardInterp">${Number.isFinite(emDollars) ? `$${escapeHtml(emDollars.toFixed(2))} pts` : "—"} · ${emExpiry ? `Exp: ${escapeHtml(emExpiry)}` : ""} · ${emSourceLabel}</div>
        </div>
        <div class="taCard">
          <div class="taCardTop">
            <div class="taCardTitle">Strike Targets (EM)</div>
            <span class="info" title="Wing strike distances based on ORATS implied earnings move (EM). 1.0× = 1× EM, 1.5× = 1.5× EM, 2.0× = 2× EM. Use for short-strike targeting.">ⓘ</span>
          </div>
          <div class="emTargetGrid">
            <div class="emRow emBox--white"><span class="k">1.0× EM</span><span class="v mono">${Number.isFinite(stWhitePct) ? escapeHtml(stWhitePct.toFixed(2)) + "%" : "—"}</span></div>
            <div class="emRow emBox--blue"><span class="k">1.5× EM</span><span class="v mono">${Number.isFinite(stBluePct) ? escapeHtml(stBluePct.toFixed(2)) + "%" : "—"}</span></div>
            <div class="emRow emBox--red"><span class="k">2.0× EM</span><span class="v mono">${Number.isFinite(stRedPct) ? escapeHtml(stRedPct.toFixed(2)) + "%" : "—"}</span></div>
          </div>
          <div class="taCardInterp">Wing distance as % of spot (${stEmSource === "delayed" ? "15-min delayed EM" : "ORATS EOD EM"}).</div>
        </div>
      </div>

      <div id="actionSummary" class="taCardInterp muted" style="margin-top:10px;" aria-live="polite">—</div>
    </div>
  `;

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

// --- Earnings Playbook Cards (Live Data dropdown) ---
// Populates the 4 playbook cards from goNoGo.checks data.
function renderPlaybookCards(go) {
  const checks = Array.isArray(go?.checks) ? go.checks : [];
  const byId = {};
  for (const c of checks) byId[String(c?.id || "")] = c;

  const num = (x) => (x === null || x === undefined || x === "" ? Number.NaN : Number(x));
  const badge = (state) => {
    const s = String(state || "").toUpperCase();
    if (s === "PASS") return '<span style="color:#34c759;font-weight:600;margin-left:6px;">PASS</span>';
    if (s === "BLOCK") return '<span style="color:#ff3b30;font-weight:600;margin-left:6px;">BLOCK</span>';
    if (s === "FAIL") return '<span style="color:#ff3b30;font-weight:600;margin-left:6px;">FAIL</span>';
    if (s === "FLAG") return '<span style="color:#ff9500;font-weight:600;margin-left:6px;">FLAG</span>';
    return '<span style="color:#ff9f0a;font-weight:600;margin-left:6px;">—</span>';
  };

  // --- Card 1: IV Percentile ---
  const ivEl = $("pbIvState");
  const ivInterp = $("pbIvInterp");
  const iv = byId["SN_IV_ELEVATED"] || null;
  if (ivEl && ivInterp) {
    if (iv) {
      const v = iv.value || {};
      const pctl = num(v.percentile01);
      const z = num(v.z);
      const ivPct = num(v.currentIv30Pct);
      const n = num(v.sampleN);
      ivEl.innerHTML = (Number.isFinite(pctl) ? (pctl * 100).toFixed(0) + "th" : "—") + badge(iv.state);
      const parts = [];
      if (Number.isFinite(ivPct)) parts.push("IV30=" + ivPct.toFixed(1) + "%");
      if (Number.isFinite(z)) parts.push("z=" + z.toFixed(2));
      if (Number.isFinite(n)) parts.push("n=" + n.toFixed(0));
      ivInterp.textContent = parts.length ? parts.join(" · ") : (iv.explain || "—");
    } else {
      ivEl.innerHTML = "—";
      ivInterp.textContent = "No IV check data available.";
    }
  }

  // --- Card 2: Premium Richness ---
  const richEl = $("pbRichnessState");
  const richInterp = $("pbRichnessInterp");
  const emR = byId["SN_EM_RICHNESS"] || null;
  const tailR = byId["SN_TAIL_P90_RICHNESS"] || null;
  if (richEl && richInterp) {
    if (emR) {
      const v = emR.value || {};
      const ratio = num(v.ratio);
      const em = num(v.expectedMovePct);
      const med = num(v.realizedMedianPct);
      const tailV = tailR?.value || {};
      const p90 = num(tailV.p90RealizedPct);
      const tRatio = num(tailV.ratio);
      richEl.innerHTML = (Number.isFinite(ratio) ? ratio.toFixed(2) + "× median" : "—") + badge(emR.state);
      const parts = [];
      if (Number.isFinite(em)) parts.push("EM=" + em.toFixed(2) + "%");
      if (Number.isFinite(med)) parts.push("med=" + med.toFixed(2) + "%");
      if (Number.isFinite(tRatio)) parts.push("P90 ratio=" + tRatio.toFixed(2) + "×");
      if (tailR) parts.push("tail " + (String(tailR.state || "").toUpperCase() === "PASS" ? "PASS" : "FAIL"));
      richInterp.textContent = parts.length ? parts.join(" · ") : (emR.explain || "—");
    } else {
      richEl.innerHTML = "—";
      richInterp.textContent = "No richness check data available.";
    }
  }

  // --- Card 3: Options Liquidity ---
  const liqEl = $("pbLiqState");
  const liqInterp = $("pbLiqInterp");
  const liq = byId["SN_LIQUIDITY"] || null;
  if (liqEl && liqInterp) {
    if (liq) {
      const v = liq.value || {};
      const dvol = num(v.avgDollarVol20d);
      const agg = v.deltaBandAgg || {};
      const putSpr = num((agg.put || {}).medianSpread);
      const callSpr = num((agg.call || {}).medianSpread);
      const putCov = num((agg.put || {}).coverage);
      const callCov = num((agg.call || {}).coverage);
      const dvolTxt = Number.isFinite(dvol) ? "$" + Math.round(dvol / 1e6) + "M" : "—";
      liqEl.innerHTML = dvolTxt + " 20d avg" + badge(liq.state);
      const parts = [];
      if (Number.isFinite(putSpr)) parts.push("P.spr=" + putSpr.toFixed(2));
      if (Number.isFinite(callSpr)) parts.push("C.spr=" + callSpr.toFixed(2));
      if (Number.isFinite(putCov)) parts.push("P.cov=" + putCov.toFixed(2));
      if (Number.isFinite(callCov)) parts.push("C.cov=" + callCov.toFixed(2));
      liqInterp.textContent = parts.length ? parts.join(" · ") : (liq.explain || "—");
    } else {
      liqEl.innerHTML = "—";
      liqInterp.textContent = "No liquidity check data available.";
    }
  }

  // --- Card 4: Macro Overlay ---
  const macroEl = $("pbMacroState");
  const macroInterp = $("pbMacroInterp");
  const gamma = byId["MACRO_GAMMA"] || null;
  const idxSens = byId["SN_INDEX_SENSITIVITY"] || null;
  const rvAccel = byId["MACRO_RV_ACCEL"] || null;
  const gFlip = byId["MACRO_GAMMA_FLIP"] || null;
  const forced = byId["MACRO_FORCED_FLOWS"] || null;
  if (macroEl && macroInterp) {
    // Primary display: dealer gamma sign + magnitude
    if (gamma) {
      const gv = gamma.value || {};
      const sign = String(gv.netGammaSign || "—");
      const bucket = String(gv.magnitudeBucket || "");
      macroEl.innerHTML = sign + (bucket ? " · " + bucket : "") + badge(gamma.state);
    } else {
      macroEl.innerHTML = "—";
    }
    const parts = [];
    if (idxSens) {
      const sv = idxSens.value || {};
      const c20 = num(sv.corr20);
      const b20 = num(sv.beta20);
      if (Number.isFinite(c20)) parts.push("corr=" + c20.toFixed(2));
      if (Number.isFinite(b20)) parts.push("beta=" + b20.toFixed(2));
      if (sv.sensitive) parts.push("index-sensitive");
    }
    if (rvAccel) {
      const rv = rvAccel.value || {};
      const r5 = num(rv.rv5Jump);
      if (Number.isFinite(r5)) parts.push("RV5=" + r5.toFixed(2) + "×");
    }
    if (forced) {
      const fv = forced.value || {};
      const hi = Array.isArray(fv.high) ? fv.high.length : 0;
      if (hi > 0) parts.push("flows=" + hi + " HIGH");
    }
    if (gFlip) {
      const gfv = gFlip.value || {};
      const mf = num(gfv.minFlipEm);
      if (Number.isFinite(mf)) parts.push("flip@" + mf.toFixed(2) + "×EM");
    }
    macroInterp.textContent = parts.length ? parts.join(" · ") : "No macro overlay data.";
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

  // Muted when quarter recommendation is Avoid (fundamental data signal).
  // Regime stress is context, not a blocker — still show the wing target.
  if (isAvoid) {
    return { primary: "—", secondary: null, subtext: "Quarter: Avoid", muted: true };
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
  if (l === "NO_TRADE") return "Regime stressed — use with caution";
  return l || "—";
}

// ── Earnings Gamma Context (Raven-Tech 2.0) ──────────────────────────
function renderEarningsGammaContext(payload) {
  const sec = $("earningsGammaSection");
  if (!sec) return;

  const egc = payload?.earningsGammaContext;
  if (!egc) {
    sec.style.display = "none";
    return;
  }
  sec.style.display = "";

  const pillCls = (label) => {
    const l = (label || "").toLowerCase();
    if (l === "supportive" || l === "near") return "good";
    if (l === "hostile" || l === "downside_heavy" || l === "snap risk") return "bad";
    return "warn";
  };

  const html = `
    <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:10px;">
      <div class="kv">
        <span class="k">Gamma Context</span>
        <span class="v">${pill(egc.gamma_context || "—", pillCls(egc.gamma_context))}</span>
      </div>
      <div class="kv">
        <span class="k">Pin Zone</span>
        <span class="v">${pill(egc.pin_zone_proximity || "—", pillCls(egc.pin_zone_proximity))}</span>
      </div>
      <div class="kv">
        <span class="k">Skew Risk</span>
        <span class="v">${pill((egc.skew_risk || "—").replace(/_/g, " "), pillCls(egc.skew_risk))}</span>
      </div>
      <div class="kv">
        <span class="k">Path Risk</span>
        <span class="v"><strong>${egc.path_risk_label || "—"}</strong></span>
      </div>
    </div>
    <div style="margin-top:6px; font-size:11px; color:var(--muted);">${egc.path_risk_rationale || ""}</div>
    ${egc.expected_move_band && egc.expected_move_band.low ? `
      <div style="margin-top:6px; font-size:11px; color:var(--muted);">
        Expected move band: $${Number(egc.expected_move_band.low).toFixed(2)} – $${Number(egc.expected_move_band.high).toFixed(2)}
        (±${Number(egc.expected_move_band.impliedMovePct || 0).toFixed(1)}%)
      </div>` : ""}
  `;
  sec.innerHTML = `
    <div class="sectionHeader">
      <h2 class="sectionTitle">Dealer Gamma Context</h2>
    </div>
    ${html}
  `;
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

  if (hasWing && wingWhy) {
    wingWhy.textContent = (wr.structureRationale || wr.rationale || "—");
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
    const rgLbl = payload?.regime?.label || "—";
    impacts.push(`Regime: +${Number(bump || 0).toFixed(1)}% tail · ${rgLbl}`);
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
  // Prefer 15-min delayed EM from /cores (freshest available)
  if (cur && cur.delayedImpliedMovePct !== null && cur.delayedImpliedMovePct !== undefined) {
    const imp = Number(cur.delayedImpliedMovePct);
    if (Number.isFinite(imp) && imp > 0) return imp;
  }
  // Fallback: EOD hist_cores EM
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

  const extra = "Chain-based strike selection not enabled; showing distance targets only.";
  const cur = payload?.current || null;
  const usingDelayed = cur?.delayedImpliedMovePct !== null && cur?.delayedImpliedMovePct !== undefined && Number.isFinite(Number(cur.delayedImpliedMovePct)) && Number(cur.delayedImpliedMovePct) > 0;
  const emLabel = usingDelayed ? "15-min delayed" : "EOD";
  const src = cur?.source ? `source=${cur.source}` : "";
  const asOf = usingDelayed && cur?.delayedUpdatedAt ? `updated=${cur.delayedUpdatedAt}` : cur?.asOfDate ? `asOf=${cur.asOfDate}` : "";
  const meta = [src, emLabel, asOf].filter(Boolean).join(", ");
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
      <td class="num">${fmtPct(e.ctcMovePct)}</td>
      <td class="num ${e.ctcVsEM != null ? (Number(e.ctcVsEM) >= 1.5 ? "ctcEM--high" : Number(e.ctcVsEM) >= 1.0 ? "ctcEM--med" : "ctcEM--low") : ""}">${e.ctcVsEM != null ? Number(e.ctcVsEM).toFixed(2) + "×" : "—"}</td>
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
    condNoteEl.textContent = `${n} flat open events`;
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

    // CTC (session) breach deltas for this quarter
    const ctcD = row.ctcBreachDelta || {};
    const gapD = row.gapBreachDelta || {};

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
        <div class="k">Gap breach Δ</div>
        <div class="v" title="${escapeHtml(tooltip)}"><span class="delta ${deltaClass(breachDeltaPP)}">${escapeHtml(fmtSignedPP(breachDeltaPP))}</span></div>
        <div class="k">Session Δ 1.0×</div>
        <div class="v"><span class="delta ${deltaClass(ctcD["1.0"])}">${escapeHtml(fmtSignedPP(ctcD["1.0"]))}</span></div>
        <div class="k">Session Δ 1.5×</div>
        <div class="v"><span class="delta ${deltaClass(ctcD["1.5"])}">${escapeHtml(fmtSignedPP(ctcD["1.5"]))}</span></div>
        <div class="k">Session Δ 2.0×</div>
        <div class="v"><span class="delta ${deltaClass(ctcD["2.0"])}">${escapeHtml(fmtSignedPP(ctcD["2.0"]))}</span></div>
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
  if (window._e1InsightCacheClear) window._e1InsightCacheClear();
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
    const rgLabel = String(rg?.label || "").toLowerCase();
    if (rgLabel === "stress") tg.innerHTML = pill("Stress", "warn");
    else if (rgLabel === "elevated") tg.innerHTML = pill("Elevated", "warn");
    else if (rgLabel === "normal" || rgLabel === "calm") tg.innerHTML = pill(rg.label, "neutral");
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

  // Earnings Playbook cards (Live Data dropdown)
  try { renderPlaybookCards(payload?.goNoGo || null); } catch (e) { /* ignore */ }

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
  renderEarningsGammaContext(payload);
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

  // ── Earnings IC Advisor (Vol Crush) ──
  try { _renderE1AdvisorSection(payload); } catch (e) { console.warn("E1 advisor render:", e); }
  try { _loadE1TradeJournal(); } catch (e) { console.warn("E1 journal load:", e); }
}

// ---------------------------------------------------------------------------
// Engine 1 — Earnings IC (Vol Crush) Advisor UI
// ---------------------------------------------------------------------------

var _e1AdvisorResult = null;

function _renderE1AdvisorSection(payload) {
  var wc = payload?.e1WidthComparison;
  var vrp = payload?.vrpAnalysis;
  var dc = payload?.e1DeskConsensus;
  var sec = $("e1AdvisorSection");
  var el = $("e1AdvisorContent");
  if (!sec || !el) return;

  if (!Array.isArray(wc) || wc.length < 1 || !vrp || vrp.vrpScore === null) {
    sec.classList.add("hidden");
    return;
  }

  sec.classList.remove("hidden");

  var vrpLabel = vrp.vrpScore >= 75 ? "Strong" : vrp.vrpScore >= 55 ? "Moderate" : vrp.vrpScore >= 40 ? "Weak" : "Insufficient";
  var vrpColor = vrp.vrpScore >= 75 ? "#16a34a" : vrp.vrpScore >= 55 ? "#ca8a04" : "#dc2626";

  var html = '<div class="taPanel">';
  html += '<div class="taHeader"><div class="taHeaderRow">';
  html += '<span class="taHeaderTitle">AI Trade Advisor — Earnings Vol Crush</span>';
  html += '<button id="e1RunAdvisorBtn" class="primaryButton" style="padding:6px 16px;font-size:12px;">Run Advisor</button>';
  html += '</div></div>';

  // VRP Scorecard
  html += '<div style="padding:16px;display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;border-bottom:1px solid var(--border)">';
  html += _vrpCard("VRP Score", vrp.vrpScore != null ? vrp.vrpScore.toFixed(0) + "/100" : "—", vrpLabel, vrpColor);
  html += _vrpCard("Mean Ratio", vrp.meanRatio != null ? vrp.meanRatio.toFixed(3) : "—", vrp.meanRatio != null && vrp.meanRatio < 0.75 ? "Below 0.75 = Strong" : "", null);
  html += _vrpCard("Consistency", vrp.stdRatio != null ? "σ " + vrp.stdRatio.toFixed(3) : "—", vrp.stdRatio != null && vrp.stdRatio < 0.30 ? "Low = Reliable" : "", null);
  html += _vrpCard("Trend", vrp.trendDelta != null ? (vrp.trendDelta > 0 ? "+" : "") + vrp.trendDelta.toFixed(3) : "—", vrp.trendDelta != null && vrp.trendDelta < 0 ? "Improving" : vrp.trendDelta != null && vrp.trendDelta > 0 ? "Deteriorating" : "", null);
  html += _vrpCard("IV Elevation", vrp.ivElevation != null ? vrp.ivElevation.toFixed(2) + "x" : "—", vrp.ivElevation != null && vrp.ivElevation > 1.1 ? "Rich Premium" : "", null);
  html += _vrpCard("Sample", vrp.sampleSize + " events", vrp.confidence, null);
  html += '</div>';

  // Desk Consensus Strip
  if (dc) {
    var vColor = dc.verdict === "TRADE" ? "#16a34a" : dc.verdict === "LEAN_PASS" ? "#ca8a04" : "#dc2626";
    html += '<div style="padding:12px 16px;background:' + vColor + '18;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px;flex-wrap:wrap">';
    html += '<span style="font-weight:700;color:' + vColor + '">' + (dc.verdict || "—") + '</span>';
    html += '<span style="font-size:12px;opacity:.7">Risk: ' + (dc.riskLevel || "—") + ' · EM Floor: ' + (dc.suggestedEmFloor || "—") + 'x · Entry Quality: ' + (dc.entryQuality != null ? dc.entryQuality.toFixed(0) : "—") + '/100</span>';
    if (dc.reasons && dc.reasons.length) {
      html += '<span style="font-size:11px;opacity:.6">' + dc.reasons.join(" · ") + '</span>';
    }
    html += '</div>';
  }

  // Direct log buttons (always visible for TRADE / LEAN_PASS — no LLM required)
  if (dc && (dc.verdict === "TRADE" || dc.verdict === "LEAN_PASS")) {
    var emp = payload?.e1EmPreference || {};
    var prefWing = 5;
    html += '<div style="padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px;flex-wrap:wrap">';
    html += '<button id="e1QuickLogBtn" class="primaryButton" style="padding:8px 20px;font-size:12px;font-weight:600">Log This Trade</button>';
    html += '<button id="e1AdjustLogBtn" class="primaryButton" style="padding:8px 20px;font-size:12px;background:var(--surface2);color:var(--text);border:1px solid var(--border)">Adjust & Log</button>';
    html += '<span style="font-size:11px;opacity:.5">Uses desk consensus: ' + (emp.preferredEm || dc.preferredEm || "2.0") + 'x EM · $' + prefWing + ' wings</span>';
    html += '</div>';
  }

  // Advisor result placeholder
  html += '<div id="e1AdvisorResultArea"></div>';

  html += '</div>';

  // Width Comparison Table
  html += _buildE1WidthTable(wc, payload?.e1EmBreachSummary, payload?.e1EmPreference);

  // Active Trades section placeholder
  html += '<div id="e1ActiveTradesArea"></div>';

  el.innerHTML = html;

  var btn = $("e1RunAdvisorBtn");
  if (btn) {
    btn.addEventListener("click", function () {
      _runE1Advisor(payload);
    });
  }

  // Wire up direct log buttons
  var quickLog = $("e1QuickLogBtn");
  if (quickLog) quickLog.addEventListener("click", function () { _logE1DirectTrade(payload); });
  var adjustLog = $("e1AdjustLogBtn");
  if (adjustLog) adjustLog.addEventListener("click", function () { _showE1AdjustModal(payload); });

  // Load active trades
  _loadE1ActiveTrades();
}

function _vrpCard(label, value, sub, color) {
  var c = color ? ' style="color:' + color + '"' : '';
  return '<div style="text-align:center"><div style="font-size:10px;text-transform:uppercase;opacity:.5;margin-bottom:4px">' + label + '</div>'
    + '<div style="font-size:20px;font-weight:700"' + c + '>' + value + '</div>'
    + (sub ? '<div style="font-size:11px;opacity:.6;margin-top:2px">' + sub + '</div>' : '')
    + '</div>';
}

function _buildE1WidthTable(wc, emBreachSummary, emPref) {
  if (!wc || !wc.length) return '';

  var emGroups = {};
  for (var i = 0; i < wc.length; i++) {
    var row = wc[i];
    var ek = Number(row.emMult).toFixed(1);
    if (!emGroups[ek]) emGroups[ek] = [];
    emGroups[ek].push(row);
  }

  var prefEm = emPref ? Number(emPref.preferredEm).toFixed(1) : null;
  var prefLabel = emPref ? emPref.label : "";
  var html = '<div style="padding:16px">';
  html += '<div style="display:flex;align-items:baseline;gap:12px;margin-bottom:16px;flex-wrap:wrap">';
  html += '<h3 style="margin:0;font-size:15px;font-weight:700">EM × Wing Width Analysis</h3>';
  if (prefEm) {
    html += '<span style="font-size:12px;opacity:.7">Preferred: ' + prefEm + 'x ' + prefLabel + '</span>';
  }
  html += '</div>';

  var emKeys = Object.keys(emGroups).sort(function(a,b){return parseFloat(a)-parseFloat(b)});
  for (var ei = 0; ei < emKeys.length; ei++) {
    var em = emKeys[ei];
    var rows = emGroups[em];
    var breach = emBreachSummary ? emBreachSummary[em] : null;
    var breachLabel = em === "1.0" ? "Aggressive" : em === "1.5" ? "Standard" : "Defensive";
    var isPreferred = prefEm && em === prefEm;

    html += '<div style="margin-bottom:20px">';
    html += '<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:8px">';
    html += '<span style="font-weight:700">EM ' + em + 'x</span>';
    html += '<span style="font-size:12px;opacity:.6">' + breachLabel + '</span>';
    if (breach != null) html += '<span style="font-size:12px;opacity:.6">Breach: ' + breach.toFixed(1) + '%</span>';
    if (isPreferred) html += '<span style="background:#16a34a22;color:#16a34a;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600">RECOMMENDED</span>';
    html += '</div>';

    html += '<div style="overflow-x:auto"><table class="eventsTable" style="width:100%;font-size:12px">';
    html += '<thead><tr><th>Wing</th><th class="num">Full Loss %</th><th class="num">E[Loss]</th><th class="num">Credit</th><th class="num">Max Loss</th><th class="num">ROC %</th><th class="num">Risk-Adj ROC</th><th class="num">Obs</th><th>Label</th></tr></thead><tbody>';

    for (var ri = 0; ri < rows.length; ri++) {
      var r = rows[ri];
      html += '<tr>';
      html += '<td>$' + r.wingWidthPts + '</td>';
      html += '<td class="num">' + (r.fullLossPct != null ? r.fullLossPct.toFixed(1) + '%' : '—') + '</td>';
      html += '<td class="num">' + (r.expectedLoss != null ? '$' + r.expectedLoss.toFixed(2) : '—') + '</td>';
      html += '<td class="num">' + (r.creditProxy != null ? '$' + r.creditProxy.toFixed(2) : '—') + '</td>';
      html += '<td class="num">' + (r.maxLoss != null ? '$' + r.maxLoss.toFixed(0) : '—') + '</td>';
      html += '<td class="num">' + (r.rocPct != null ? r.rocPct.toFixed(1) + '%' : '—') + '</td>';
      html += '<td class="num">' + (r.riskAdjRocPct != null ? r.riskAdjRocPct.toFixed(1) + '%' : '—') + '</td>';
      html += '<td class="num">' + (r.totalObs || '—') + '</td>';
      html += '<td>' + (r.label || '—') + '</td>';
      html += '</tr>';
    }
    html += '</tbody></table></div></div>';
  }
  html += '</div>';
  return html;
}

async function _runE1Advisor(payload) {
  var btn = $("e1RunAdvisorBtn");
  if (btn) { btn.disabled = true; btn.textContent = "Analyzing…"; }
  var area = $("e1AdvisorResultArea");
  if (area) area.innerHTML = '<div style="padding:16px;text-align:center;opacity:.6">Running LLM advisor…</div>';

  try {
    var ticker = payload?.ticker || "";
    var resp = await fetch("/api/breach/advisor", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker: ticker, n: payload?.params?.n || 20, years: payload?.params?.years || 5 }),
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    var data = await resp.json();
    _e1AdvisorResult = data;
    _renderE1AdvisorResult(data);
  } catch (e) {
    if (area) area.innerHTML = '<div style="padding:16px;color:#dc2626">Advisor failed: ' + e.message + '</div>';
  }
  if (btn) { btn.disabled = false; btn.textContent = "Run Advisor"; }
}

function _renderE1AdvisorResult(data) {
  var area = $("e1AdvisorResultArea");
  if (!area || !data) return;
  var a = data.advisor || {};
  var verdict = a.verdict || "PASS";
  var vColor = verdict === "TRADE" ? "#16a34a" : verdict === "LEAN_PASS" ? "#ca8a04" : "#dc2626";

  var html = '<div style="border-top:1px solid var(--border)">';

  // Verdict banner
  html += '<div style="padding:16px;background:' + vColor + '18;display:flex;align-items:center;gap:16px;flex-wrap:wrap">';
  html += '<span style="font-size:22px;font-weight:800;color:' + vColor + '">' + verdict + '</span>';
  if (a.confidence != null) html += '<span style="opacity:.7;font-size:13px">Confidence: ' + a.confidence + '/100</span>';
  if (a._source) html += '<span style="opacity:.5;font-size:11px">Powered by LLM · ' + (a._model || "") + '</span>';
  html += '</div>';

  // Trade ticket
  var t = a.tradeTicket || {};
  if (t.shortPutStrike || t.shortCallStrike) {
    html += '<div style="padding:12px 16px;border-bottom:1px solid var(--border);font-size:13px">';
    html += '<div style="font-weight:700;margin-bottom:6px;text-transform:uppercase;font-size:11px;opacity:.5">Trade Ticket</div>';
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px">';
    var fields = [
      ["Ticker", t.ticker],
      ["Earnings", t.earningsDate],
      ["Timing", t.earningsTiming],
      ["Short Put", t.shortPutStrike],
      ["Long Put", t.longPutStrike],
      ["Short Call", t.shortCallStrike],
      ["Long Call", t.longCallStrike],
      ["Wing", "$" + (t.wingWidth || "")],
      ["EM", (t.emMultiple || "") + "x"],
      ["Credit", t.estimatedCredit],
      ["Max Loss", t.maxLoss],
    ];
    for (var fi = 0; fi < fields.length; fi++) {
      if (fields[fi][1] != null && fields[fi][1] !== "") {
        html += '<div><span style="opacity:.5;font-size:10px;text-transform:uppercase">' + fields[fi][0] + '</span><br><b>' + fields[fi][1] + '</b></div>';
      }
    }
    html += '</div>';
    if (t.entryWindow) html += '<div style="margin-top:8px;font-size:12px"><b>Entry:</b> ' + t.entryWindow + '</div>';
    if (t.exitTarget) html += '<div style="font-size:12px"><b>Exit:</b> ' + t.exitTarget + '</div>';
    html += '</div>';
  }

  // Rationale sections
  var sections = [
    ["VRP Rationale", a.vrpRationale],
    ["Wing Width Rationale", a.wingWidthRationale],
    ["Risk Context", a.riskContext],
    ["Entry Plan", a.entryPlan],
    ["Management Plan", a.managementPlan],
    ["Exit Rules", a.exitRules],
    ["Pass Reason", a.passReason],
    ["Desk Note", a.deskNote],
  ];
  for (var si = 0; si < sections.length; si++) {
    if (sections[si][1]) {
      html += '<div style="padding:8px 16px;border-bottom:1px solid var(--border);font-size:13px">';
      html += '<div style="font-weight:700;font-size:11px;text-transform:uppercase;opacity:.5;margin-bottom:4px">' + sections[si][0] + '</div>';
      html += '<div>' + sections[si][1] + '</div></div>';
    }
  }

  // Key risks
  if (Array.isArray(a.keyRisks) && a.keyRisks.length) {
    html += '<div style="padding:8px 16px;border-bottom:1px solid var(--border);font-size:13px">';
    html += '<div style="font-weight:700;font-size:11px;text-transform:uppercase;opacity:.5;margin-bottom:4px">Key Risks</div>';
    html += '<ul style="margin:0;padding-left:16px">';
    for (var ki = 0; ki < a.keyRisks.length; ki++) {
      html += '<li>' + a.keyRisks[ki] + '</li>';
    }
    html += '</ul></div>';
  }

  // Log buttons (only for TRADE / LEAN_PASS)
  if (verdict === "TRADE" || verdict === "LEAN_PASS") {
    html += '<div style="padding:12px 16px;display:flex;gap:12px;flex-wrap:wrap">';
    html += '<button id="e1LogTradeBtn" class="primaryButton" style="padding:8px 20px;font-size:12px;font-weight:600">Log This Trade</button>';
    html += '<button id="e1AdjustLogBtn2" class="primaryButton" style="padding:8px 20px;font-size:12px;background:var(--surface2);color:var(--text);border:1px solid var(--border)">Adjust & Log</button>';
    html += '</div>';
  }

  html += '</div>';
  area.innerHTML = html;

  var logBtn = $("e1LogTradeBtn");
  if (logBtn) logBtn.addEventListener("click", function() { _logE1AdvisorTrade(data); });
  var adjBtn2 = $("e1AdjustLogBtn2");
  if (adjBtn2) adjBtn2.addEventListener("click", function() { _showE1AdjustModal(lastPayload, data); });
}

// --- Direct trade log (from desk consensus, no LLM) ---
async function _logE1DirectTrade(payload) {
  var dc = payload?.e1DeskConsensus || {};
  var emp = payload?.e1EmPreference || {};
  var vrp = payload?.vrpAnalysis || {};
  var cur = payload?.current || {};
  var ticker = payload?.ticker || "";
  var emMult = emp.preferredEm || dc.preferredEm || 2.0;
  var wing = 5;
  var timing = (cur.earningsTiming || "AMC").toUpperCase();

  var body = {
    source: "desk_consensus",
    ticker: ticker,
    entry: {
      emMultiple: emMult,
      wingWidth: wing,
      earningsDate: cur.earningsDate || cur.earnDateNext || null,
      earningsTiming: timing,
      entryWindow: timing === "AMC" ? "3:00 PM EST on earnings day" : "3:00 PM EST day before earnings",
      exitTarget: "Next morning open or mid-day vol bleed",
    },
    entryContext: {
      vrpScore: vrp.vrpScore,
      breachPct: (payload?.e1EmBreachSummary || {})[String(emMult)] || null,
      earningsTiming: timing,
      regimeBucket: (payload?.regime || {}).regimeBucket || (payload?.regime || {}).bucket,
    },
    advisorVerdict: { verdict: dc.verdict, confidence: null, source: "desk_consensus" },
  };

  try {
    var resp = await fetch("/api/breach/trade", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    var result = await resp.json();
    alert("Trade logged: " + ticker + " · " + emMult + "x EM · $" + wing + " wings\nTrade ID: " + result.tradeId);
    _loadE1ActiveTrades();
    _loadE1TradeJournal();
  } catch (e) {
    alert("Failed to log trade: " + e.message);
  }
}

// --- Advisor-sourced trade log ---
async function _logE1AdvisorTrade(advisorData) {
  var a = (advisorData.advisor || {});
  var ticket = a.tradeTicket || {};
  var body = {
    source: "advisor",
    ticker: ticket.ticker || lastPayload?.ticker || "",
    entry: {
      shortPutStrike: ticket.shortPutStrike,
      longPutStrike: ticket.longPutStrike,
      shortCallStrike: ticket.shortCallStrike,
      longCallStrike: ticket.longCallStrike,
      wingWidth: ticket.wingWidth,
      emMultiple: ticket.emMultiple,
      entryCredit: parseFloat(String(ticket.estimatedCredit || "0").replace(/[^0-9.]/g, "")) || 0,
      earningsDate: ticket.earningsDate,
      earningsTiming: ticket.earningsTiming,
      entryWindow: ticket.entryWindow,
      exitTarget: ticket.exitTarget,
    },
    entryContext: {
      vrpScore: (advisorData.vrpAnalysis || {}).vrpScore,
      breachPct: (advisorData.emBreachSummary || {})[String(ticket.emMultiple)] || null,
      earningsTiming: ticket.earningsTiming,
      regimeBucket: (lastPayload?.regime || {}).regimeBucket || (lastPayload?.regime || {}).bucket,
    },
    advisorVerdict: { verdict: a.verdict, confidence: a.confidence },
  };

  try {
    var resp = await fetch("/api/breach/trade", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    var result = await resp.json();
    alert("Trade logged: " + (ticket.ticker || lastPayload?.ticker) + " · " + result.tradeId);
    _loadE1ActiveTrades();
    _loadE1TradeJournal();
  } catch (e) {
    alert("Failed to log trade: " + e.message);
  }
}

// --- Adjust & Log modal ---
function _showE1AdjustModal(payload, advisorData) {
  var dc = payload?.e1DeskConsensus || {};
  var emp = payload?.e1EmPreference || {};
  var cur = payload?.current || {};
  var ticker = payload?.ticker || "";
  var defEm = (advisorData?.advisor?.tradeTicket?.emMultiple) || emp.preferredEm || dc.preferredEm || 2.0;
  var defWing = (advisorData?.advisor?.tradeTicket?.wingWidth) || 5;
  var defCredit = (advisorData?.advisor?.tradeTicket?.estimatedCredit) || "";
  var timing = (cur.earningsTiming || "AMC").toUpperCase();
  var earnDate = cur.earningsDate || cur.earnDateNext || "";

  var overlay = document.createElement("div");
  overlay.id = "e1AdjustOverlay";
  overlay.style.cssText = "position:fixed;inset:0;z-index:10001;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center";
  overlay.innerHTML = '<div style="background:var(--bg,#fff);border-radius:12px;padding:24px;width:420px;max-width:90vw;max-height:85vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.3)">'
    + '<h3 style="margin:0 0 16px">Adjust & Log Trade — ' + ticker + '</h3>'
    + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:13px">'
    + _adjField("EM Multiple", "e1AdjEm", defEm, "number", "0.5", "3.0", "0.5")
    + _adjField("Wing Width ($)", "e1AdjWing", defWing, "number", "1", "25", "0.5")
    + _adjField("Entry Credit ($)", "e1AdjCredit", String(defCredit).replace(/[^0-9.]/g, ""), "number", "0", "999", "0.01")
    + _adjField("Short Put Strike", "e1AdjSP", (advisorData?.advisor?.tradeTicket?.shortPutStrike) || "", "number", "", "", "0.5")
    + _adjField("Short Call Strike", "e1AdjSC", (advisorData?.advisor?.tradeTicket?.shortCallStrike) || "", "number", "", "", "0.5")
    + _adjField("Timing", "e1AdjTiming", timing, "select", "", "", "", ["AMC", "BMO"])
    + _adjField("Earnings Date", "e1AdjDate", earnDate, "date")
    + '</div>'
    + '<div style="margin-top:12px"><label style="font-size:12px;opacity:.7">Notes</label><textarea id="e1AdjNotes" rows="2" style="width:100%;font-size:12px;padding:6px;border-radius:6px;border:1px solid var(--border,#ddd);margin-top:4px" placeholder="Optional adjustment notes..."></textarea></div>'
    + '<div style="display:flex;gap:12px;margin-top:20px;justify-content:flex-end">'
    + '<button id="e1AdjCancelBtn" style="padding:8px 20px;font-size:12px;border-radius:6px;border:1px solid var(--border,#ddd);background:none;cursor:pointer">Cancel</button>'
    + '<button id="e1AdjSubmitBtn" class="primaryButton" style="padding:8px 20px;font-size:12px;font-weight:600">Log Adjusted Trade</button>'
    + '</div></div>';

  document.body.appendChild(overlay);

  $("e1AdjCancelBtn").addEventListener("click", function () { overlay.remove(); });
  overlay.addEventListener("click", function (ev) { if (ev.target === overlay) overlay.remove(); });

  $("e1AdjSubmitBtn").addEventListener("click", async function () {
    var emVal = parseFloat($("e1AdjEm").value) || defEm;
    var wingVal = parseFloat($("e1AdjWing").value) || defWing;
    var creditVal = parseFloat($("e1AdjCredit").value) || 0;
    var spVal = parseFloat($("e1AdjSP").value) || null;
    var scVal = parseFloat($("e1AdjSC").value) || null;
    var timingVal = $("e1AdjTiming").value || timing;
    var dateVal = $("e1AdjDate").value || earnDate;
    var notes = ($("e1AdjNotes").value || "").trim();

    var body = {
      source: "adjusted",
      ticker: ticker,
      entry: {
        emMultiple: emVal,
        wingWidth: wingVal,
        entryCredit: creditVal,
        shortPutStrike: spVal,
        shortCallStrike: scVal,
        longPutStrike: spVal ? spVal - wingVal : null,
        longCallStrike: scVal ? scVal + wingVal : null,
        earningsDate: dateVal,
        earningsTiming: timingVal,
        entryWindow: timingVal === "AMC" ? "3:00 PM EST on earnings day" : "3:00 PM EST day before earnings",
        exitTarget: "Next morning open or mid-day vol bleed",
      },
      entryContext: {
        vrpScore: (payload?.vrpAnalysis || {}).vrpScore,
        breachPct: (payload?.e1EmBreachSummary || {})[String(emVal)] || null,
        earningsTiming: timingVal,
        regimeBucket: (payload?.regime || {}).regimeBucket || (payload?.regime || {}).bucket,
      },
      advisorVerdict: advisorData ? { verdict: (advisorData.advisor || {}).verdict, confidence: (advisorData.advisor || {}).confidence } : { verdict: dc.verdict, source: "desk_consensus" },
      adjustmentNote: notes || "Manual adjustment from desk consensus",
      originalTicket: advisorData ? (advisorData.advisor || {}).tradeTicket : null,
    };

    try {
      var resp = await fetch("/api/breach/trade", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      var result = await resp.json();
      overlay.remove();
      alert("Adjusted trade logged: " + ticker + " · " + emVal + "x EM · $" + wingVal + " wings\nTrade ID: " + result.tradeId);
      _loadE1ActiveTrades();
      _loadE1TradeJournal();
    } catch (e) {
      alert("Failed to log trade: " + e.message);
    }
  });
}

function _adjField(label, id, defVal, type, min, max, step, options) {
  var html = '<div><label for="' + id + '" style="font-size:11px;opacity:.7;display:block;margin-bottom:3px">' + label + '</label>';
  if (type === "select" && options) {
    html += '<select id="' + id + '" style="width:100%;padding:6px;font-size:12px;border-radius:6px;border:1px solid var(--border,#ddd)">';
    for (var oi = 0; oi < options.length; oi++) {
      var sel = options[oi] === defVal ? " selected" : "";
      html += '<option value="' + options[oi] + '"' + sel + '>' + options[oi] + '</option>';
    }
    html += '</select>';
  } else {
    html += '<input id="' + id + '" type="' + (type || "text") + '" value="' + (defVal != null ? defVal : "") + '"'
      + (min ? ' min="' + min + '"' : '') + (max ? ' max="' + max + '"' : '') + (step ? ' step="' + step + '"' : '')
      + ' style="width:100%;padding:6px;font-size:12px;border-radius:6px;border:1px solid var(--border,#ddd)">';
  }
  html += '</div>';
  return html;
}

// --- Active Trades (monitoring + close) ---
async function _loadE1ActiveTrades() {
  var areaInAdvisor = $("e1ActiveTradesArea");
  var sec = $("e1ActiveTrades");
  var el = $("e1ActiveTradesContent");

  try {
    var resp = await fetch("/api/breach/trades");
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    var data = await resp.json();
    var trades = data.trades || [];

    if (!trades.length) {
      if (sec) sec.classList.add("hidden");
      if (areaInAdvisor) areaInAdvisor.innerHTML = "";
      return;
    }

    var html = '<div style="padding:16px;border-top:2px solid var(--border)">';
    html += '<div style="display:flex;align-items:baseline;gap:12px;margin-bottom:16px">';
    html += '<h3 style="margin:0;font-size:15px;font-weight:700">Active Trades</h3>';
    html += '<span style="font-size:12px;opacity:.6">' + trades.length + ' open position' + (trades.length > 1 ? 's' : '') + '</span>';
    html += '</div>';

    for (var ti = 0; ti < trades.length; ti++) {
      var t = trades[ti];
      var entry = t.entry || {};
      var ctx = t.entryContext || {};
      html += '<div style="background:var(--surface2,#f8f9fa);border-radius:8px;padding:12px 16px;margin-bottom:12px;border:1px solid var(--border)">';

      // Trade header
      html += '<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px">';
      html += '<span style="font-weight:700;font-size:14px">' + (t.ticker || "—") + '</span>';
      html += '<span style="font-size:12px;opacity:.6">' + (entry.emMultiple || "?") + 'x EM · $' + (entry.wingWidth || "?") + ' wings</span>';
      html += '<span style="font-size:12px;opacity:.6">' + (entry.earningsTiming || "?") + ' · ' + (entry.earningsDate || "?") + '</span>';
      html += '<span style="font-size:11px;opacity:.4">Logged ' + (t.loggedAt || "").slice(0, 16).replace("T", " ") + '</span>';
      html += '</div>';

      // Entry details
      if (entry.shortPutStrike || entry.shortCallStrike) {
        html += '<div style="font-size:12px;opacity:.7;margin-bottom:8px">';
        html += 'Strikes: ' + (entry.shortPutStrike || "?") + '/' + (entry.longPutStrike || "?") + ' put · ' + (entry.shortCallStrike || "?") + '/' + (entry.longCallStrike || "?") + ' call';
        if (entry.entryCredit) html += ' · Credit: $' + entry.entryCredit;
        html += '</div>';
      }
      if (entry.entryWindow) html += '<div style="font-size:11px;opacity:.5;margin-bottom:4px">Entry: ' + entry.entryWindow + '</div>';
      if (entry.exitTarget) html += '<div style="font-size:11px;opacity:.5;margin-bottom:8px">Exit: ' + entry.exitTarget + '</div>';

      // Action buttons
      html += '<div style="display:flex;gap:8px;flex-wrap:wrap">';
      html += '<button class="e1CloseTradeBtn primaryButton" data-trade-id="' + t.tradeId + '" style="padding:5px 14px;font-size:11px;font-weight:600">Close Trade</button>';
      html += '<button class="e1CloseWinBtn" data-trade-id="' + t.tradeId + '" style="padding:5px 14px;font-size:11px;border-radius:6px;border:1px solid #16a34a;background:#16a34a18;color:#16a34a;cursor:pointer;font-weight:600">Close as Win</button>';
      html += '<button class="e1CloseLossBtn" data-trade-id="' + t.tradeId + '" style="padding:5px 14px;font-size:11px;border-radius:6px;border:1px solid #dc2626;background:#dc262618;color:#dc2626;cursor:pointer;font-weight:600">Close as Loss</button>';
      html += '<button class="e1CloseExpBtn" data-trade-id="' + t.tradeId + '" style="padding:5px 14px;font-size:11px;border-radius:6px;border:1px solid var(--border);background:none;cursor:pointer;opacity:.7">Expired Worthless</button>';
      html += '</div>';

      html += '</div>';
    }
    html += '</div>';

    // Render in the inline area (inside advisor section)
    if (areaInAdvisor) areaInAdvisor.innerHTML = html;
    // Also update the standalone section
    if (sec && el) { sec.classList.add("hidden"); }

    // Wire close buttons
    _wireE1CloseButtons();
  } catch (e) {
    if (sec) sec.classList.add("hidden");
  }
}

function _wireE1CloseButtons() {
  document.querySelectorAll(".e1CloseTradeBtn").forEach(function (btn) {
    btn.addEventListener("click", function () { _showE1CloseModal(btn.dataset.tradeId); });
  });
  document.querySelectorAll(".e1CloseWinBtn").forEach(function (btn) {
    btn.addEventListener("click", function () { _quickCloseE1Trade(btn.dataset.tradeId, "win"); });
  });
  document.querySelectorAll(".e1CloseLossBtn").forEach(function (btn) {
    btn.addEventListener("click", function () { _quickCloseE1Trade(btn.dataset.tradeId, "loss"); });
  });
  document.querySelectorAll(".e1CloseExpBtn").forEach(function (btn) {
    btn.addEventListener("click", function () { _quickCloseE1Trade(btn.dataset.tradeId, "expired"); });
  });
}

async function _quickCloseE1Trade(tradeId, outcome) {
  var labels = { "win": "WIN (full credit kept)", "loss": "LOSS", "expired": "Expired Worthless (WIN)" };
  if (!confirm("Close trade " + tradeId + " as " + (labels[outcome] || outcome) + "?")) return;

  var body = { closeReason: outcome === "expired" ? "expired_worthless" : "manual" };
  if (outcome === "win" || outcome === "expired") {
    body.exitCredit = 0;
    body.expiredWorthless = outcome === "expired";
  }

  try {
    var resp = await fetch("/api/breach/trade/" + tradeId + "/close", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    _loadE1ActiveTrades();
    _loadE1TradeJournal();
  } catch (e) {
    alert("Close failed: " + e.message);
  }
}

function _showE1CloseModal(tradeId) {
  var overlay = document.createElement("div");
  overlay.style.cssText = "position:fixed;inset:0;z-index:10001;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center";
  overlay.innerHTML = '<div style="background:var(--bg,#fff);border-radius:12px;padding:24px;width:380px;max-width:90vw;box-shadow:0 20px 60px rgba(0,0,0,.3)">'
    + '<h3 style="margin:0 0 16px">Close Trade · ' + tradeId + '</h3>'
    + '<div style="display:grid;gap:12px;font-size:13px">'
    + _adjField("Exit Debit ($)", "e1CloseDebit", "0", "number", "0", "9999", "0.01")
    + _adjField("Close Reason", "e1CloseReason", "manual", "select", "", "", "", ["manual", "stop_loss", "target_hit", "expired_worthless", "vol_bleed_complete"])
    + '</div>'
    + '<div style="margin-top:12px"><label style="font-size:12px;opacity:.7">Notes</label><textarea id="e1CloseNotes" rows="2" style="width:100%;font-size:12px;padding:6px;border-radius:6px;border:1px solid var(--border,#ddd);margin-top:4px" placeholder="Exit notes..."></textarea></div>'
    + '<div style="display:flex;gap:12px;margin-top:20px;justify-content:flex-end">'
    + '<button id="e1CloseCancelBtn" style="padding:8px 20px;font-size:12px;border-radius:6px;border:1px solid var(--border,#ddd);background:none;cursor:pointer">Cancel</button>'
    + '<button id="e1CloseSubmitBtn" class="primaryButton" style="padding:8px 20px;font-size:12px;font-weight:600">Close Trade</button>'
    + '</div></div>';

  document.body.appendChild(overlay);
  document.getElementById("e1CloseCancelBtn").addEventListener("click", function () { overlay.remove(); });
  overlay.addEventListener("click", function (ev) { if (ev.target === overlay) overlay.remove(); });

  document.getElementById("e1CloseSubmitBtn").addEventListener("click", async function () {
    var debit = parseFloat(document.getElementById("e1CloseDebit").value) || 0;
    var reason = document.getElementById("e1CloseReason").value || "manual";
    var notes = (document.getElementById("e1CloseNotes").value || "").trim();

    try {
      var resp = await fetch("/api/breach/trade/" + tradeId + "/close", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ exitCredit: debit, closeReason: reason, notes: notes, expiredWorthless: reason === "expired_worthless" }),
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      overlay.remove();
      _loadE1ActiveTrades();
      _loadE1TradeJournal();
    } catch (e) {
      alert("Close failed: " + e.message);
    }
  });
}

// ---------------------------------------------------------------------------
// Engine 1 — Trade Journal
// ---------------------------------------------------------------------------

async function _loadE1TradeJournal() {
  var sec = $("e1TradeJournal");
  var el = $("e1TradeJournalContent");
  if (!sec || !el) return;

  try {
    var histResp = await fetch("/api/breach/trades/history?limit=20");
    var perfResp = await fetch("/api/breach/trades/performance");
    if (!histResp.ok || !perfResp.ok) { sec.classList.add("hidden"); return; }
    var hist = await histResp.json();
    var perf = await perfResp.json();

    if (!perf.hasData && !(hist.trades && hist.trades.length)) {
      sec.classList.add("hidden");
      return;
    }

    sec.classList.remove("hidden");
    var html = '<div class="taPanel">';
    html += '<div class="taHeader"><div class="taHeaderRow"><span class="taHeaderTitle">Trade Journal</span>';
    html += '<span style="font-size:12px;opacity:.6">Learning System · ' + (perf.totalClosed || 0) + ' closed trades</span>';
    html += '</div></div>';

    // Performance Stats
    if (perf.hasData) {
      html += '<div style="padding:16px;display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:12px;border-bottom:1px solid var(--border)">';
      html += _vrpCard("Win Rate", perf.winRate != null ? perf.winRate + "%" : "—", "", perf.winRate >= 60 ? "#16a34a" : perf.winRate >= 40 ? "#ca8a04" : "#dc2626");
      html += _vrpCard("Total P&L", perf.totalPnl != null ? "$" + perf.totalPnl.toFixed(2) : "—", "", null);
      html += _vrpCard("Avg P&L", perf.avgPnl != null ? "$" + perf.avgPnl.toFixed(2) : "—", "", null);
      html += _vrpCard("W-L-S", perf.wins + "-" + perf.losses + "-" + perf.scratches, "", null);
      html += _vrpCard("Tendency", perf.riskTendency || "—", "", null);
      html += '</div>';

      // Bucketed breakdowns
      var buckets = [
        ["By EM", perf.byEm],
        ["By Wing", perf.byWing],
        ["By VRP", perf.byVrpBucket],
        ["By Timing", perf.byTiming],
        ["By Regime", perf.byRegime],
      ];
      html += '<div style="padding:12px 16px;display:flex;gap:16px;flex-wrap:wrap;border-bottom:1px solid var(--border);font-size:12px">';
      for (var bi = 0; bi < buckets.length; bi++) {
        var bData = buckets[bi][1];
        if (!bData || !Object.keys(bData).length) continue;
        html += '<div><div style="font-weight:700;font-size:10px;text-transform:uppercase;opacity:.5;margin-bottom:4px">' + buckets[bi][0] + '</div>';
        var bKeys = Object.keys(bData);
        for (var bki = 0; bki < bKeys.length; bki++) {
          var bd = bData[bKeys[bki]];
          html += '<div>' + bKeys[bki] + ': ' + (bd.winRate != null ? bd.winRate + '%' : '—') + ' (' + bd.n + ')</div>';
        }
        html += '</div>';
      }
      html += '</div>';
    }

    // Recent Closed Trades
    var trades = hist.trades || [];
    if (trades.length) {
      html += '<div style="padding:12px 16px"><div style="font-weight:700;font-size:11px;text-transform:uppercase;opacity:.5;margin-bottom:8px">Recent Closed Trades</div>';
      for (var ti = 0; ti < Math.min(trades.length, 10); ti++) {
        var tr = trades[ti];
        var oc = (tr.outcome || {}).outcomeClass || "?";
        var pnl = (tr.outcome || {}).realizedPnl;
        var ocColor = oc === "win" ? "#16a34a" : oc === "loss" ? "#dc2626" : "#888";
        html += '<div style="display:flex;align-items:center;gap:12px;padding:4px 0;font-size:12px">';
        html += '<span style="background:' + ocColor + '22;color:' + ocColor + ';padding:2px 8px;border-radius:4px;font-weight:600;text-transform:uppercase;font-size:10px">' + oc + '</span>';
        html += '<span style="font-weight:600">' + (tr.ticker || "—") + '</span>';
        html += '<span>' + ((tr.entry || {}).emMultiple || "?") + 'x EM · $' + ((tr.entry || {}).wingWidth || "?") + ' wings</span>';
        if (pnl != null) html += '<span style="color:' + (pnl >= 0 ? "#16a34a" : "#dc2626") + ';font-weight:600">' + (pnl >= 0 ? "+" : "") + '$' + pnl.toFixed(2) + '</span>';
        html += '<span style="opacity:.5">' + (tr.closedAt || "").slice(0, 10) + '</span>';
        html += '</div>';
      }
      html += '</div>';
    }

    html += '</div>';
    el.innerHTML = html;
  } catch (e) {
    sec.classList.add("hidden");
  }
}

function setStatus(text, isError = false) {
  const el = $("status");
  el.textContent = text || "";
  el.classList.toggle("isError", !!isError);
  el.classList.toggle("isRunning", !isError && !!text && String(text).includes("…"));
  el.classList.toggle("isOk", !isError && String(text || "").toUpperCase() === "OK");
}

function setBusy(busy, statusMsg) {
  isBusy = !!busy;
  const btn = $("submit");
  const form = $("form");
  const ticker = $("ticker");
  const k = $("k");
  if (btn) btn.disabled = isBusy;
  if (ticker) ticker.disabled = isBusy;
  if (k) k.disabled = isBusy;
  if (form) form.classList.toggle("isLoading", isBusy);
  
  // Raven Loading Overlay
  if (window.RavenLoading) {
    if (isBusy) {
      window.RavenLoading.show({ status: statusMsg || "Analyzing ticker..." });
    } else {
      window.RavenLoading.hide();
    }
  }
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
  const qsK = (qs.get("k") || "").trim();
  const qsMc = String(qs.get("mc") || "").trim().toLowerCase();
  const qsAutorun = String(qs.get("autorun") || "").trim().toLowerCase();

  const form = $("form");
  const ticker = $("ticker");
  const kSel = $("k");
  ticker.value = (qsTicker || ticker.value || "AAPL").toUpperCase();
  setTickerLogo(ticker.value);

  // Apply k from querystring if valid
  if (kSel && qsK && ["1.0", "1.5", "2.0"].includes(qsK)) {
    kSel.value = qsK;
  }

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

    setBusy(true, `Analyzing ${t}...`);
    if (!cached?.isStale) {
      setStatus(`Computing with k=${k}…`);
    }
    
    // Progress updates
    if (window.RavenLoading) {
      window.RavenLoading.setProgress(15, `Fetching data for ${t}...`);
    }

    try {
      const payload = await fetchJson(url);
      
      if (window.RavenLoading) {
        window.RavenLoading.setProgress(70, "Processing results...");
      }
      
      render(payload);
      
      if (window.RavenLoading) {
        window.RavenLoading.setProgress(90, "Loading charts...");
      }
      
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

  initTooltips();
  try { window.RavenUI?.initInfoTips?.(); } catch { /* ignore */ }
  initEngine1GammaVizUI();

  // ---------------------------------------------------------------------------
  // Desk Insight Popup — LLM-powered card insights for Engine 1
  // ---------------------------------------------------------------------------

  var e1Popup = $("e1InsightPopup");
  initDrag(e1Popup, $("e1InsightHeader"), { closeSelector: "#e1InsightClose" });
  var e1Close = $("e1InsightClose");
  if (e1Close) e1Close.addEventListener("click", function () { e1Popup.style.display = "none"; });

  var e1Insight = new InsightPopup({
    popupEl: e1Popup,
    titleEl: $("e1InsightTitle"),
    bodyEl:  $("e1InsightBody"),
    prefix:  "e1Insight",
    labels: {
      decision_summary:"Decision Summary",key_risks:"Key Risks",what_to_watch:"What to Watch",execution_guidance:"Execution Guidance",
      the_setup:"The Setup",what_can_hurt_you:"What Can Hurt You",catalyst_calendar:"Catalyst Calendar",how_to_structure_it:"How to Structure It",the_call:"The Call",
      hold_risk_assessment:"Hold Risk Assessment",conditional_vs_unconditional:"Conditional vs Unconditional",drift_analysis:"Drift Analysis",structure_implications:"Structure Implications",
      what_simulation_says:"What the Simulation Says",put_vs_call_skew:"Put vs Call Skew",tail_risk:"Tail Risk",wing_optimization:"Wing Optimization",
      regime_read:"Regime Read",gate_implications:"Gate Implications",tail_multiplier_impact:"Tail Multiplier Impact",
      skew_read:"Skew Read",wing_recommendation:"Wing Recommendation",directional_risk:"Directional Risk",structure_selection:"Structure Selection",
      event_risk_level:"Event Risk Level",top_drivers:"Top Drivers",impact_on_trade:"Impact on Trade",
      dealer_positioning:"Dealer Positioning",tail_ignition_risk:"Tail Ignition Risk",gamma_earnings_interaction:"Gamma-Earnings Interaction",
      seasonal_pattern:"Seasonal Pattern",current_quarter:"Current Quarter",statistical_significance:"Statistical Significance",
      strike_map:"Strike Map",symmetric_vs_asymmetric:"Symmetric vs Asymmetric",tail_multiplier_effect:"Tail Multiplier Effect",
      oi_clusters:"OI Clusters",trade_implications:"Trade Implications",
      iv_read:"IV Read",earnings_context:"Earnings Context",z_score_significance:"Z-Score Significance",risk_implication:"Risk Implication",
      median_richness:"Median Richness",tail_richness:"Tail Richness",premium_quality:"Premium Quality",structure_guidance:"Structure Guidance",
      dollar_volume:"Dollar Volume",spread_quality:"Spread Quality",oi_coverage:"OI & Coverage",execution_risk:"Execution Risk",
      dealer_gamma_backdrop:"Dealer Gamma Backdrop",index_sensitivity:"Index Sensitivity",vol_acceleration:"Vol Acceleration",tail_risks:"Tail Risks",
      desk_takeaway:"Desk Takeaway",
    },
  });

  function e1FetchInsight(cardType, cardData, title, x, y) {
    var ctx = {};
    if (lastPayload) {
      ctx.ticker = lastPayload.ticker;
      ctx.regime = lastPayload.regime || {};
      ctx.summary = lastPayload.summary || {};
      ctx.current = lastPayload.current || {};
    }
    e1Insight.fetch(cardType, cardData, title, x, y, ctx);
  }

  // ── Click: Decision Panel ──
  var e1DecisionEl = $("e1DecisionSection");
  if (e1DecisionEl) {
    e1DecisionEl.classList.add("e1Click");
    e1DecisionEl.title = "Click for desk insight";
    e1DecisionEl.addEventListener("click", function (ev) {
      if (ev.target.closest("button, a, input, .tipWrap, .info")) return;
      if (!lastPayload) return;
      var data = {
        goNoGo: lastPayload.goNoGo || {},
        summary: lastPayload.summary || {},
        ticker: lastPayload.ticker,
        expectedMove: lastPayload.expectedMove || {},
        current: lastPayload.current || {},
        strikeTargets: lastPayload.strikeTargets || {},
        gapVsCtc: lastPayload.gapVsCtc || {},
        earningsHoldRisk: lastPayload.earningsHoldRisk || {},
        baseline: lastPayload.baseline || {},
        regime: lastPayload.regime || {},
        wingRecommendation: lastPayload.wingRecommendation || {},
        skewOverlay: lastPayload.skewOverlay || {},
        eventRisk: lastPayload.eventRisk || {},
        monteCarlo: lastPayload.monteCarlo || {},
        earningsGammaContext: lastPayload.earningsGammaContext || {},
        quarters: lastPayload.quarters || {},
        technicals: lastPayload.technicals || {},
      };
      e1FetchInsight("e1_decision", data, "Decision Panel: " + (lastPayload.ticker || ""), ev.clientX, ev.clientY);
    });
  }

  // ── Click: Earnings Hold Risk ──
  var holdRiskEl = $("holdRiskSection");
  if (holdRiskEl) {
    holdRiskEl.classList.add("e1Click");
    holdRiskEl.title = "Click for desk insight";
    holdRiskEl.addEventListener("click", function (ev) {
      if (ev.target.closest("button, a, .tipWrap, .info")) return;
      if (!lastPayload || !lastPayload.earningsHoldRisk) return;
      e1FetchInsight("e1_hold_risk", lastPayload.earningsHoldRisk, "Earnings Hold Risk: " + (lastPayload.ticker || ""), ev.clientX, ev.clientY);
    });
  }

  // ── Click: Monte Carlo ──
  var mcEl = $("mcSection");
  if (mcEl) {
    mcEl.classList.add("e1Click");
    mcEl.title = "Click for desk insight";
    mcEl.addEventListener("click", function (ev) {
      if (ev.target.closest("button, a, .tipWrap, .info")) return;
      if (!lastPayload) return;
      var data = { monteCarlo: lastPayload.monteCarlo || {}, monteCarloOptimization: lastPayload.monteCarloOptimization || {}, ticker: lastPayload.ticker };
      e1FetchInsight("e1_monte_carlo", data, "Monte Carlo: " + (lastPayload.ticker || ""), ev.clientX, ev.clientY);
    });
  }

  // ── Click: Regime Overlay ──
  // The regime section wraps child cards (gamma, strike targets, playbook).
  // Guard against clicks on those children — only trigger for the regime header area.
  var regimeBannerE1 = document.querySelector(".regimeOverlay, #regimeAsOf")?.closest("section, .surface");
  if (!regimeBannerE1) regimeBannerE1 = $("regimeAsOf")?.parentElement;
  if (regimeBannerE1) {
    regimeBannerE1.classList.add("e1Click");
    regimeBannerE1.title = "Click for desk insight";
    regimeBannerE1.addEventListener("click", function (ev) {
      // Ignore clicks on child cards, details, buttons, info tips
      if (ev.target.closest("#marketGammaCard, #tickerGammaCard, #bufferTargetCard, #playbookGrid, details, button, a, .tipWrap, .info, .segmented")) return;
      if (!lastPayload || !lastPayload.regime) return;
      e1FetchInsight("e1_regime", lastPayload.regime, "Regime: " + (lastPayload.regime.label || ""), ev.clientX, ev.clientY);
    });
  }

  // ── Click: Event Risk ──
  var eventRiskEl = $("eventRiskSection");
  if (eventRiskEl) {
    eventRiskEl.classList.add("e1Click");
    eventRiskEl.title = "Click for desk insight";
    eventRiskEl.addEventListener("click", function (ev) {
      if (ev.target.closest("button, a, .tipWrap, .info")) return;
      if (!lastPayload || !lastPayload.eventRisk) return;
      e1FetchInsight("e1_event_risk", lastPayload.eventRisk, "Event Risk", ev.clientX, ev.clientY);
    });
  }

  // ── Click: Skew & Wings ──
  var skewEl = $("skewWingsSection");
  if (skewEl) {
    skewEl.classList.add("e1Click");
    skewEl.title = "Click for desk insight";
    skewEl.addEventListener("click", function (ev) {
      if (ev.target.closest("button, a, .tipWrap, .info")) return;
      if (!lastPayload) return;
      var data = { wingRecommendation: lastPayload.wingRecommendation || {}, skewOverlay: lastPayload.skewOverlay || {}, ticker: lastPayload.ticker };
      e1FetchInsight("e1_skew_wings", data, "Skew & Wings: " + (lastPayload.ticker || ""), ev.clientX, ev.clientY);
    });
  }

  // ── Click: Earnings Gamma Context ──
  var gammaCtxEl = $("earningsGammaSection");
  if (gammaCtxEl) {
    gammaCtxEl.classList.add("e1Click");
    gammaCtxEl.title = "Click for desk insight";
    gammaCtxEl.addEventListener("click", function (ev) {
      if (ev.target.closest("button, a, .tipWrap, .info")) return;
      if (!lastPayload || !lastPayload.earningsGammaContext) return;
      e1FetchInsight("e1_gamma_context", lastPayload.earningsGammaContext, "Earnings Gamma Context", ev.clientX, ev.clientY);
    });
  }

  // ── Click: Strike Targets ──
  var strikeEl = $("bufferTargetCard");
  if (strikeEl) {
    strikeEl.classList.add("e1Click");
    strikeEl.title = "Click for desk insight";
    strikeEl.addEventListener("click", function (ev) {
      ev.stopPropagation();
      if (ev.target.closest("button, .segmented")) return;
      if (!lastPayload) return;
      var data = { strikeTargets: lastPayload.strikeTargets || {}, current: lastPayload.current || {}, regime: lastPayload.regime || {}, ticker: lastPayload.ticker };
      e1FetchInsight("e1_strike_targets", data, "Strike Targets: " + (lastPayload.ticker || ""), ev.clientX, ev.clientY);
    });
  }

  // ── Click: Market Dealer Gamma ──
  var mktGammaEl = $("marketGammaCard");
  if (mktGammaEl) {
    mktGammaEl.classList.add("e1Click");
    mktGammaEl.title = "Click for desk insight";
    mktGammaEl.addEventListener("click", function (ev) {
      ev.stopPropagation();
      if (!lastPayload || !lastPayload.marketDealerGamma) return;
      var data = lastPayload.marketDealerGamma;
      data._label = "Market (SPX)";
      e1FetchInsight("e1_dealer_gamma", data, "Market Dealer Gamma (SPX)", ev.clientX, ev.clientY);
    });
  }

  // ── Click: Ticker Dealer Gamma ──
  var tckGammaEl = $("tickerGammaCard");
  if (tckGammaEl) {
    tckGammaEl.classList.add("e1Click");
    tckGammaEl.title = "Click for desk insight";
    tckGammaEl.addEventListener("click", function (ev) {
      ev.stopPropagation();
      if (!lastPayload || !lastPayload.tickerDealerGamma) return;
      var data = lastPayload.tickerDealerGamma;
      data._label = (lastPayload.ticker || "Ticker") + " Dealer Gamma";
      e1FetchInsight("e1_dealer_gamma", data, (lastPayload.ticker || "Ticker") + " Dealer Gamma", ev.clientX, ev.clientY);
    });
  }

  // ── Click: Quarter Seasonality ──
  var qtrEl = $("quarterCards");
  if (qtrEl) {
    qtrEl.classList.add("e1Click");
    qtrEl.title = "Click for desk insight";
    qtrEl.addEventListener("click", function (ev) {
      if (ev.target.closest("button, a, .tipWrap, .info")) return;
      if (!lastPayload || !lastPayload.quarters) return;
      e1FetchInsight("e1_quarter", { quarters: lastPayload.quarters, ticker: lastPayload.ticker }, "Quarter Seasonality: " + (lastPayload.ticker || ""), ev.clientX, ev.clientY);
    });
  }

  // ── Click: Earnings Playbook Cards (Live Data dropdown) ──
  var pbGrid = $("playbookGrid");
  if (pbGrid) {
    pbGrid.addEventListener("click", function (ev) {
      ev.stopPropagation();
      var card = ev.target.closest(".taCard[data-e1-insight]");
      if (!card || !lastPayload) return;
      var insightType = card.getAttribute("data-e1-insight");
      var title = card.getAttribute("data-e1-title") || insightType;
      var go = lastPayload.goNoGo || {};
      var checks = Array.isArray(go.checks) ? go.checks : [];
      var checkMap = {};
      for (var ci = 0; ci < checks.length; ci++) checkMap[String(checks[ci].id || "")] = checks[ci];

      var cardData = {};
      if (insightType === "e1_iv_check") {
        cardData = { check: checkMap["SN_IV_ELEVATED"] || {}, ticker: lastPayload.ticker, regime: lastPayload.regime || {} };
      } else if (insightType === "e1_premium_richness") {
        cardData = { emRichness: checkMap["SN_EM_RICHNESS"] || {}, tailP90: checkMap["SN_TAIL_P90_RICHNESS"] || {}, ticker: lastPayload.ticker, expectedMove: lastPayload.expectedMove || {} };
      } else if (insightType === "e1_liquidity_check") {
        cardData = { check: checkMap["SN_LIQUIDITY"] || {}, ticker: lastPayload.ticker };
      } else if (insightType === "e1_macro_overlay") {
        cardData = { gamma: checkMap["MACRO_GAMMA"] || {}, indexSensitivity: checkMap["SN_INDEX_SENSITIVITY"] || {}, rvAccel: checkMap["MACRO_RV_ACCEL"] || {}, gammaFlip: checkMap["MACRO_GAMMA_FLIP"] || {}, forcedFlows: checkMap["MACRO_FORCED_FLOWS"] || {}, ticker: lastPayload.ticker, regime: lastPayload.regime || {} };
      }
      e1FetchInsight(insightType, cardData, title + ": " + (lastPayload.ticker || ""), ev.clientX, ev.clientY);
    });
  }

  // Clear cache on new calculations
  var origRender = window._e1OrigRender;
  if (!origRender) {
    // Monkey-patch to clear insight cache when new data arrives
    window._e1InsightCacheClear = function () { e1Insight.clearCache(); };
  }

  // Optional auto-run for calendar deep-links: /breach?ticker=...&mc=1&autorun=1
  if (qsAutorun === "1" || qsAutorun === "true" || qsAutorun === "yes" || qsAutorun === "on") {
    // Kick once on page load. Respect "busy" guardrails.
    if (!isBusy) runCalculation();
  }
});


