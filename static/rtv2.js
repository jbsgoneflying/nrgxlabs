/* ── RTv2.0 Dashboard JS — Unified Trading Desk OS ──────────── */

const API = '/api/rtv2';
const CC  = '/api/command-center';
let _data = null;   // RTv2 init payload
let _cc   = {};     // Command Center data (desk brief, sequencer, alerts, ideas)

/* ── Helpers ────────────────────────────────────────────────── */
function $(id) { return document.getElementById(id); }
function h(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function fmt(n, d) { return (typeof n === 'number') ? n.toFixed(d === undefined ? 2 : d) : '—'; }
function pct(n) { return (typeof n === 'number') ? (n * 100).toFixed(1) + '%' : '—'; }

function stateColor(state) {
  return ({ ON_TRACK:'green', NEAR_TARGET:'blue', RISK_INCREASING:'amber', THESIS_WEAKENING:'orange', INVALIDATED:'red' })[(state||'').toUpperCase()] || 'gray';
}
function actionClass(action) {
  return ({ HOLD:'green', TRIM:'amber', TIGHTEN:'amber', EXIT:'red', REVIEW:'blue' })[(action||'').toUpperCase()] || 'gray';
}
function regimeColor(r) {
  return ({ 'Risk-On':'green', 'Transitional':'amber', 'Risk-Off':'red', 'Stressed':'red' })[r] || 'gray';
}
function bucketColor(b) {
  return ({ income_core:'green', directional:'blue', opportunistic:'amber' })[b] || 'gray';
}

function showOverlay(msg) {
  const ov = $('ravenOverlay');
  if (ov) { ov.style.display = 'flex'; $('ravenStatus').textContent = msg || 'Loading…'; }
}
function hideOverlay() {
  const ov = $('ravenOverlay');
  if (ov) ov.style.display = 'none';
}
function setProgress(pct) {
  const f = $('ravenProgressFill');
  if (f) f.style.width = pct + '%';
}
function setStatus(msg) {
  const el = $('ravenStatus');
  if (el) el.textContent = msg;
}

function daysSince(isoStr) {
  if (!isoStr) return 999;
  const d = new Date(isoStr);
  const now = new Date();
  return Math.floor((now - d) / (1000 * 60 * 60 * 24));
}

function ageClass(days) {
  if (days < 1) return 'rv2Age--fresh';
  if (days <= 3) return 'rv2Age--warm';
  return 'rv2Age--stale';
}

const ENGINE_NAMES = {
  E1: 'Earnings Breach', E2: 'SPX IC', E3: 'Red Dog', E4: 'Ichimoku',
  E5: 'Lead-Lag', E7: 'Pairs', E8: 'Post-Event', E9: 'Credit Stress',
};


/* ══════════════════════════════════════════════════════════════ */
/*  PASSIVE LOAD — reads cached state, fast                      */
/* ══════════════════════════════════════════════════════════════ */
async function rv2Load() {
  showOverlay('Loading RTv2.0 dashboard…');
  setProgress(10);
  try {
    const resp = await fetch(API + '/init');
    if (!resp.ok) throw new Error('Init failed: ' + resp.status);
    setProgress(60);
    _data = await resp.json();
    setProgress(80);
    renderAll();
    setProgress(100);
    setTimeout(hideOverlay, 300);
  } catch (e) {
    console.error('RTv2 init error:', e);
    setStatus('Failed to load: ' + e.message);
    setProgress(100);
    setTimeout(hideOverlay, 2000);
  }
}


/* ══════════════════════════════════════════════════════════════ */
/*  FULL REFRESH — synchronous engine scans + RTv2 pipeline      */
/* ══════════════════════════════════════════════════════════════ */
async function rv2Refresh() {
  showOverlay('Clearing caches and scanning engines…');
  setProgress(3);
  const btn = $('rv2Refresh');
  if (btn) { btn.disabled = true; btn.textContent = 'Scanning engines…'; }

  const refreshStatus = {};

  try {
    // ── Step 1: Synchronous engine refresh (blocks until E3/E4/E5 finish) ──
    setStatus('Step 1/5 — Running E5 (regime), E3 (Red Dog), E4 (Ichimoku)… this takes 30-90s');
    setProgress(5);
    let engineData = null;
    try {
      const engineResp = await fetch('/api/desk/refresh-engines', { method: 'POST' });
      if (!engineResp.ok) {
        const err = await engineResp.json().catch(() => ({}));
        throw new Error(err.detail || 'Engine refresh failed: ' + engineResp.status);
      }
      engineData = await engineResp.json();
      refreshStatus.engines = engineData.status || {};
      refreshStatus.ideasFound = engineData.ideas_count || 0;
    } catch (e) {
      console.error('Engine refresh error:', e);
      refreshStatus.engines = { error: e.message };
    }
    setProgress(50);

    // ── Step 2: Ingest ideas into RTv2 scoring pipeline ──
    setStatus('Step 2/5 — Scoring & ingesting signals into RTv2 pipeline…');
    const ideas = (engineData && engineData.ideas) || [];
    if (ideas.length > 0) {
      const engineOutputs = { auto: true };
      for (const idea of ideas) {
        const key = idea.engine || '';
        if (key) {
          if (!engineOutputs[key]) engineOutputs[key] = [];
          engineOutputs[key].push(idea);
        }
      }
      try {
        const ingestResp = await fetch(API + '/ingest', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(engineOutputs),
        });
        if (ingestResp.ok) {
          const ingestData = await ingestResp.json();
          refreshStatus.ingest = (ingestData.signals_extracted || 0) + ' signals, ' + (ingestData.trades_created || 0) + ' trades';
        }
      } catch (e) {
        refreshStatus.ingest = 'error: ' + e.message;
      }
    } else {
      refreshStatus.ingest = 'no ideas found';
    }
    setProgress(65);

    // ── Step 3: Fetch CC context (desk brief, sequencer, alerts) in parallel ──
    setStatus('Step 3/5 — Loading desk brief, sequencer, alerts…');
    const [briefResp, seqResp, alertResp] = await Promise.allSettled([
      fetch(CC + '/desk-brief').then(r => r.ok ? r.json() : null),
      fetch(CC + '/sequencer').then(r => r.ok ? r.json() : null),
      fetch(CC + '/alerts').then(r => r.ok ? r.json() : null),
    ]);
    _cc.deskBrief = briefResp.status === 'fulfilled' ? briefResp.value : null;
    _cc.sequencer = seqResp.status === 'fulfilled' ? seqResp.value : null;
    _cc.alerts    = alertResp.status === 'fulfilled' ? alertResp.value : null;
    setProgress(80);

    // ── Step 4: Load full RTv2 init payload ──
    setStatus('Step 4/5 — Building portfolio state…');
    try {
      const rv2Resp = await fetch(API + '/init');
      if (rv2Resp.ok) _data = await rv2Resp.json();
    } catch (e) {
      console.error('RTv2 init after refresh:', e);
    }
    setProgress(90);

    // ── Step 5: Render ──
    setStatus('Step 5/5 — Rendering dashboard…');
    renderAll();
    renderRefreshSummary(refreshStatus);
    setProgress(100);
    setTimeout(hideOverlay, 400);

  } catch (e) {
    console.error('RTv2 refresh error:', e);
    setStatus('Refresh failed: ' + e.message);
    setProgress(100);
    setTimeout(hideOverlay, 3000);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Refresh Dashboard'; }
  }
}


