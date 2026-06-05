/* AlgoBot Terminal — vanilla polling dashboard
 * Reads JSON snapshots written by bot.py to /reports/
 *   - status.json         every  5s
 *   - portfolio.json      every 15s
 *   - candidates.json     every 60s
 *   - trades.json         every 30s
 *   - equity_curve.json   every 30s
 *   - strategy_state.json every 10s
 *   - log_tail.txt        every  5s
 */

const POLL = {
  status: 5_000,
  portfolio: 15_000,
  candidates: 60_000,
  trades: 30_000,
  equity: 30_000,
  strategy: 10_000,
  log: 5_000,
};

const ENDPOINTS = {
  status: "/status.json",
  portfolio: "/portfolio.json",
  candidates: "/candidates.json",
  trades: "/trades.json",
  equity: "/equity_curve.json",
  strategy: "/strategy_state.json",
  log: "/log_tail.txt",
  control: "/v2/api/control",
};

// ─────────────────────────────────────────────────────────
// formatters
// ─────────────────────────────────────────────────────────
const fmtUsd = (v, opts = {}) => {
  if (v === null || v === undefined || Number.isNaN(+v)) return "—";
  const n = +v;
  const sign = opts.signed && n > 0 ? "+" : "";
  return sign + n.toLocaleString("en-US", {
    style: "currency", currency: "USD",
    maximumFractionDigits: 2, minimumFractionDigits: 2,
  });
};

const fmtPct = (v, opts = {}) => {
  if (v === null || v === undefined || Number.isNaN(+v)) return "—";
  const n = +v * (opts.alreadyPct ? 1 : 100);
  const sign = (opts.signed !== false && n > 0) ? "+" : "";
  return `${sign}${n.toFixed(opts.digits ?? 2)}%`;
};

const fmtCompactUsd = (v) => {
  if (v === null || v === undefined || Number.isNaN(+v)) return "—";
  const n = +v;
  if (Math.abs(n) >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`;
  if (Math.abs(n) >= 10_000)    return `$${(n / 1_000).toFixed(1)}k`;
  return `$${n.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
};

const NY_TZ = "America/New_York";

const fmtTime = (iso) => {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  } catch { return "—"; }
};

// Format clock / Next Sell / Next Buy — always in New York timezone, regardless of
// browser locale. Without explicit timeZone the ISO offset gets re-projected into
// the user's local TZ which made 14:35 NY display as 20:35 Prague.
const fmtNY_HMS = (iso) => {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString("en-US", {
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      hour12: false, timeZone: NY_TZ,
    });
  } catch { return "—"; }
};

const fmtNY_HM = (iso) => {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString("en-US", {
      hour: "2-digit", minute: "2-digit",
      hour12: false, timeZone: NY_TZ,
    });
  } catch { return "—"; }
};

// Format Next Sell / Next Buy with day suffix when not today in NY ("tomorrow", "Mon").
// Without this 10:31 looks like 19 h away even though it's the day after.
const fmtNY_HM_DayAware = (iso, refIso) => {
  if (!iso) return "—";
  try {
    const target = new Date(iso);
    const ref = refIso ? new Date(refIso) : new Date();
    const dateFmt = new Intl.DateTimeFormat("en-US", { timeZone: NY_TZ, year: "numeric", month: "2-digit", day: "2-digit" });
    const tDate = dateFmt.format(target);
    const rDate = dateFmt.format(ref);
    const hm = target.toLocaleTimeString("en-US", {
      hour: "2-digit", minute: "2-digit", hour12: false, timeZone: NY_TZ,
    });
    if (tDate === rDate) return hm;
    // compute "tomorrow" vs weekday
    const dayMs = 86_400_000;
    const refMid = new Date(`${rDate}T00:00:00`);
    const tgtMid = new Date(`${tDate}T00:00:00`);
    const diffDays = Math.round((tgtMid - refMid) / dayMs);
    if (diffDays === 1) return `${hm} tomorrow`;
    if (diffDays > 1 && diffDays < 7) {
      const wk = target.toLocaleDateString("en-US", { weekday: "short", timeZone: NY_TZ });
      return `${hm} ${wk}`;
    }
    return `${hm} ${target.toLocaleDateString("en-US", { day: "numeric", month: "short", timeZone: NY_TZ })}`;
  } catch { return "—"; }
};

