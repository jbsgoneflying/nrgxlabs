/* Engine 15 — Earnings IC Scenario UI
 *
 * Step 1: ticker scan -> /api/earnings-ic/scan (Engine 1 payload + coverage)
 * Step 2: user enters wings + planned exit -> /api/earnings-ic/scenario
 * Step 3: render results (entry, planned-exit caveat, outcome distribution,
 *         conditioning modifiers, MTM chart, exit rules, matched events)
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);

  // ------------------------------------------------------------------
  // State
  // ------------------------------------------------------------------
  const state = {
    scan: null,         // raw /scan payload
    scenario: null,     // raw /scenario payload
    mtmChart: null,
  };

  // ------------------------------------------------------------------
  // Utilities
  // ------------------------------------------------------------------
  function fmtPct(v, digits) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    return `${Number(v).toFixed(digits != null ? digits : 1)}%`;
  }
  function fmtNum(v, digits) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    return Number(v).toFixed(digits != null ? digits : 2);
  }
  function fmtInt(v) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    return String(Math.round(Number(v)));
  }
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function card(label, value, caption) {
    const c = el('div', 'e15Card');
    c.appendChild(el('div', 'e15CardLabel', label));
    c.appendChild(el('div', 'e15CardValue', value));
    if (caption) c.appendChild(el('div', 'e15CardCaption', caption));
    return c;
  }
  function banner(msg, type) {
    const b = $('banner');
    b.className = 'e15Banner' + (type ? ' ' + type : '');
    b.textContent = msg;
    b.style.display = msg ? '' : 'none';
  }
  function setStatus(elId, text) { $(elId).textContent = text; }
  function show(id) { $(id).classList.remove('hidden'); $(id).style.display = ''; }
  function hide(id) { $(id).classList.add('hidden'); }

  async function postJson(url, body, extraHeaders) {
    const headers = { 'Content-Type': 'application/json' };
    if (extraHeaders) Object.assign(headers, extraHeaders);
    const r = await fetch(url, {
      method: 'POST',
      headers,
      body: JSON.stringify(body || {}),
    });
    const txt = await r.text();
    let data = null;
    try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = { _raw: txt }; }
    if (!r.ok) {
      const msg = (data && data.detail) ? data.detail : (data && data._raw) || r.statusText;
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    return data;
  }
  async function getJson(url) {
    const r = await fetch(url);
    const txt = await r.text();
    let data = null;
    try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = { _raw: txt }; }
    if (!r.ok) {
      const msg = (data && data.detail) ? data.detail : (data && data._raw) || r.statusText;
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    return data;
  }

  // Add N business days to an ISO YYYY-MM-DD date string.
  function shiftBizDays(iso, days) {
    if (!iso) return iso;
    const d = new Date(iso + 'T00:00:00');
    let rem = Math.abs(days);
    const step = days >= 0 ? 1 : -1;
    while (d.getDay() === 0 || d.getDay() === 6) d.setDate(d.getDate() + step);
    while (rem > 0) {
      d.setDate(d.getDate() + step);
      if (d.getDay() !== 0 && d.getDay() !== 6) rem--;
    }
    return d.toISOString().slice(0, 10);
  }

  // ------------------------------------------------------------------
  // Step 1: /api/earnings-ic/scan
  // ------------------------------------------------------------------
  async function doScan(evt) {
    if (evt) evt.preventDefault();
    const t = ($('ticker').value || '').trim().toUpperCase();
    if (!t) return;
    $('ticker').value = t;

    setStatus('scanStatus', 'Running Engine 1…');
    $('scanBtn').disabled = true;
    banner('');
    try {
      const n = parseInt($('historyN').value || '20', 10);
      const data = await postJson('/api/earnings-ic/scan', { ticker: t, n, years: 5 });
      state.scan = data;
      renderScanResult(data);
      setStatus('scanStatus', 'Done.');
    } catch (err) {
      banner('Scan failed: ' + err.message, 'error');
      setStatus('scanStatus', 'Error');
    } finally {
      $('scanBtn').disabled = false;
    }
  }

  function renderScanResult(data) {
    const s = (data && data.engine1Summary) || {};
    const e1 = (data && data.engine1) || {};
    // Back-compat: if the server didn't send engine1Summary (older builds),
    // fall back to reading the raw payload inline.
    const current = e1.current || {};
    const vrp = e1.vrpAnalysis || {};
    const dc = e1.deskConsensus || {};
    const next = e1.nextEvent || {};
    const em = e1.expectedMove || {};
    const st = e1.strikeTargets || {};

    const stockPrice = s.stockPrice != null ? s.stockPrice : current.stockPrice;
    const asOfDate = s.asOfDate || current.asOfDate;
    const vrpScore = s.vrpScore != null ? s.vrpScore : vrp.vrpScore;
    const ivElev = s.ivElevation != null ? s.ivElevation : vrp.ivElevation;
    const oratsEmPct = s.oratsEmPct != null ? s.oratsEmPct : current.impliedMovePct;
    const delayedEmPct = s.delayedEmPct != null ? s.delayedEmPct : current.delayedImpliedMovePct;
    const delayedUpdated = s.delayedUpdatedAt || current.delayedUpdatedAt;
    const straddlePct = s.straddleEmPct != null ? s.straddleEmPct : em.expectedMovePct;
    const straddleDollars = s.straddleEmDollars != null ? s.straddleEmDollars : em.expectedMoveDollars;
    const straddleExpiry = s.straddleExpiry || em.expiry;
    const straddleSource = (s.straddleSource || em.source || '').toLowerCase();
    const strike1x = (s.strikeTargets && s.strikeTargets.whitePct) != null ? s.strikeTargets.whitePct : st.whitePct;
    const strike15x = (s.strikeTargets && s.strikeTargets.bluePct) != null ? s.strikeTargets.bluePct : st.bluePct;
    const strike2x = (s.strikeTargets && s.strikeTargets.redPct) != null ? s.strikeTargets.redPct : st.redPct;
    const strikeEmSource = (s.strikeTargets && s.strikeTargets.emSource) || st.emSource;
    const nextDate = s.nextEventDate || next.earnDate || next.earnDateNext || next.date;
    const nextTiming = s.anncTod || next.timing || next.timingPlanned || next.anncTod;
    const pricingExpiry = s.nextEventPricingExpiry || next.pricingExpiry;
    const nextSource = s.nextEventSource || next.source;
    const deskVerdict = s.deskConsensus || dc.consensus || dc.verdict;
    const deskScore = s.deskConsensusScore != null ? s.deskConsensusScore : dc.score;
    const breach1x = s.emBreachRate1xPct;
    const breach15x = s.emBreachRate15xPct;
    const breach2x = s.emBreachRate2xPct;
    const breachN = s.emBreachN != null ? s.emBreachN : ((e1.summary || {}).events_used);
    const regimeLabel = s.regimeLabel || (e1.regime || {}).label;
    const regimeTail = s.regimeTailMultiplier != null ? s.regimeTailMultiplier : (e1.regime || {}).tailMultiplier;
    const eventRiskLabel = s.eventRiskLabel || (e1.eventRisk || {}).label;
    const evN = s.historyN != null ? s.historyN : (e1.events || []).length;

    const srcLabel = (s.emPctSource === 'live' || !s.emPctSource) ? 'Live' : 'Delayed';
    const nextSrcCaption = nextSource === 'friday_expiry_fallback'
      ? 'Friday-exp fallback — confirm date'
      : (nextSource === 'orats_snapshot' ? 'ORATS snapshot' : '');
    const regimeCaption = regimeLabel
      ? `Regime ${regimeLabel}${regimeTail != null ? ' (' + Number(regimeTail).toFixed(2) + 'x)' : ''}${eventRiskLabel ? ' · Risk ' + eventRiskLabel : ''}`
      : '';

    const cards = $('e1Cards');
    cards.innerHTML = '';

    // 1. Stock Price (EOD anchor + headline EM)
    const stockCaption = asOfDate
      ? `EOD ${String(asOfDate).slice(0,10)}${Number.isFinite(oratsEmPct) || Number.isFinite(delayedEmPct) ? ' · EM ' + fmtPct(oratsEmPct != null ? oratsEmPct : delayedEmPct, 2) + ' (' + srcLabel + ')' : ''}`
      : (Number.isFinite(oratsEmPct) || Number.isFinite(delayedEmPct)) ? 'EM ' + fmtPct(oratsEmPct != null ? oratsEmPct : delayedEmPct, 2) : '—';
    cards.appendChild(card('Stock Price', fmtNum(stockPrice, 2), stockCaption));

    // 2. VRP + IV elevation
    cards.appendChild(card('VRP Score',
      vrpScore != null ? Number(vrpScore).toFixed(2) : '—',
      ivElev != null ? `IV elev: ${fmtPct(ivElev, 1)}` : regimeCaption || ''));

    // 3. Next Event — date + pricing expiry
    const nextCaption = [nextTiming || '—',
      pricingExpiry ? `Exp: ${String(pricingExpiry).slice(0,10)}` : '',
      nextSrcCaption].filter(Boolean).join(' · ');
    cards.appendChild(card('Next Event', nextDate || '—', nextCaption));

    // 4. Straddle EM ($ and %, expiry, source)
    const straddleCaption = [
      Number.isFinite(straddleDollars) ? `$${Number(straddleDollars).toFixed(2)} pts` : '',
      straddleExpiry ? `Exp: ${String(straddleExpiry).slice(0,10)}` : '',
      straddleSource ? straddleSource.toUpperCase() : ''
    ].filter(Boolean).join(' · ') || '—';
    cards.appendChild(card('Straddle EM', fmtPct(straddlePct, 2), straddleCaption));

    // 5. ORATS EM (EOD + delayed block)
    const oratsCardVal = fmtPct(oratsEmPct, 2);
    const oratsCaption = `EOD${asOfDate ? ' ' + String(asOfDate).slice(0,10) : ''} · Delayed ${fmtPct(delayedEmPct, 2)}${delayedUpdated ? ' @ ' + String(delayedUpdated).slice(11,16) : ''}`;
    cards.appendChild(card('ORATS EM', oratsCardVal, oratsCaption));

    // 6. Strike Targets (1x / 1.5x / 2x)
    const strikeCardVal = Number.isFinite(strike1x) ? `${fmtPct(strike1x, 2)} / ${fmtPct(strike15x, 2)} / ${fmtPct(strike2x, 2)}` : '—';
    const strikeCaption = `1× / 1.5× / 2× EM wing distance${strikeEmSource ? ' · ' + strikeEmSource : ''}`;
    cards.appendChild(card('Strike Targets', strikeCardVal, strikeCaption));

    // 7. Desk Consensus
    cards.appendChild(card('Desk Consensus', deskVerdict || '—',
      deskScore != null ? `Score ${Number(deskScore).toFixed(2)}` : (regimeCaption || '')));

    // 8. EM Breach (multi-row)
    const breachVal = (breach1x != null || breach15x != null || breach2x != null)
      ? `${fmtPct(breach1x, 0)} / ${fmtPct(breach15x, 0)} / ${fmtPct(breach2x, 0)}`
      : '—';
    cards.appendChild(card('EM Breach', breachVal,
      `1× / 1.5× / 2× historical breach${breachN != null ? ' · n=' + breachN : ''}`));

    // 9. Events Harvested
    cards.appendChild(card('Events Harvested', String(evN),
      evN >= 8 ? 'Sufficient for replay' : 'Thin; consider running backfill'));

    // Coverage box
    const cov = (data.chainCoverage || {});
    const covBox = $('coverageBox');
    const days = cov.daysCovered || 0;
    if (days > 0) {
      covBox.textContent =
        `Chain cache: ${days} days of history for ${data.ticker} ` +
        (cov.minDate ? `(${cov.minDate} → ${cov.maxDate})` : '') +
        `, ${cov.totalRows || 0} rows. ` +
        (days >= 2 * 8
          ? 'Ready for replay.'
          : 'Cache is thin — /scenario may backfill on-demand (can take 10-30s).');
      covBox.style.display = '';
    } else {
      covBox.textContent =
        `No chain cache for ${data.ticker} yet. The first scenario run will backfill on-demand — ` +
        `expect a 10-30s wait while ORATS historical slices warm up.`;
      covBox.style.display = '';
    }

    // Pre-fill Step 2 form
    show('e1Summary');
    show('scenarioForm');
    prefillScenarioForm(data);
  }

  function prefillScenarioForm(scan) {
    const s = scan.engine1Summary || {};
    const e1 = scan.engine1 || {};
    const next = e1.nextEvent || {};
    const current = e1.current || {};
    const em = e1.expectedMove || {};

    const earnDate = s.nextEventDate || next.earnDate || next.earnDateNext || next.date || '';
    const timing = String(s.anncTod || next.timing || next.timingPlanned || next.anncTod || 'BMO').toUpperCase();
    const stock = (s.stockPrice != null ? s.stockPrice
      : (s.straddleSpotPrice != null ? s.straddleSpotPrice : current.stockPrice));
    // Prefer delayed ORATS EM (strike-target basis on E1) then live ORATS,
    // then straddle EM, so pre-market prefill always lands on a real number.
    const emPct = s.delayedEmPct != null ? s.delayedEmPct
      : s.oratsEmPct != null ? s.oratsEmPct
      : s.straddleEmPct != null ? s.straddleEmPct
      : (current.delayedImpliedMovePct || current.impliedMovePct || em.expectedMovePct || null);
    // Prefer the upcoming Friday expiry E1 already computed.
    const pricingExpiry = s.nextEventPricingExpiry || s.straddleExpiry || next.pricingExpiry || em.expiry || '';

    $('earningsDate').value = earnDate;
    $('earningsTiming').value = ['BMO', 'AMC', 'UNK'].includes(timing) ? timing : 'BMO';

    if (earnDate) {
      const entry = timing === 'AMC' ? earnDate : shiftBizDays(earnDate, -1);
      const exit = timing === 'AMC' ? shiftBizDays(earnDate, 1) : earnDate;
      $('entryDate').value = entry;
      $('plannedExitDate').value = exit;
      // Prefer E1's computed straddle expiry over a heuristic shift.
      $('expiry').value = pricingExpiry ? String(pricingExpiry).slice(0, 10) : shiftBizDays(earnDate, 4);
    } else if (pricingExpiry) {
      // No earnings date — still anchor the expiry field so the form isn't empty.
      $('expiry').value = String(pricingExpiry).slice(0, 10);
    }

    const stockNum = stock != null ? Number(stock) : null;
    const emNum = emPct != null ? Number(emPct) : null;
    if (stockNum && emNum) {
      const emDollars = stockNum * (emNum / 100.0);
      const tickStep = stockNum < 50 ? 1 : (stockNum < 200 ? 1 : 5);
      const snap = (v) => Math.round(v / tickStep) * tickStep;
      const shortPut = snap(stockNum - 1.5 * emDollars);
      const longPut = snap(shortPut - Math.max(tickStep * 2, 2));
      const shortCall = snap(stockNum + 1.5 * emDollars);
      const longCall = snap(shortCall + Math.max(tickStep * 2, 2));
      $('shortPut').value = shortPut;
      $('longPut').value = longPut;
      $('shortCall').value = shortCall;
      $('longCall').value = longCall;
      const wing = Math.max(shortPut - longPut, longCall - shortCall, tickStep);
      $('creditReceived').value = (wing * 0.2).toFixed(2);
    }
  }

  // ------------------------------------------------------------------
  // Step 2: /api/earnings-ic/scenario
  // ------------------------------------------------------------------
  async function doScenario(evt) {
    if (evt) evt.preventDefault();
    if (!state.scan) {
      banner('Run Step 1 (ticker scan) first.', 'error');
      return;
    }
    const body = collectScenarioBody();
    if (!body) return;

    setStatus('runStatus', 'Running replay…');
    $('runBtn').disabled = true;
    banner('');
    hide('results');
    try {
      const data = await postJson('/api/earnings-ic/scenario', body);
      state.scenario = data;
      renderScenario(data);
      setStatus('runStatus', 'Done.');
      show('results');
    } catch (err) {
      banner('Scenario failed: ' + err.message, 'error');
      setStatus('runStatus', 'Error');
    } finally {
      $('runBtn').disabled = false;
    }
  }

  function collectScenarioBody() {
    const t = ($('ticker').value || '').trim().toUpperCase();
    const body = {
      ticker: t,
      entryDate: $('entryDate').value,
      expiry: $('expiry').value,
      earningsDate: $('earningsDate').value,
      earningsTiming: $('earningsTiming').value,
      plannedExitDate: $('plannedExitDate').value,
      plannedExitOffsetHours: parseFloat($('plannedExitOffsetHours').value),
      longPut: parseFloat($('longPut').value),
      shortPut: parseFloat($('shortPut').value),
      shortCall: parseFloat($('shortCall').value),
      longCall: parseFloat($('longCall').value),
      creditReceived: parseFloat($('creditReceived').value),
      profitTargetPct: parseFloat($('profitTargetPct').value),
      stopLossPct: parseFloat($('stopLossPct').value),
      includeE1Payload: false,
    };
    const season = $('seasonMode').value;
    if (season === 'quarter') body.seasonMode = 'quarter';

    for (const k of ['entryDate','expiry','earningsDate','plannedExitDate',
                      'longPut','shortPut','shortCall','longCall','creditReceived']) {
      if (body[k] === '' || body[k] === null || Number.isNaN(body[k])) {
        banner(`Missing field: ${k}`, 'error');
        return null;
      }
    }
    if (!(body.longPut < body.shortPut && body.shortPut < body.shortCall
          && body.shortCall < body.longCall)) {
      banner('Strikes must satisfy: longPut < shortPut < shortCall < longCall', 'error');
      return null;
    }
    return body;
  }

  // ------------------------------------------------------------------
  // Renderers
  // ------------------------------------------------------------------
  function renderScenario(d) {
    if (!d) return;
    if ((d.eventsUsed || 0) === 0) {
      banner((d.notes || ['No events used.'])[0], 'error');
    }
    renderEntryState(d);
    renderPlannedExit(d);
    renderCreditRichness(d);
    renderOutcomeDistribution('outcomePanel', d.outcomeDistribution || {});
    renderAdjusted(d);
    renderModifiers(d);
    renderMtmChart(d);
    renderExpectedValue(d);
    renderExitRules(d);
    renderMatchedEvents(d);
    renderDroppedEvents(d);
    renderNotes(d);
  }

  function renderEntryState(d) {
    const cards = $('entryCards');
    cards.innerHTML = '';
    const es = d.entryState || {};
    const req = d.request || {};
    cards.appendChild(card('Ticker', req.ticker || '—',
      `Entry: ${req.entry_date || '—'}  →  Expiry: ${req.expiry || '—'}`));
    cards.appendChild(card('Entry Spot', fmtNum(es.userSpot, 2),
      `Wing: ${fmtNum(es.wingWidth, 2)} pts`));
    cards.appendChild(card('1σ EM (entry→expiry)', fmtPct(es.userEmPct, 2),
      es.userEmSource || ''));
    cards.appendChild(card('Credit / Max Loss',
      `$${fmtNum(req.credit_received, 2)}`,
      `MaxLoss ≈ $${fmtNum((es.wingWidth || 0) - (req.credit_received || 0), 2)}`));
    cards.appendChild(card('Events Used',
      `${d.eventsUsed || 0} / ${d.eventsConsidered || 0}`,
      (d.dataQuality && d.dataQuality.minEventsMet) ? 'Min pool met' : 'Thin sample'));
    cards.appendChild(card('Profit Target / Stop',
      `${fmtInt(req.profit_target_pct)}% / ${fmtInt(req.stop_loss_pct)}%`,
      'Of credit received'));
  }

  function renderPlannedExit(d) {
    $('plannedExitCaveat').textContent =
      (d.plannedExit && d.plannedExit.fidelityCaveat) || '';
    const cards = $('plannedExitCards');
    cards.innerHTML = '';
    const pe = d.plannedExit || {};
    const req = d.request || {};
    cards.appendChild(card('Earnings', req.earnings_date || '—',
      `${req.earnings_timing || '—'}`));
    cards.appendChild(card('Entry → Exit',
      `${req.entry_date || '—'} → ${pe.plannedExitDate || '—'}`,
      `+${fmtNum(pe.plannedExitOffsetHours, 1)}h after open`));
    cards.appendChild(card('Hold (biz days)', fmtInt(pe.holdBizDays),
      'Time-stop enforced in replay'));
    cards.appendChild(card('Crush Factor', fmtNum(pe.intradayCrushFactor, 2),
      'EOD→AM exit approximation'));
    cards.appendChild(card('Fill Model',
      (d.fillModel && d.fillModel.mode) || '—',
      (d.fillModel && d.fillModel.mode === 'mid_penalty')
        ? `+${fmtInt(d.fillModel.penaltyPct)}% half-spread` : ''));
  }

  function renderCreditRichness(d) {
    // Credit richness compares the user's forward credit to the
    // per-analogue mean natural credit. Surfaces the pre-market
    // wide-spread / stale-IV risk visibly before trade submit.
    const cr = d && d.creditRichness;
    let host = $('creditRichnessPanel');
    if (!host) {
      // Inject a minimal container next to the planned-exit cards so we
      // don't require template edits.
      const anchor = $('plannedExitCards') || $('entryCards');
      if (!anchor || !anchor.parentNode) return;
      host = document.createElement('div');
      host.id = 'creditRichnessPanel';
      host.style.marginTop = '8px';
      host.style.padding = '10px 12px';
      host.style.borderRadius = '8px';
      host.style.fontSize = '13px';
      anchor.parentNode.insertBefore(host, anchor.nextSibling);
    }
    if (!cr || cr.analogueMean == null) {
      host.style.display = 'none';
      return;
    }
    host.style.display = 'block';
    const tone = cr.verdict === 'user_cheap' ? ['#fef3c7', '#92400e']
      : cr.verdict === 'user_rich' ? ['#fee2e2', '#991b1b']
      : ['#ecfdf5', '#065f46'];
    host.style.background = tone[0];
    host.style.color = tone[1];
    const userCr = (cr.userCredit != null) ? `$${Number(cr.userCredit).toFixed(2)}` : '—';
    const meanCr = (cr.analogueMean != null) ? `$${Number(cr.analogueMean).toFixed(2)}` : '—';
    const delta  = (cr.deltaPct != null) ? `${cr.deltaPct >= 0 ? '+' : ''}${Number(cr.deltaPct).toFixed(0)}%` : '—';
    host.innerHTML = `<b>Credit richness (${cr.verdict || '—'}):</b> `
      + `user ${userCr} vs. ${cr.n || 0}-event analogue mean ${meanCr} (${delta}).`
      + (cr.note ? `<div style="margin-top:4px;opacity:.85">${cr.note}</div>` : '');
  }

  const OUTCOME_META = {
    fullCollect:  { label: 'Full Collect',  color: '#15803d' },
    earlyTarget:  { label: 'Early Target',  color: '#0ea5e9' },
    whiteKnuckle: { label: 'White Knuckle', color: '#64748b' },
    breach:       { label: 'Breach',        color: '#b91c1c' },
    stopOut:      { label: 'Stop Out',      color: '#d97706' },
  };

  function renderOutcomeDistribution(panelId, dist) {
    const panel = $(panelId);
    panel.innerHTML = '';
    const keys = ['fullCollect', 'earlyTarget', 'whiteKnuckle', 'breach', 'stopOut'];
    let dominant = keys[0];
    let best = -1;
    keys.forEach(k => {
      const pct = ((dist[k] || {}).pct) || 0;
      if (pct > best) { best = pct; dominant = k; }
    });
    keys.forEach(k => {
      const meta = OUTCOME_META[k];
      const v = dist[k] || { pct: 0, n: 0, avgPnlPct: 0, avgDays: 0 };
      const c = el('div', 'e15OutcomeCard' + (k === dominant ? ' dominant' : ''));
      c.appendChild(el('div', 'e15OutcomeName', meta.label));
      const pctEl = el('div', 'e15OutcomePct', fmtPct(v.pct, 0));
      pctEl.style.color = meta.color;
      c.appendChild(pctEl);
      c.appendChild(el('div', 'e15OutcomeMeta',
        `n=${v.n || 0} · Avg P&L ${fmtPct(v.avgPnlPct, 0)}`));
      const bar = el('div', 'e15OutcomeBar');
      const fill = el('div', 'e15OutcomeBarFill');
      fill.style.width = `${Math.max(0, Math.min(100, Number(v.pct) || 0))}%`;
      fill.style.background = meta.color;
      bar.appendChild(fill);
      c.appendChild(bar);
      panel.appendChild(c);
    });
  }

  function renderAdjusted(d) {
    const adj = d.adjustedOutcomeDistribution;
    const cs = d.conditioningSummary;
    if (!adj || !Object.keys(adj).length) {
      $('adjustedDividerLabel').style.display = 'none';
      $('adjustedOutcomePanel').style.display = 'none';
      $('conditioningSummary').style.display = 'none';
      return;
    }
    $('adjustedDividerLabel').style.display = '';
    $('adjustedOutcomePanel').style.display = '';
    if (cs && cs.summary) {
      const el = $('conditioningSummary');
      el.textContent = cs.summary;
      el.style.display = '';
    } else {
      $('conditioningSummary').style.display = 'none';
    }
    renderOutcomeDistribution('adjustedOutcomePanel', adj);
  }

  function renderModifiers(d) {
    const mods = d.conditioningModifiers || {};
    const modifiers = mods.modifiers || [];
    const panel = $('modifiersPanel');
    const divider = $('modifiersDivider');
    panel.innerHTML = '';
    if (!modifiers.length) {
      panel.style.display = 'none';
      divider.style.display = 'none';
      return;
    }
    divider.style.display = '';
    panel.style.display = '';
    modifiers.forEach(m => {
      const c = el('div', 'e15Card');
      c.appendChild(el('div', 'e15CardLabel', m.name || 'modifier'));
      const tilt = m.tailMult != null ? `×${Number(m.tailMult).toFixed(2)} tails` : '';
      const wr = m.wrShiftPct != null ? `${m.wrShiftPct > 0 ? '+' : ''}${Number(m.wrShiftPct).toFixed(1)}pp WR` : '';
      c.appendChild(el('div', 'e15CardValue', [tilt, wr].filter(Boolean).join(' · ') || '—'));
      if (m.reason || m.note) {
        c.appendChild(el('div', 'e15CardCaption', m.reason || m.note));
      }
      panel.appendChild(c);
    });
  }

  function renderMtmChart(d) {
    const timeline = d.mtmTimeline || [];
    if (state.mtmChart) { state.mtmChart.destroy(); state.mtmChart = null; }
    if (!timeline.length || typeof Chart === 'undefined') return;
    const ctx = $('mtmChart').getContext('2d');
    const labels = timeline.map(p => `D${p.day}`);
    const p10 = timeline.map(p => p.p10);
    const p50 = timeline.map(p => p.p50);
    const p90 = timeline.map(p => p.p90);
    state.mtmChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [
          { label: 'P10', data: p10, borderColor: '#b91c1c', backgroundColor: 'rgba(185,28,28,0.1)', borderWidth: 1.5, tension: 0.2 },
          { label: 'P50', data: p50, borderColor: '#2563eb', backgroundColor: 'rgba(37,99,235,0.15)', borderWidth: 2, tension: 0.2 },
          { label: 'P90', data: p90, borderColor: '#15803d', backgroundColor: 'rgba(21,128,61,0.1)', borderWidth: 1.5, tension: 0.2 },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'bottom' } },
        scales: {
          y: { title: { display: true, text: 'P&L %' } },
          x: { title: { display: true, text: 'Biz day since entry' } },
        },
      },
    });
  }

  function renderExpectedValue(d) {
    const ev = d.expectedValue || {};
    const panel = $('evPanel');
    panel.innerHTML = '';
    panel.appendChild(card('Mean P&L', fmtPct(ev.meanPnlPct, 1)));
    panel.appendChild(card('Median P&L', fmtPct(ev.medianPnlPct, 1)));
    panel.appendChild(card('Sharpe-proxy', fmtNum(ev.sharpeProxy, 2),
      'μ/σ across replayed events'));
    const ci = d.outcomeDistributionCI || {};
    if (ci.fullCollect && ci.fullCollect.ci) {
      const full = ci.fullCollect;
      panel.appendChild(card('FullCollect 90% CI',
        `[${fmtPct(full.ci[0], 0)}, ${fmtPct(full.ci[1], 0)}]`,
        `n=${(ci._meta || {}).n || 0} bootstrap iters`));
    }
  }

  function renderExitRules(d) {
    const eo = d.exitRulesOptimization || {};
    const panel = $('exitRulesPanel');
    panel.innerHTML = '';
    panel.appendChild(card('Recommended PT',
      fmtInt(eo.recommendedProfitTarget) + '%',
      'of credit'));
    panel.appendChild(card('Recommended SL',
      fmtInt(eo.recommendedStopLoss) + '%',
      'of credit'));
    panel.appendChild(card('Time-stop (days)',
      fmtInt(eo.recommendedTimeStopDays),
      'Hard exit after N biz days'));
    const delta = eo.deltaFromDefault || {};
    const wrDelta = delta.winRatePct;
    const pnlDelta = delta.avgPnlPct;
    panel.appendChild(card('Δ vs default',
      `${wrDelta != null ? ((wrDelta >= 0 ? '+' : '') + fmtPct(wrDelta, 1)) : '—'} WR`,
      pnlDelta != null ? `${(pnlDelta >= 0 ? '+' : '') + fmtPct(pnlDelta, 1)} avg P&L` : ''));
  }

  function renderMatchedEvents(d) {
    const events = d.matchedEvents || [];
    const wrap = $('matchedEventsWrap');
    wrap.innerHTML = '';
    if (!events.length) {
      wrap.appendChild(el('div', 'e15SectionSubtle', 'No analogue events.'));
      return;
    }
    const table = el('table', 'e15Table');
    const thead = el('thead');
    const trh = el('tr');
    ['Earn Date','Timing','Entry','Exit','Expiry','Outcome','Exit Day','P&L','MAE','Breached','EM %','Realized %','Analogue Credit'].forEach(h => {
      trh.appendChild(el('th', null, h));
    });
    thead.appendChild(trh);
    table.appendChild(thead);
    const tbody = el('tbody');
    events.forEach(ev => {
      const tr = el('tr');
      tr.appendChild(el('td', null, ev.earnDate || '—'));
      tr.appendChild(el('td', null, ev.anncTod || '—'));
      tr.appendChild(el('td', null, ev.entryDateHist || '—'));
      tr.appendChild(el('td', null, ev.plannedExitDateHist || '—'));
      tr.appendChild(el('td', null, ev.expiryHist || '—'));
      tr.appendChild(el('td', null, ev.outcome || '—'));
      tr.appendChild(el('td', null, fmtInt(ev.exitDay)));
      const pnlTd = el('td', null, fmtPct(ev.pnlPct, 1));
      if (ev.pnlPct != null) pnlTd.className = ev.pnlPct >= 0 ? 'win' : 'loss';
      tr.appendChild(pnlTd);
      tr.appendChild(el('td', null, fmtPct(ev.mae, 1)));
      tr.appendChild(el('td', null, ev.breached ? 'yes' : ''));
      tr.appendChild(el('td', null, fmtPct(ev.impliedMovePct, 2)));
      tr.appendChild(el('td', null, fmtPct(ev.realizedMovePct, 2)));
      tr.appendChild(el('td', null,
        (ev.analogueEntryCredit != null) ? `$${Number(ev.analogueEntryCredit).toFixed(2)}` : '—'));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
  }

  function renderDroppedEvents(d) {
    const drops = d.droppedEvents || [];
    const wrap = $('droppedEventsWrap');
    const divider = $('droppedDivider');
    wrap.innerHTML = '';
    if (!drops.length) {
      wrap.style.display = 'none';
      divider.style.display = 'none';
      return;
    }
    divider.style.display = '';
    wrap.style.display = '';
    const table = el('table', 'e15Table');
    const thead = el('thead');
    const trh = el('tr');
    ['Earn Date', 'Reason'].forEach(h => trh.appendChild(el('th', null, h)));
    thead.appendChild(trh);
    table.appendChild(thead);
    const tbody = el('tbody');
    drops.forEach(x => {
      const tr = el('tr');
      tr.appendChild(el('td', null, x.earnDate || x.date || '—'));
      tr.appendChild(el('td', null, x.reason || '—'));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
  }

  function renderNotes(d) {
    const ul = $('notesList');
    ul.innerHTML = '';
    (d.notes || []).forEach(n => {
      const li = document.createElement('li');
      li.textContent = n;
      ul.appendChild(li);
    });
  }

  // ------------------------------------------------------------------
  // Actions
  // ------------------------------------------------------------------
  async function doJournal() {
    if (!state.scenario) return;
    setStatus('actionStatus', 'Saving to journal…');
    try {
      const data = await postJson('/api/earnings-ic/journal', {
        request: state.scenario.request,
        scenario: state.scenario,
      });
      setStatus('actionStatus',
        `Saved. Trade ID: ${data.tradeId}. ` +
        `View: ${data.viewUrl || '/earnings-ic?tradeId=' + data.tradeId}`);
    } catch (err) {
      setStatus('actionStatus', 'Journal failed: ' + err.message);
    }
  }

  async function doAdvisor() {
    if (!state.scenario) return;
    setStatus('actionStatus', 'Running advisor…');
    $('advisorPanel').innerHTML = '';
    try {
      const data = await postJson('/api/earnings-ic/advisor', {
        scenario: state.scenario,
        engine1: state.scenario.engine1 || ((state.scan || {}).engine1),
      });
      renderAdvisor(data);
      setStatus('actionStatus', 'Advisor complete.');
    } catch (err) {
      setStatus('actionStatus', 'Advisor failed: ' + err.message);
    }
  }

  function renderAdvisor(a) {
    const p = $('advisorPanel');
    p.innerHTML = '';
    if (!a) return;

    const header = el('div');
    header.style.cssText = 'display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:8px;';
    const v = a.verdict || '—';
    const vChip = el('span', 'e15Chip');
    vChip.textContent = v;
    if (v === 'GO') vChip.className = 'e15Chip tailwind';
    else if (v === 'PASS') vChip.className = 'e15Chip headwind';
    else vChip.className = 'e15Chip neutral';
    vChip.style.cssText += 'font-size:13px;padding:4px 12px;';
    header.appendChild(vChip);
    if (a.confidence != null) {
      header.appendChild(el('span', 'e15SectionSubtle',
        `Confidence ${a.confidence}% · Stance: ${a.stance || 'neutral'}`));
    }
    p.appendChild(header);

    if (a.narrative) {
      const n = el('div', 'e15Caveat', a.narrative);
      p.appendChild(n);
    }
    if (a.deskNote) {
      const dn = el('div');
      dn.style.cssText = 'font-size:12px;font-style:italic;color:var(--muted);margin-bottom:10px;';
      dn.textContent = '"' + a.deskNote + '"';
      p.appendChild(dn);
    }

    if (Array.isArray(a.keyPoints) && a.keyPoints.length) {
      p.appendChild(el('div', 'e15CardLabel', 'Key points'));
      const ul = el('ul', 'e15NoteList');
      a.keyPoints.forEach(b => {
        const li = document.createElement('li');
        li.textContent = b;
        ul.appendChild(li);
      });
      p.appendChild(ul);
    }
    if (Array.isArray(a.risks) && a.risks.length) {
      p.appendChild(el('div', 'e15CardLabel', 'Risks'));
      const ul = el('ul', 'e15NoteList');
      a.risks.forEach(b => {
        const li = document.createElement('li');
        li.textContent = b;
        ul.appendChild(li);
      });
      p.appendChild(ul);
    }
    if (Array.isArray(a.suggestedAdjustments) && a.suggestedAdjustments.length) {
      p.appendChild(el('div', 'e15CardLabel', 'Suggested adjustments'));
      const ul = el('ul', 'e15NoteList');
      a.suggestedAdjustments.forEach(x => {
        const li = document.createElement('li');
        li.innerHTML = `<strong>[${x.type}]</strong> ${x.suggestion}`
          + (x.rationale ? ` <span style="color:var(--muted);">— ${x.rationale}</span>` : '');
        ul.appendChild(li);
      });
      p.appendChild(ul);
    }
    if (a.plannedExitNote) {
      p.appendChild(el('div', 'e15SectionSubtle', 'Planned-exit: ' + a.plannedExitNote));
    }
    const src = a._source ? `Source: ${a._source}` : '';
    const model = a._model ? ` · ${a._model}` : '';
    const fb = a._fallback_reason ? ` · ${a._fallback_reason}` : '';
    if (src || model || fb) {
      p.appendChild(el('div', 'e15SectionSubtle', src + model + fb));
    }
  }

  async function doReconcile() {
    if (!state.scenario) return;
    setStatus('actionStatus', 'Reconciling…');
    try {
      const data = await postJson('/api/earnings-ic/reconcile', {
        scenario: state.scenario,
      });
      const chip = (data.reconcile && data.reconcile.creditChip) || {};
      setStatus('actionStatus',
        `Reconcile: ${chip.status || 'unknown'}` +
        (chip.note ? ` — ${chip.note}` : ''));
    } catch (err) {
      setStatus('actionStatus', 'Reconcile failed: ' + err.message);
    }
  }

  async function doBackfill() {
    const t = ($('ticker').value || '').trim().toUpperCase();
    if (!t) return;
    const token = prompt('X-Admin-Token?');
    if (!token) return;
    setStatus('runStatus', 'Starting backfill…');
    try {
      const data = await postJson(
        '/api/earnings-ic/backfill',
        { ticker: t },
        { 'X-Admin-Token': token },
      );
      setStatus('runStatus', `Backfill kicked off for ${t}. ${JSON.stringify(data.state?.params || {})}`);
      pollBackfillStatus(t, token);
    } catch (err) {
      setStatus('runStatus', 'Backfill failed: ' + err.message);
    }
  }

  async function pollBackfillStatus(ticker, token) {
    for (let i = 0; i < 120; i++) {
      await new Promise(r => setTimeout(r, 2000));
      try {
        const s = await getJson('/api/earnings-ic/backfill/status?ticker=' + encodeURIComponent(ticker));
        const p = s.progress || {};
        if (!s.running) {
          const cov = s.coverage || {};
          setStatus('runStatus',
            `Backfill done. ${s.result?.succeeded || 0} succeeded, ${s.result?.failed || 0} failed. ` +
            `Cache: ${cov.daysCovered || 0} days.`);
          return;
        }
        setStatus('runStatus',
          `Backfill running: ${p.completed || 0}/${p.total || '?'} ok=${p.succeeded || 0} fail=${p.failed || 0}`);
      } catch (err) { /* swallow */ }
    }
  }

  // ------------------------------------------------------------------
  // Init
  // ------------------------------------------------------------------
  function init() {
    $('scanForm').addEventListener('submit', doScan);
    $('scenarioForm').addEventListener('submit', doScenario);
    $('journalBtn').addEventListener('click', doJournal);
    $('advisorBtn').addEventListener('click', doAdvisor);
    $('reconcileBtn').addEventListener('click', doReconcile);
    $('backfillBtn').addEventListener('click', doBackfill);

    // Check feature flag
    getJson('/api/earnings-ic/health').then(h => {
      if (!h.enabled) {
        banner('Engine 15 is disabled on the server (ENABLE_ENGINE15_EARNINGS_IC=0).', 'error');
        $('scanBtn').disabled = true;
      }
    }).catch(() => { /* ignore */ });

    // Support deep-link: /earnings-ic?ticker=GE
    try {
      const q = new URLSearchParams(window.location.search);
      const t = (q.get('ticker') || '').trim().toUpperCase();
      if (t) {
        $('ticker').value = t;
        doScan();
      }
    } catch (e) { /* ignore */ }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