/* ══════════════════════════════════════════════════════════════ */
/*  RENDER ALL                                                   */
/* ══════════════════════════════════════════════════════════════ */
function renderAll() {
  if (!_data) return;
  renderContextCards();
  renderBanners();
  renderDeskBrief();
  renderAlerts();
  renderAllocation();
  renderRisk();
  renderPositions();
  renderQueue();
  renderSequencer();
  renderPerformance();
}


/* ── Context cards: Regime, Flow, Vol, Engine Gates ─────────── */
function renderContextCards() {
  const d = _data;

  // Regime
  const regime = d.regime || 'Transitional';
  $('rv2RegimeVal').textContent = regime;
  const rc = d.regime_card || {};
  const driversText = Array.isArray(rc.drivers) ? rc.drivers.join(' · ') : '';
  $('rv2RegimeSub').textContent = driversText || 'Click Refresh Dashboard to load regime data';

  // Update regime card border color
  const regCard = $('rv2RegimeCard');
  regCard.className = 'rv2Card rv2Card--' + regimeColor(regime);

  // Flow
  const fc = d.flow_card || {};
  const ccFp = (_cc.flowPressure || {});
  const flowScore = fc.composite_score != null ? fmt(fc.composite_score, 0) :
                    ccFp.composite_score != null ? fmt(ccFp.composite_score, 0) :
                    fc.score != null ? fmt(fc.score, 0) : '—';
  $('rv2FlowVal').textContent = flowScore;
  const flowLabel = fc.label || fc.flow_label || ccFp.label || '';
  $('rv2FlowSub').textContent = flowLabel;

  // Vol
  const vc = d.vol_card || {};
  const termStr = vc.term_structure || '';
  const skew = vc.skew || '';
  const level = vc.level;
  $('rv2VolVal').textContent = termStr || '—';
  const volParts = [];
  if (skew) volParts.push('Skew: ' + skew);
  if (level != null && level !== '') volParts.push('Level: ' + fmt(Number(level), 1));
  $('rv2VolSub').textContent = volParts.join(' · ');

  // Engine Gates
  const gates = d.engine_gates || {};
  const gateEl = $('rv2GateContent');
  const GATE_LABELS = {
    earnings: 'E1 Earnings', red_dog: 'E3 Red Dog', ichimoku: 'E4 Ichimoku',
    index_income: 'E2 SPX IC', post_event_ext: 'E8 Post-Event',
  };
  if (Object.keys(gates).length) {
    let html = '';
    for (const [key, val] of Object.entries(gates)) {
      const status = typeof val === 'string' ? val : (val && val.status) || '—';
      const cls = status.toLowerCase();
      const label = GATE_LABELS[key] || key;
      html += `<div style="margin-bottom:3px"><span class="rv2Gate rv2Gate--${h(cls)}">${h(status)}</span> <span style="color:var(--muted)">${h(label)}</span></div>`;
    }
    gateEl.innerHTML = html;
  } else {
    gateEl.innerHTML = '<span style="color:var(--muted)">No gate data</span>';
  }
}