const fmtNY_Date = (iso) => {
  // Long-form date label in NY (so "Thursday" lines up with NY clock past midnight Prague).
  if (iso) {
    try {
      return new Date(iso).toLocaleDateString("en-US", {
        weekday: "short", day: "numeric", month: "short", year: "numeric",
        timeZone: NY_TZ,
      });
    } catch {}
  }
  return new Date().toLocaleDateString("en-US", {
    weekday: "short", day: "numeric", month: "short", year: "numeric",
    timeZone: NY_TZ,
  });
};

const gainCls = (v) => (v > 0 ? "gain" : (v < 0 ? "loss" : ""));

// ─────────────────────────────────────────────────────────
// fetch helpers
// ─────────────────────────────────────────────────────────
async function fetchJson(path) {
  try {
    const r = await fetch(path + "?t=" + Date.now(), { cache: "no-store" });
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

async function fetchText(path) {
  try {
    const r = await fetch(path + "?t=" + Date.now(), { cache: "no-store" });
    if (!r.ok) return null;
    return await r.text();
  } catch { return null; }
}

// ─────────────────────────────────────────────────────────
// state
// ─────────────────────────────────────────────────────────
const STATE = {
  status: null,
  portfolio: null,
  candidates: null,
  trades: null,
  equity: null,
  strategy: null,
  chartRange: "30d",
  chart: null,
  equitySeries: null,
  ddSeries: null,
};

// ─────────────────────────────────────────────────────────
// status / masthead / cockpit
// ─────────────────────────────────────────────────────────
function renderStatus(st) {
  if (!st) return;
  STATE.status = st;

  // Clock with seconds so it visibly ticks (status.json refreshes every 5s).
  document.getElementById("nyTime").textContent = fmtNY_HMS(st.ts_ny);
  document.getElementById("nextSell").textContent = fmtNY_HM_DayAware(st.next_sell_ny, st.ts_ny);
  document.getElementById("nextBuy").textContent = fmtNY_HM_DayAware(st.next_buy_ny, st.ts_ny);
  document.getElementById("dateLabel").textContent = fmtNY_Date(st.ts_ny);

  const markBadge = document.getElementById("markBadge");
  const ibBadge = document.getElementById("ibBadge");

  if (st.market_open) {
    markBadge.className = "pill live";
    markBadge.innerHTML = '<span class="dot live"></span>Market Open';
  } else {
    markBadge.className = "pill";
    markBadge.innerHTML = '<span class="dot"></span>Market Closed';
  }

  if (st.ib_connected) {
    ibBadge.className = "pill info";
    ibBadge.innerHTML = '<span class="dot"></span>IB Connected';
  } else {
    ibBadge.className = "pill bad";
    ibBadge.innerHTML = '<span class="dot"></span>IB Disconnected';
  }

  document.getElementById("cEquity").textContent = fmtCompactUsd(st.equity);
  document.getElementById("cCash").textContent = fmtCompactUsd(st.cash);
}

function renderCockpit() {
  const st = STATE.status;
  const port = STATE.portfolio;
  const strat = STATE.strategy;
  if (!st) return;

  // Daily realized PnL
  let dailyPnl = strat?.daily_realized_pnl;
  let dailyCount = strat?.daily_trades_count;
  if ((dailyPnl === undefined || dailyPnl === null) && STATE.trades) {
    const today = new Date().toISOString().slice(0, 10);
    const list = STATE.trades.items || STATE.trades || [];
    const t = list.filter(r => (r.ts || "").startsWith(today));
    dailyPnl = t.reduce((a, b) => a + (+b.pnl || 0), 0);
    dailyCount = t.length;
  }
  const dEl = document.getElementById("cDailyPnl");
  const dMeta = document.getElementById("cDailyMeta");
  if (dailyPnl !== undefined && dailyPnl !== null) {
    dEl.textContent = fmtUsd(dailyPnl, { signed: true });
    dEl.className = "value display " + gainCls(dailyPnl);
    dMeta.innerHTML = `${dailyCount ?? "—"} trades today`;
  }

  // Open PnL
  let openPnl = 0;
  let openCount = 0;
  if (port?.positions) {
    for (const p of port.positions) {
      const cost = (+p.avgCost || 0) * (+p.qty || 0);
      const mkt  = (+p.marketPrice || 0) * (+p.qty || 0);
      openPnl += mkt - cost;
      openCount++;
    }
  }
  const oEl = document.getElementById("cOpenPnl");
  const oMeta = document.getElementById("cOpenMeta");
  oEl.textContent = fmtUsd(openPnl, { signed: true });
  oEl.className = "value display " + gainCls(openPnl);
  oMeta.innerHTML = `${openCount} / ${strat?.max_positions ?? 5} positions`;

  // Risk meter
  const limit = strat?.daily_loss_limit_pct ?? 3.0;
  const equity = st.equity || 1;
  const dailyPct = ((dailyPnl ?? 0) / equity) * 100;
  let consumed = 0;
  if (dailyPct < 0) consumed = Math.min(100, Math.abs(dailyPct) / limit * 100);

  const dotsEl = document.getElementById("cRiskDots");
  const pctEl = document.getElementById("cRiskPct");
  const lblEl = document.getElementById("cRiskLabel");
  const stratPaused = !!strat?.paused;

  const filled = Math.round((consumed / 100) * 5);
  let cls = "dots";
  if (stratPaused) cls = "dots crit";
  else if (consumed > 80) cls = "dots crit";
  else if (consumed > 50) cls = "dots warn";

  dotsEl.className = cls;
  Array.from(dotsEl.children).forEach((c, i) => {
    if (consumed === 0 && !stratPaused) {
      c.classList.add("on");
    } else {
      c.classList.toggle("on", i < filled);
    }
  });

  pctEl.textContent = stratPaused ? "PAUSED" : `${consumed.toFixed(0)} %`;

  if (stratPaused) { lblEl.textContent = "HALTED"; lblEl.className = "value display loss"; }
  else if (consumed > 80) { lblEl.textContent = "CRITICAL"; lblEl.className = "value display loss"; }
  else if (consumed > 50) { lblEl.textContent = "WATCH"; lblEl.className = "value display warn"; }
  else { lblEl.textContent = "OK"; lblEl.className = "value display gain"; }

  // Strategy badge pill
  const sBadge = document.getElementById("strategyBadge");
  if (stratPaused) {
    sBadge.className = "pill bad";
    sBadge.innerHTML = '<span class="dot"></span>Paused';
  } else {
    sBadge.className = "pill live";
    sBadge.innerHTML = '<span class="dot live"></span>Active';
  }
}

// ─────────────────────────────────────────────────────────
// positions
// ─────────────────────────────────────────────────────────
function buildSparkline(values, gain) {
  if (!values || values.length < 2) return "";
  const w = 100, h = 48;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const pts = values.map((v, i) => {
    const x = (i / (values.length - 1)) * w;
    const y = h - ((v - min) / range) * h;
    return [x, y];
  });
  const linePath = pts.map((p, i) => (i === 0 ? "M" : "L") + p[0].toFixed(2) + "," + p[1].toFixed(2)).join(" ");
  const fillPath = linePath + ` L${w},${h} L0,${h} Z`;
  const cls = gain ? "gain" : "loss";
  return `
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <path class="fill ${cls}" d="${fillPath}"/>
      <path class="line ${cls}" d="${linePath}"/>
    </svg>`;
}

function trigBar(pct) {
  const v = Math.max(0, Math.min(100, +pct || 0));
  let cls = "trig";
  if (v > 80) cls += " crit";
  else if (v > 60) cls += " warn";
  return { cls, width: v };
}

function renderPositions(port) {
  STATE.portfolio = port;
  const host = document.getElementById("positionsHost");
  const countEl = document.getElementById("posCount");
  const strat = STATE.strategy;
  const max = strat?.max_positions ?? 5;

  if (!port || !port.positions || port.positions.length === 0) {
    host.innerHTML = `
      <div class="positions-empty">
        No open positions
        <div class="sub">Waiting for signal</div>
      </div>`;
    countEl.textContent = `0 / ${max}`;
    return;
  }

  countEl.textContent = `${port.positions.length} / ${max}`;

  host.innerHTML = port.positions.map(p => {
    const pnlPct = (+p.pnl_pct || 0) * 100;
    const isGain = pnlPct >= 0;
    const pnlCls = isGain ? "gain" : "loss";
    const pnlUsd = ((+p.marketPrice || 0) - (+p.avgCost || 0)) * (+p.qty || 0);

    const spark = buildSparkline(p.sparkline || [], isGain);

    const slPct = +(p.stop_loss_progress ?? 0);
    const tsPct = +(p.trailing_stop_progress ?? 0);
    const timePct = +(p.time_stop_progress ?? 0);

    const slBar = trigBar(slPct);
    const tsBar = trigBar(tsPct);
    const timeBar = trigBar(timePct);

    return `
      <article class="position-card fade-in">
        <div class="top">
          <div>
            <div class="sym">${p.symbol}</div>
            <div class="qty">${p.qty} sh</div>
          </div>
          <div>
            <div class="pnl-pct ${pnlCls}">${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(2)}%</div>
            <div class="pnl-usd">${pnlUsd >= 0 ? "+" : ""}${fmtUsd(pnlUsd)}</div>
          </div>
        </div>
        <div class="prices">
          <span>$${(+p.avgCost || 0).toFixed(2)}</span>
          <span class="arrow">→</span>
          <span>$${(+p.marketPrice || 0).toFixed(2)}</span>
          <span class="hold">${p.holding_bars ?? "—"}h held</span>
        </div>
        <div class="sparkbox">${spark}</div>
        <div class="triggers">
          <div class="${slBar.cls}">
            <span class="l">SL</span>
            <div class="bar"><i style="width:${slBar.width}%"></i></div>
            <span class="v">${slPct.toFixed(0)}%</span>
          </div>
          <div class="${tsBar.cls}">
            <span class="l">TS</span>
            <div class="bar"><i style="width:${tsBar.width}%"></i></div>
            <span class="v">${tsPct.toFixed(0)}%</span>
          </div>
          <div class="${timeBar.cls}">
            <span class="l">Time</span>
            <div class="bar"><i style="width:${timePct.toFixed(0)}%"></i></div>
            <span class="v">${timePct.toFixed(0)}%</span>
          </div>
        </div>
      </article>`;
  }).join("");
}

// ─────────────────────────────────────────────────────────
// watchlist
// ─────────────────────────────────────────────────────────
function scoreCls(s) {
  if (s == null) return "low";
  if (s >= 0.7) return "high";
  if (s >= 0.4) return "mid";
  return "low";
}

function renderWatchlist(cands) {
  STATE.candidates = cands;
  const host = document.getElementById("watchHost");
  const countEl = document.getElementById("watchCount");

  // remove old non-header rows
  while (host.children.length > 1) host.removeChild(host.lastChild);

  if (!cands || !cands.items || cands.items.length === 0) {
    countEl.textContent = "0";
    const empty = document.createElement("div");
    empty.className = "watchlist-empty";
    empty.textContent = "Watchlist populates after BUY cycle (10:31 NY hourly)";
    host.appendChild(empty);
    return;
  }

  countEl.textContent = String(cands.items.length);

  for (const c of cands.items.slice(0, 15)) {
    const sigCls = c.is_buy_signal ? "row signal" : "row";
    const sc = c.score ?? null;
    const scTxt = sc == null ? "—" : sc.toFixed(2);
    const scCls = "score " + scoreCls(sc);
    const drop = (+c.drop || 0) * 100;
    const dropCls = drop >= 0 ? "drop up" : "drop";

    const rsi = c.rsi != null ? `<span><span class="l">RSI</span>${(+c.rsi).toFixed(0)}</span>` : "";
    const bb = c.bb_pct != null ? `<span><span class="l">BB</span>${(+c.bb_pct * 100).toFixed(0)}</span>` : "";

    const row = document.createElement("div");
    row.className = sigCls;
    row.innerHTML = `
      <span class="sym">${c.symbol}</span>
      <span class="${scCls}">${scTxt}</span>
      <span class="metrics">${rsi}${bb}</span>
      <span class="${dropCls}">${drop >= 0 ? "+" : ""}${drop.toFixed(1)}%</span>
    `;
    host.appendChild(row);
  }
}

// ─────────────────────────────────────────────────────────
// strategy panel
// ─────────────────────────────────────────────────────────
function renderStrategy(strat) {
  STATE.strategy = strat;
  if (!strat) return;

  document.getElementById("stratName").textContent = strat.name || "—";
  if (strat.mode) {
    const m = document.getElementById("modeBadge");
    m.textContent = String(strat.mode).toUpperCase();
    m.className = "pill " + (String(strat.mode).toLowerCase() === "live" ? "bad" : "warn");
  }

  const host = document.getElementById("stratHost");
  const params = strat.params || {};
  const rows = [
    ["Strategy", strat.name || "—"],
    ["Buy drop", params.buy_drop != null ? fmtPct(params.buy_drop, { signed: false }) : "—"],
    ["Sell gain", params.sell_gain != null ? fmtPct(params.sell_gain, { signed: false }) : "—"],
    ["Stop loss", params.stop_loss != null ? fmtPct(params.stop_loss, { signed: false }) : "—"],
    ["Trailing", params.trailing_stop_pct != null ? fmtPct(params.trailing_stop_pct, { signed: false }) : "—"],
    ["Time stop", params.time_stop_bars != null ? `${params.time_stop_bars} bars` : "—"],
    ["RSI limit", params.rsi_limit != null ? `< ${params.rsi_limit}` : "—"],
  ];

  Array.from(host.querySelectorAll(".row")).forEach(r => r.remove());
  const cdList = host.querySelector(".cooldown-list");
  for (const [l, v] of rows) {
    const div = document.createElement("div");
    div.className = "row";
    div.innerHTML = `<span class="l">${l}</span><span class="v">${v}</span>`;
    host.insertBefore(div, cdList);
  }

  const cdHost = document.getElementById("cooldownList");
  cdHost.innerHTML = "";
  for (const cd of (strat.cooldowns || [])) {
    const span = document.createElement("span");
    span.className = "cd-pill";
    const mins = Math.ceil((cd.remaining_sec || 0) / 60);
    const sideLbl = cd.side ? `${cd.side} ` : "";
    span.textContent = `${sideLbl}${cd.symbol} · ${mins}m`;
    cdHost.appendChild(span);
  }

  const lc = strat.last_buy_cycle || strat.last_sell_cycle || "—";
  document.getElementById("stratLastCycle").textContent = `Cycle ${lc}`;

  const btn = document.getElementById("pauseBtn");
  const lbl = document.getElementById("pauseLabel");
  if (strat.paused) {
    btn.classList.add("paused");
    lbl.textContent = "Resume Trading";
  } else {
    btn.classList.remove("paused");
    lbl.textContent = "Pause Trading";
  }
}

// ─────────────────────────────────────────────────────────
// trades
// ─────────────────────────────────────────────────────────
function renderTrades(trades) {
  STATE.trades = trades;
  const host = document.getElementById("tradesHost");
  const countEl = document.getElementById("tradesCount");

  const list = trades?.items || trades || [];
  countEl.textContent = `${list.length} trades`;

  if (!list.length) {
    host.innerHTML = `<tr><td colspan="7" style="text-align:center; padding:40px; color:var(--ink-quiet);">No trades</td></tr>`;
    return;
  }

  host.innerHTML = list.map(t => {
    const pnl = +t.pnl || 0;
    const cls = pnl > 0 ? "gain" : (pnl < 0 ? "loss" : "");
    const ts = (t.ts || "").replace("T", " ").slice(0, 16);
    const note = t.note || "";
    return `
      <tr>
        <td>${ts}</td>
        <td><span class="action-badge ${t.action === "BUY" ? "buy" : "sell"}">${t.action}</span></td>
        <td class="sym">${t.symbol}</td>
        <td>${t.qty}</td>
        <td>$${(+t.price || 0).toFixed(2)}</td>
        <td class="${cls}">${pnl > 0 ? "+" : ""}${pnl.toFixed(2)}</td>
        <td class="note">${note}</td>
      </tr>`;
  }).join("");
}

// ─────────────────────────────────────────────────────────
// equity chart (lightweight-charts)
// ─────────────────────────────────────────────────────────
function initChart() {
  const host = document.getElementById("chartHost");
  host.innerHTML = "";
  if (typeof LightweightCharts === "undefined") {
    host.innerHTML = '<div style="padding:40px; color:var(--ink-quiet); text-align:center;">Chart library failed to load.</div>';
    return;
  }
  const chart = LightweightCharts.createChart(host, {
    layout: {
      background: { type: "solid", color: "transparent" },
      textColor: "#7b8493",
      fontFamily: "'JetBrains Mono', monospace",
      fontSize: 11,
    },
    grid: {
      vertLines: { color: "rgba(255,255,255,0.03)" },
      horzLines: { color: "rgba(255,255,255,0.03)" },
    },
    rightPriceScale: { borderColor: "rgba(255,255,255,0.06)" },
    timeScale: { borderColor: "rgba(255,255,255,0.06)", timeVisible: false },
    crosshair: {
      vertLine: { color: "rgba(122,168,255,0.35)", width: 1, labelBackgroundColor: "#7aa8ff" },
      horzLine: { color: "rgba(122,168,255,0.35)", width: 1, labelBackgroundColor: "#7aa8ff" },
    },
    width: host.clientWidth,
    height: 340,
    handleScroll: true,
    handleScale: true,
  });

  STATE.equitySeries = chart.addAreaSeries({
    lineColor: "#7aa8ff",
    topColor: "rgba(122, 168, 255, 0.32)",
    bottomColor: "rgba(122, 168, 255, 0.00)",
    lineWidth: 2,
    priceLineVisible: false,
  });
  STATE.ddSeries = chart.addLineSeries({
    color: "rgba(248, 113, 113, 0.55)",
    lineWidth: 1,
    priceScaleId: "drawdown",
    priceLineVisible: false,
  });
  chart.priceScale("drawdown").applyOptions({
    scaleMargins: { top: 0.78, bottom: 0 },
    borderColor: "rgba(255,255,255,0.06)",
  });

  STATE.chart = chart;

  const ro = new ResizeObserver(() => {
    chart.applyOptions({ width: host.clientWidth });
  });
  ro.observe(host);
}

function applyEquity(curve) {
  STATE.equity = curve;
  if (!STATE.chart) return;
  if (!curve || !curve.points) return;

  const now = Date.now();
  const cutoffs = {
    "7d": now - 7 * 86400 * 1000,
    "30d": now - 30 * 86400 * 1000,
    "all": 0,
  };
  const cutoff = cutoffs[STATE.chartRange] ?? cutoffs["30d"];
  const filt = curve.points.filter(p => new Date(p.t).getTime() >= cutoff);

  const eqData = filt.map(p => ({
    time: Math.floor(new Date(p.t).getTime() / 1000),
    value: +p.equity || +p.value || 0,
  }));
  const ddData = filt.map(p => ({
    time: Math.floor(new Date(p.t).getTime() / 1000),
    value: -Math.abs(+p.drawdown_pct || 0),
  }));

  STATE.equitySeries.setData(eqData);
  STATE.ddSeries.setData(ddData);

  const last = filt[filt.length - 1];
  if (last) {
    const tot = +last.cumulative_pnl;
    const roi = +last.roi_pct;
    const el = document.getElementById("totalPnl");
    if (!Number.isNaN(tot)) {
      el.innerHTML = `${fmtUsd(tot, { signed: true })} <span class="roi">${!Number.isNaN(roi) ? fmtPct(roi, { signed: true, alreadyPct: true }) : ""}</span>`;
      el.className = "total display " + gainCls(tot);
    }
  }
  if (curve.max_drawdown_pct != null) {
    document.getElementById("maxDd").textContent = fmtPct(curve.max_drawdown_pct, { signed: false, alreadyPct: true });
  }
}

// ─────────────────────────────────────────────────────────
// log tail
// ─────────────────────────────────────────────────────────
function renderLog(text) {
  if (text == null) return;
  const box = document.getElementById("logBox");
  const atBottom = (box.scrollTop + box.clientHeight + 30) >= box.scrollHeight;
  const lines = text.split("\n").slice(-200);
  box.innerHTML = lines.map(ln => {
    let cls = "log-line";
    if (/\[ERROR\]|\[CRITICAL\]/.test(ln)) cls += " err";
    else if (/\[WARNING\]/.test(ln)) cls += " warn";
    else if (/TRADE (BUY|SELL)/.test(ln)) cls += " trade";
    return `<div class="${cls}">${ln.replace(/[<>&]/g, c => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]))}</div>`;
  }).join("");
  if (atBottom) box.scrollTop = box.scrollHeight;
}

// ─────────────────────────────────────────────────────────
// pause control
// ─────────────────────────────────────────────────────────
async function togglePause() {
  const cur = STATE.strategy?.paused;
  const target = !cur;
  const btn = document.getElementById("pauseBtn");
  btn.disabled = true;
  btn.style.opacity = 0.5;
  try {
    const r = await fetch(ENDPOINTS.control, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paused: target }),
    });
    if (!r.ok) throw new Error("control failed");
    setTimeout(() => { tickStrategy(); }, 400);
  } catch (e) {
    alert("Failed to toggle pause: " + e.message);
  } finally {
    btn.disabled = false;
    btn.style.opacity = 1;
  }
}

// ─────────────────────────────────────────────────────────
// chart range buttons
// ─────────────────────────────────────────────────────────
function bindRangeButtons() {
  document.querySelectorAll(".chart-section .controls button").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".chart-section .controls button").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      STATE.chartRange = btn.dataset.range;
      applyEquity(STATE.equity);
    });
  });
}

// ─────────────────────────────────────────────────────────
// pollers
// ─────────────────────────────────────────────────────────
async function tickStatus() {
  const st = await fetchJson(ENDPOINTS.status);
  if (st) { renderStatus(st); renderCockpit(); }
}
async function tickPortfolio() {
  const p = await fetchJson(ENDPOINTS.portfolio);
  if (p) { renderPositions(p); renderCockpit(); }
}
async function tickCandidates() {
  const c = await fetchJson(ENDPOINTS.candidates);
  if (c) renderWatchlist(c);
}
async function tickTrades() {
  const t = await fetchJson(ENDPOINTS.trades);
  if (t) { renderTrades(t); renderCockpit(); }
}
async function tickEquity() {
  const e = await fetchJson(ENDPOINTS.equity);
  if (e) applyEquity(e);
}
async function tickStrategy() {
  const s = await fetchJson(ENDPOINTS.strategy);
  if (s) { renderStrategy(s); renderCockpit(); }
}
async function tickLog() {
  const t = await fetchText(ENDPOINTS.log);
  if (t) renderLog(t);
}

function startPoll(fn, interval) {
  fn();
  setInterval(fn, interval);
}

// ─────────────────────────────────────────────────────────
// boot
// ─────────────────────────────────────────────────────────
function boot() {
  try { initChart(); } catch (e) { console.error("chart init failed", e); }
  bindRangeButtons();
  const pb = document.getElementById("pauseBtn");
  if (pb) pb.addEventListener("click", togglePause);

  startPoll(tickStatus, POLL.status);
  startPoll(tickPortfolio, POLL.portfolio);
  startPoll(tickCandidates, POLL.candidates);
  startPoll(tickTrades, POLL.trades);
  startPoll(tickEquity, POLL.equity);
  startPoll(tickStrategy, POLL.strategy);
  startPoll(tickLog, POLL.log);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