/* ── Banners (E9 Credit Stress, Caution) ───────────────────── */
function renderBanners() {
  const d = _data;
  const r = d.risk || {};

  // E9 credit stress banner
  const e9Banner = $('rv2E9Banner');
  if (r.credit_stress_warning) {
    e9Banner.style.display = 'block';
    e9Banner.innerHTML = `<strong>E9 Credit Stress Elevated</strong> — Composite: ${fmt(r.e9_level, 0)} · Regime: ${h(d.regime || '—')}. Reduce new entries if confirmed by regime + vol.`;
  } else {
    e9Banner.style.display = 'none';
  }

  // Caution banners
  const cautionBanner = $('rv2CautionBanner');
  const cautions = [];
  if (d.regime === 'Transitional') cautions.push('Regime is Transitional — signal conflict possible');
  if (r.drawdown_warning) cautions.push('Weekly drawdown threshold exceeded');
  if (r.directional_tilt && r.directional_tilt !== 'neutral') cautions.push('Portfolio tilt: ' + r.directional_tilt);

  if (cautions.length) {
    cautionBanner.style.display = 'block';
    cautionBanner.innerHTML = cautions.map(c => '<div>' + h(c) + '</div>').join('');
  } else {
    cautionBanner.style.display = 'none';
  }
}


/* ── Desk Brief ────────────────────────────────────────────── */
function renderDeskBrief() {
  const container = $('rv2DeskBrief');
  const briefData = _cc.deskBrief;

  if (!briefData || !briefData.brief) {
    container.innerHTML = '<div class="rv2Empty">Click <strong>Refresh Dashboard</strong> to generate the desk brief.</div>';
    return;
  }

  const b = briefData.brief;
  let html = '';
  if (b.market_state) html += `<p><strong>Market State:</strong> ${h(b.market_state)}</p>`;
  if (b.trade_implications) html += `<p><strong>Trade Implications:</strong> ${h(b.trade_implications)}</p>`;
  if (b.risk_factors) html += `<p><strong>Risk Factors:</strong> ${h(b.risk_factors)}</p>`;
  if (b.key_levels) html += `<p><strong>Key Levels:</strong> ${h(b.key_levels)}</p>`;

  if (briefData.enabled === false) {
    html += '<div style="font-size:10px;color:var(--muted);margin-top:4px">Deterministic synthesis (LLM disabled)</div>';
  }

  container.innerHTML = html || '<div class="rv2Empty">Brief generated but empty.</div>';
}


/* ── State Flip Alerts ──────────────────────────────────────── */
function renderAlerts() {
  const container = $('rv2AlertsContent');
  const alertData = _cc.alerts;

  if (!alertData || !alertData.alerts || !alertData.alerts.length) {
    container.innerHTML = '<div class="rv2Empty">No state flip alerts this week.</div>';
    return;
  }

  let html = '';
  for (const a of alertData.alerts.slice(0, 8)) {
    const typeColor = (a.event_type || '').includes('REGIME') ? 'red' :
                      (a.event_type || '').includes('FLOW') ? 'amber' :
                      (a.event_type || '').includes('VOL') ? 'purple' : 'gray';
    html += `<div style="margin-bottom:8px;padding:6px 10px;background:var(--hover);border-radius:8px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span class="rv2Pill rv2Pill--${typeColor}">${h(a.type || a.event_type || '')}</span>
        <span style="font-size:10px;color:var(--muted)">${h(a.date || '')}</span>
      </div>
      ${a.from_state ? `<div style="font-size:11px;margin-top:3px">${h(a.from_state)} → ${h(a.to_state)}</div>` : ''}
      ${a.summary ? `<div style="font-size:10px;color:var(--muted);margin-top:2px">${h(a.summary)}</div>` : ''}
    </div>`;
  }

  container.innerHTML = html;
}


/* ── Allocation bars ────────────────────────────────────────── */
function renderAllocation() {
  const d = _data;
  const alloc = (d.allocation || {}).buckets || {};
  const container = $('rv2AllocBars');
  let html = '';
  for (const [name, b] of Object.entries(alloc)) {
    if (name === 'cash_reserve') continue;
    const used = parseFloat(b.used_ru || 0);
    const max = parseFloat(b.max_ru || 1);
    const pctW = Math.min(100, (used / max) * 100);
    const col = bucketColor(name);
    const label = name.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    html += `<div class="rv2Bar">
      <span class="label">${h(label)}</span>
      <div class="track"><div class="fill fill--${col}" style="width:${pctW}%"></div></div>
      <span class="val">${fmt(used,1)} / ${fmt(max,1)} RU</span>
    </div>`;
  }
  container.innerHTML = html || '<div class="rv2Empty">No allocation data.</div>';

  const a = d.allocation || {};
  $('rv2AllocSummary').textContent = `Regime: ${a.regime || '—'} · Total RU: ${fmt(a.total_used_ru,1)} / ${fmt(a.portfolio_ru_cap,0)}`;
}


/* ── Risk dashboard ─────────────────────────────────────────── */
function renderRisk() {
  const d = _data;
  const r = d.risk || {};
  const container = $('rv2RiskContent');
  let html = '';

  // Portfolio RU bar
  const ruPct = r.ru_utilisation_pct || 0;
  html += `<div class="rv2Bar"><span class="label">Portfolio RU</span>
    <div class="track"><div class="fill fill--${ruPct > 80 ? 'red' : ruPct > 50 ? 'amber' : 'green'}" style="width:${ruPct}%"></div></div>
    <span class="val">${fmt(r.total_ru,1)} / ${fmt(r.portfolio_ru_cap,0)}</span></div>`;

  // Pills row
  html += `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">`;
  html += `<span class="rv2Pill rv2Pill--${regimeColor(r.regime)}">Regime: ${h(r.regime || '—')}</span>`;
  html += `<span class="rv2Pill rv2Pill--${r.directional_tilt === 'neutral' ? 'green' : 'amber'}">Tilt: ${h(r.directional_tilt || 'neutral')}</span>`;
  if (r.credit_stress_warning) html += `<span class="rv2Pill rv2Pill--red">E9: ${fmt(r.e9_level,0)}</span>`;
  if (r.drawdown_warning) html += `<span class="rv2Pill rv2Pill--red">DD: ${pct(r.weekly_drawdown_pct)}</span>`;
  html += `</div>`;

  // Sector warnings
  if (r.sector_warnings && r.sector_warnings.length) {
    html += `<div style="margin-top:10px"><strong style="font-size:10px;color:var(--muted)">SECTOR CONCENTRATION</strong></div>`;
    r.sector_warnings.forEach(w => { html += `<div style="font-size:11px;color:var(--red);margin-top:2px">${h(w)}</div>`; });
  }

  // Sector exposure breakdown
  if (r.sector_exposure && Object.keys(r.sector_exposure).length) {
    html += `<div style="margin-top:10px"><strong style="font-size:10px;color:var(--muted)">SECTOR EXPOSURE</strong></div>`;
    for (const [sector, ru] of Object.entries(r.sector_exposure)) {
      const sectorPct = Math.min(100, (ru / 4) * 100);
      html += `<div class="rv2Bar" style="margin-bottom:3px">
        <span class="label" style="min-width:80px">${h(sector)}</span>
        <div class="track"><div class="fill fill--blue" style="width:${sectorPct}%"></div></div>
        <span class="val">${fmt(ru,1)} RU</span>
      </div>`;
    }
  }

  // Correlated positions
  if (r.correlation_warnings && r.correlation_warnings.length) {
    html += `<div style="margin-top:10px"><strong style="font-size:10px;color:var(--muted)">CORRELATION WARNINGS</strong></div>`;
    r.correlation_warnings.forEach(w => { html += `<div style="font-size:11px;color:var(--amber);margin-top:2px">${h(w)}</div>`; });
  }

  container.innerHTML = html;
}


/* ── Positions table ────────────────────────────────────────── */
function renderPositions() {
  const d = _data;
  const positions = d.positions || [];
  const summary = d.positions_summary || {};

  const hc = $('rv2HealthPills');
  let pills = '';
  if (summary.ON_TRACK) pills += `<span class="rv2Pill rv2Pill--green">${summary.ON_TRACK} On Track</span> `;
  if (summary.NEAR_TARGET) pills += `<span class="rv2Pill rv2Pill--blue">${summary.NEAR_TARGET} Near Target</span> `;
  if (summary.RISK_INCREASING) pills += `<span class="rv2Pill rv2Pill--amber">${summary.RISK_INCREASING} Risk Increasing</span> `;
  if (summary.THESIS_WEAKENING) pills += `<span class="rv2Pill rv2Pill--orange">${summary.THESIS_WEAKENING} Weakening</span> `;
  if (summary.INVALIDATED) pills += `<span class="rv2Pill rv2Pill--red">${summary.INVALIDATED} Invalidated</span> `;
  hc.innerHTML = pills || '<span class="rv2Pill rv2Pill--gray">No positions</span>';

  const container = $('rv2PositionsTable');
  if (!positions.length) {
    container.innerHTML = '<div class="rv2Empty">No active positions. Use <strong>Refresh Dashboard</strong> to scan engines, or add a trade via <strong>Manual Trade Entry</strong> below.</div>';
    return;
  }

  let html = `<table class="rv2Table"><thead><tr>
    <th>Ticker</th><th>Engine</th><th>Type</th><th>Dir</th>
    <th>Entry</th><th>Stop</th><th>Target</th><th>RU</th>
    <th>P&L</th><th>Days</th><th>State</th><th>Action</th><th>Reason</th>
    <th></th>
  </tr></thead><tbody>`;

  for (const p of positions) {
    const sc = stateColor(p.position_state);
    const ac = actionClass(p.suggested_action);
    const pnlPct = p.current_pnl_pct || 0;
    html += `<tr>
      <td><strong>${h(p.ticker || '')}</strong></td>
      <td><span class="rv2Pill rv2Pill--gray">${h(p.engine_source || '')}</span></td>
      <td style="font-size:11px">${h((p.trade_type || '').replace(/_/g, ' '))}</td>
      <td>${h(p.direction || '')}</td>
      <td class="mono">${fmt(p.entry_price)}</td>
      <td class="mono">${fmt(p.thesis_stop)}</td>
      <td class="mono">${fmt(p.thesis_target)}</td>
      <td class="mono">${fmt(p.derived_ru, 1)}</td>
      <td class="mono" style="color:${pnlPct >= 0 ? 'var(--green)' : 'var(--red)'}">${pct(pnlPct)}</td>
      <td>${p.days_in_trade || 0}d</td>
      <td><span class="rv2Dot rv2Dot--${sc}"></span><span class="rv2Pill rv2Pill--${sc}">${h(p.position_state || '')}</span></td>
      <td><span class="rv2Pill rv2Pill--${ac}">${h(p.suggested_action || '')}</span></td>
      <td style="font-size:10px;color:var(--muted);max-width:200px">${h(p.state_reason || '')}</td>
      <td>
        ${p.suggested_action === 'EXIT' ? `<button class="rv2Btn rv2Btn--red" onclick="rv2CloseTrade('${p.trade_id}')">Close</button>` : ''}
        ${p.suggested_action === 'TIGHTEN' ? `<button class="rv2Btn rv2Btn--amber" onclick="rv2TightenTrade('${p.trade_id}')">Tighten</button>` : ''}
      </td>
    </tr>`;
  }

  html += '</tbody></table>';
  container.innerHTML = html;
}


/* ── Unified Idea Queue (enriched) ──────────────────────────── */
function renderQueue() {
  const d = _data;
  const queue = d.queue || [];
  const container = $('rv2QueueTable');
  if (!queue.length) {
    container.innerHTML = '<div class="rv2Empty">No ideas in queue. Click <strong>Refresh Dashboard</strong> to scan Engine 3 (Red Dog), Engine 4 (Ichimoku), and others.</div>';
    return;
  }

  let html = `<table class="rv2Table"><thead><tr>
    <th>Ticker</th><th>Engine</th><th>Bucket</th><th>UPS</th><th>Raw Score</th>
    <th>State</th><th>RU</th><th>Dir</th><th>Age</th><th></th>
  </tr></thead><tbody>`;

  for (const q of queue) {
    const age = daysSince(q.created_at);
    const ageLabel = age < 1 ? '<1d' : age + 'd';
    const ageCls = ageClass(age);
    const engineLabel = ENGINE_NAMES[q.engine_source] || q.engine_source || '';

    html += `<tr>
      <td><strong>${h(q.ticker || '')}</strong></td>
      <td><span class="rv2Pill rv2Pill--gray">${h(q.engine_source || '')}</span> <span style="font-size:10px;color:var(--muted)">${h(engineLabel)}</span></td>
      <td><span class="rv2Pill rv2Pill--${bucketColor(q.bucket)}">${h((q.bucket || '').replace(/_/g, ' '))}</span></td>
      <td class="mono" style="font-weight:700;font-size:13px">${fmt(q.ups_score, 1)}</td>
      <td class="mono" style="font-size:11px;color:var(--muted)">${fmt(q.raw_engine_score, 1)}</td>
      <td><span class="rv2Pill rv2Pill--gray">${h(q.lifecycle_state || '')}</span></td>
      <td class="mono">${fmt(q.derived_ru, 1)}</td>
      <td>${h(q.direction || '')}</td>
      <td class="${ageCls}" style="font-size:11px;font-weight:600">${ageLabel}</td>
      <td>
        ${q.lifecycle_state === 'QUEUED' ? `<button class="rv2Btn rv2Btn--blue" onclick="rv2StageTrade('${q.trade_id}')">Stage</button>` : ''}
        ${q.lifecycle_state === 'STAGED' ? `<button class="rv2Btn rv2Btn--green" onclick="rv2ActivatePrompt('${q.trade_id}')">Activate</button>` : ''}
      </td>
    </tr>`;
  }

  html += '</tbody></table>';
  container.innerHTML = html;
}


/* ── Weekly Sequencer ───────────────────────────────────────── */
function renderSequencer() {
  const container = $('rv2SequencerContent');
  const seq = _cc.sequencer;
  if (!seq || !seq.sequence) {
    container.innerHTML = '<div class="rv2Empty">Click <strong>Refresh Dashboard</strong> to load sequencer data.</div>';
    return;
  }

  const s = seq.sequence;
  const timeline = s.timeline || {};
  const pattern = s.matched_pattern || {};
  let html = '';

  // Pattern match
  if (pattern.label) {
    html += `<div style="margin-bottom:10px;padding:8px 12px;background:var(--hover);border-radius:8px">
      <strong style="font-size:12px">${h(pattern.label)}</strong>
      <span class="rv2Pill rv2Pill--blue" style="margin-left:6px">${pattern.confidence || 0}% confidence</span>
      ${pattern.primary_risk ? `<div style="font-size:10px;color:var(--muted);margin-top:2px">Risk: ${h(pattern.primary_risk)}</div>` : ''}
      ${pattern.favored_play_types && pattern.favored_play_types.length ? `<div style="font-size:10px;color:var(--muted)">Favored: ${pattern.favored_play_types.map(h).join(', ')}</div>` : ''}
    </div>`;
  }

  // Timeline
  const days = seq.tradingDays || [];
  for (const day of days) {
    const evts = timeline[day] || [];
    const dayLabel = day;
    html += `<div class="rv2SeqDay"><div class="dayLabel">${h(dayLabel)}</div>`;
    if (evts.length) {
      for (const ev of evts) {
        html += `<div class="rv2SeqEvent">
          <span class="rv2Pill rv2Pill--gray">${h(ev.label || '')}</span>
          ${ev.from_state ? `<span class="fromTo">${h(ev.from_state)} → ${h(ev.to_state)}</span>` : ''}
        </div>`;
      }
    } else {
      html += '<div style="font-size:10px;color:var(--muted);padding:2px 0">No events</div>';
    }
    html += '</div>';
  }

  container.innerHTML = html || '<div class="rv2Empty">No sequencer events this week.</div>';
}


/* ── Performance scorecard ──────────────────────────────────── */
function renderPerformance() {
  const d = _data;
  const perf = d.performance || {};
  const container = $('rv2PerfContent');
  const engines = perf.engines || {};
  const buckets = perf.buckets || {};

  if (!Object.keys(engines).length && !Object.keys(buckets).length) {
    container.innerHTML = '<div class="rv2Empty">No performance data yet. Close trades to build the scorecard. Metrics accumulate after trades are completed.</div>';
    return;
  }

  let html = '<div style="margin-bottom:10px"><strong style="font-size:10px;color:var(--muted)">ENGINE METRICS (90-day rolling)</strong></div>';
  html += `<table class="rv2Table"><thead><tr><th>Engine</th><th>Trades</th><th>Win Rate</th><th>Avg Return/RU</th><th>Avg Days</th><th>Streak</th></tr></thead><tbody>`;
  for (const [eid, m] of Object.entries(engines)) {
    const wr = m.win_rate != null ? pct(m.win_rate) : '—';
    const streak = m.consecutive_wins > 0 ? `W${m.consecutive_wins}` : m.consecutive_losses > 0 ? `L${m.consecutive_losses}` : '—';
    const streakCol = m.consecutive_wins > 0 ? 'green' : m.consecutive_losses > 0 ? 'red' : 'gray';
    const engLabel = ENGINE_NAMES[eid] || eid;
    html += `<tr>
      <td><strong>${h(eid)}</strong> <span style="font-size:10px;color:var(--muted)">${h(engLabel)}</span></td>
      <td>${m.trade_count || 0}</td>
      <td>${wr}</td>
      <td class="mono">${fmt(m.avg_return_per_ru)}</td>
      <td>${fmt(m.avg_days_held, 1)}</td>
      <td><span class="rv2Pill rv2Pill--${streakCol}">${streak}</span></td>
    </tr>`;
  }
  html += '</tbody></table>';

  if (Object.keys(buckets).length) {
    html += '<div style="margin:14px 0 8px"><strong style="font-size:10px;color:var(--muted)">BUCKET METRICS (90-day rolling)</strong></div>';
    html += `<table class="rv2Table"><thead><tr><th>Bucket</th><th>Trades</th><th>Win Rate</th><th>Total P&L</th><th>Drawdown</th></tr></thead><tbody>`;
    for (const [bid, m] of Object.entries(buckets)) {
      const label = bid.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
      html += `<tr>
        <td><strong>${h(label)}</strong></td>
        <td>${m.trade_count || 0}</td>
        <td>${pct(m.win_rate)}</td>
        <td class="mono" style="color:${(m.total_pnl||0)>=0?'var(--green)':'var(--red)'}">$${fmt(m.total_pnl)}</td>
        <td class="mono">$${fmt(m.worst_drawdown)}</td>
      </tr>`;
    }
    html += '</tbody></table>';
  }

  container.innerHTML = html;
}


/* ── Refresh summary bar ───────────────────────────────────── */
function renderRefreshSummary(status) {
  let el = $('rv2RefreshStatus');
  if (!el) {
    const bar = document.querySelector('.controlBar');
    if (bar) {
      el = document.createElement('div');
      el.id = 'rv2RefreshStatus';
      el.style.cssText = 'font-size:11px;color:var(--muted);padding:6px 16px;border-top:1px solid var(--border)';
      bar.parentNode.insertBefore(el, bar.nextSibling);
    }
  }
  if (!el) return;
  const parts = [];
  if (status.ccInit) parts.push('Engines: ' + status.ccInit);
  if (status.tradableIdeas) parts.push('Ideas: ' + status.tradableIdeas);
  if (status.ingest) parts.push('Ingest: ' + status.ingest);
  el.innerHTML = parts.join(' · ') + ' · <span style="color:var(--green)">Refreshed ' + new Date().toLocaleTimeString() + '</span>';
}


/* ══════════════════════════════════════════════════════════════ */
/*  ACTIONS                                                      */
/* ══════════════════════════════════════════════════════════════ */
async function rv2SubmitManual() {
  const body = {
    ticker:  $('mTicker').value.toUpperCase().trim(),
    direction: $('mDirection').value,
    entry_price: parseFloat($('mEntryPrice').value),
    units: parseInt($('mUnits').value, 10),
    trade_type: $('mTradeType').value,
    thesis_stop: parseFloat($('mStop').value),
    thesis_target: parseFloat($('mTarget').value),
    thesis_max_days: parseInt($('mMaxDays').value, 10) || 10,
    invalidation_conditions: $('mInvalidation').value.split('\n').map(s => s.trim()).filter(Boolean),
    bucket: $('mBucket').value,
    sector: $('mSector').value.trim(),
    notes: $('mNotes').value.trim(),
  };

  if (!body.ticker || !body.entry_price || !body.units || !body.thesis_stop || !body.thesis_target) {
    alert('Please fill in all required fields (ticker, entry price, units, stop, target).');
    return;
  }

  try {
    const resp = await fetch(API + '/positions/manual', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
    if (!resp.ok) { const e = await resp.json().catch(() => ({})); throw new Error(e.detail || resp.statusText); }
    const result = await resp.json();
    alert(`Trade created: ${result.trade_id}\nDerived RU: ${fmt(result.derived_ru || (result.ru_info||{}).capped_ru, 2)}`);
    rv2Load();
  } catch (e) {
    alert('Error: ' + e.message);
  }
}

async function rv2StageTrade(tradeId) {
  try {
    const resp = await fetch(API + `/trades/${tradeId}/stage`, { method: 'POST' });
    if (!resp.ok) { const e = await resp.json().catch(() => ({})); throw new Error(e.detail || resp.statusText); }
    rv2Load();
  } catch (e) { alert('Stage error: ' + e.message); }
}

async function rv2ActivatePrompt(tradeId) {
  const price = prompt('Enter entry price:');
  if (!price) return;
  const units = prompt('Enter units (shares/contracts):');
  if (!units) return;

  try {
    const body = { entry_price: parseFloat(price), units: parseInt(units, 10), direction: 'long' };
    const resp = await fetch(API + `/trades/${tradeId}/activate`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
    if (!resp.ok) { const e = await resp.json().catch(() => ({})); throw new Error(e.detail || resp.statusText); }
    rv2Load();
  } catch (e) { alert('Activate error: ' + e.message); }
}

async function rv2CloseTrade(tradeId) {
  const pnl = prompt('Enter P&L in dollars (negative for loss):');
  if (pnl === null) return;
  const reason = prompt('Exit reason (target/stop/desk_discretion):', 'desk_discretion');
  if (!reason) return;

  try {
    const body = { pnl_dollars: parseFloat(pnl) || 0, exit_reason: reason };
    const resp = await fetch(API + `/trades/${tradeId}/close`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
    if (!resp.ok) { const e = await resp.json().catch(() => ({})); throw new Error(e.detail || resp.statusText); }
    rv2Load();
  } catch (e) { alert('Close error: ' + e.message); }
}

async function rv2TightenTrade(tradeId) {
  const newStop = prompt('Enter new (tighter) stop price:');
  if (!newStop) return;

  try {
    const body = { thesis_stop: parseFloat(newStop) };
    const resp = await fetch(API + `/positions/${tradeId}/thesis`, { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
    if (!resp.ok) { const e = await resp.json().catch(() => ({})); throw new Error(e.detail || resp.statusText); }
    rv2Load();
  } catch (e) { alert('Tighten error: ' + e.message); }
}


/* ── Boot ───────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', rv2Load);
