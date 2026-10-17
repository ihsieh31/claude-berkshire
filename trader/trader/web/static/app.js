// 波克夏紙上交易：本機儀表板前端。只讀 /api，不另算指標（算法在 trader/report/metrics.py）。
// 所有資料（含模型產出的報告）一律經 esc() 跳脫後才放進頁面。
"use strict";

const TOKEN = document.querySelector('meta[name="dashboard-token"]').content;

// 外觀偏好（只存本機瀏覽器）：主題 auto/light/dark、漲跌色 us（綠漲紅跌）/tw（紅漲綠跌）
const pref = {
  get: (k, d) => { try { return localStorage.getItem(k) || d; } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* 無痕模式 */ } },
};
function applyPrefs() {
  const th = pref.get("theme", "auto"), co = pref.get("colors", "us"), root = document.documentElement;
  if (th === "auto") root.removeAttribute("data-theme"); else root.dataset.theme = th;
  if (co === "tw") root.dataset.colors = "tw"; else root.removeAttribute("data-colors");
  const bt = document.getElementById("t-theme"), bc = document.getElementById("t-colors");
  if (bt) bt.textContent = { auto: "主題：自動", light: "主題：淺色", dark: "主題：深色" }[th];
  if (bc) bc.textContent = co === "tw" ? "紅漲綠跌" : "綠漲紅跌";
}
applyPrefs();
const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");

// ---------------------------------------------------------------- 工具
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isNum = (x) => typeof x === "number" && isFinite(x);
const fmtPct = (x, d = 2, sign = true) => (isNum(x) ? `${sign && x > 0 ? "+" : ""}${(x * 100).toFixed(d)}%` : "—");
const fmtPP = (x) => (isNum(x) ? `${x > 0 ? "+" : ""}${x.toFixed(2)}pp` : "—");
const fmtUSD = (x, sign = false) => (isNum(x) ? `${sign && x > 0 ? "+" : x < 0 ? "−" : ""}$${Math.abs(x).toLocaleString("en-US", { maximumFractionDigits: 0 })}` : "—");
const fmtNum = (x, d = 2) => (isNum(x) ? x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }) : "—");
const fmtTok = (x) => (isNum(x) ? (x >= 1e6 ? `${(x / 1e6).toFixed(2)}M` : x >= 1e3 ? `${(x / 1e3).toFixed(0)}k` : String(x)) : "—");
const localTime = (iso) => { const d = new Date(iso); return isNaN(d) ? esc(iso) : esc(d.toLocaleString("zh-TW", { hour12: false, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })); };
const cls = (x) => (isNum(x) ? (x > 0 ? "up" : x < 0 ? "down" : "") : "muted");

async function api(path) {
  const r = await fetch(path, { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(`${path}：${r.status}`);
  return r.json();
}
async function post(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Dashboard-Token": TOKEN },
    body: JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || `${r.status}`);
  return j;
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.style.display = "block";
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.style.display = "none"), 3200);
}
function confirmDialog(title, body, { okText = "確定", danger = false, note = false } = {}) {
  return new Promise((resolve) => {
    const d = $("#dialog");
    d.innerHTML = `<div class="t">${esc(title)}</div>
      <div class="sub" style="margin-top:10px;font-size:13px;line-height:1.6">${esc(body)}</div>
      ${note ? '<textarea rows="3" placeholder="理由（選填）"></textarea>' : ""}
      <div class="acts"><button class="btn" data-a="no">取消</button>
      <button class="btn ${danger ? "danger" : "primary"}" data-a="ok">${esc(okText)}</button></div>`;
    d.classList.add("on");
    $("#scrim").classList.add("on");
    d.onclick = (e) => {
      const a = e.target.dataset.a;
      if (!a) return;
      const txt = note ? $("textarea", d).value.trim() : null;
      d.classList.remove("on");
      if (!$("#drawer").classList.contains("on")) $("#scrim").classList.remove("on");
      resolve(a === "ok" ? { ok: true, note: txt } : { ok: false });
    };
  });
}

// ---------------------------------------------------------------- markdown（只支援報告用到的語法；先跳脫）
function md(src) {
  const lines = String(src || "").replace(/\r/g, "").split("\n");
  const inline = (s) =>
    esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  let out = "", i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (/^```/.test(l)) {
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out += `<pre><code>${esc(buf.join("\n"))}</code></pre>`;
      continue;
    }
    let m;
    if ((m = l.match(/^(#{1,4})\s+(.*)$/))) { out += `<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`; i++; continue; }
    if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(l)) { out += "<hr>"; i++; continue; }
    if (/^>\s?/.test(l)) {
      const q = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) q.push(lines[i++].replace(/^>\s?/, ""));
      out += `<blockquote>${q.map(inline).join("<br>")}</blockquote>`;
      continue;
    }
    if (/^\s*\|/.test(l)) {
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++]);
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const body = rows.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r));
      out += "<table>" + body.map((r, k) => "<tr>" + cells(r).map((c) => (k === 0 ? `<th>${inline(c)}</th>` : `<td>${inline(c)}</td>`)).join("") + "</tr>").join("") + "</table>";
      continue;
    }
    if (/^\s*([-*]|\d+[.)])\s+/.test(l)) {
      const ordered = /^\s*\d/.test(l);
      const items = [];
      while (i < lines.length && /^\s*([-*]|\d+[.)])\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*([-*]|\d+[.)])\s+/, ""));
      out += `<${ordered ? "ol" : "ul"}>` + items.map((x) => `<li>${inline(x)}</li>`).join("") + `</${ordered ? "ol" : "ul"}>`;
      continue;
    }
    if (!l.trim()) { i++; continue; }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|>|\s*\||\s*([-*]|\d+[.)])\s+)/.test(lines[i])) para.push(lines[i++]);
    out += `<p>${para.map(inline).join("<br>")}</p>`;
  }
  return out;
}

// 代號：可點開走勢彈出視窗
const tkTag = (t) => (t ? `<span class="tk" data-q="${esc(t)}">${esc(t)}</span>` : '<span class="tk muted">—</span>');

// ---------------------------------------------------------------- 個股走勢（彈出視窗；開頁／重新整理時預載一次，只存在本頁記憶體，關掉即消失）
const Q_RANGES = [["1D", "1 天"], ["1W", "1 週"], ["3M", "3 個月"], ["6M", "6 個月"], ["10Y", "10 年"]];
const quotes = { data: new Map(), inflight: new Map(), holdings: new Map(), range: pref.get("q-range", "3M"), open: null };

function prefetchQuotes(tickers) {
  const need = [...new Set(tickers.filter(Boolean))].filter((t) => !quotes.data.has(t) && !quotes.inflight.has(t));
  if (!need.length) return Promise.resolve();
  const batches = [];
  for (let i = 0; i < need.length; i += 40) batches.push(need.slice(i, i + 40));
  return Promise.all(batches.map((b) => {
    const p = api(`/api/charts?tickers=${encodeURIComponent(b.join(","))}`)
      .then((res) => { for (const t of b) if (res[t]) quotes.data.set(t, res[t]); })
      .catch((e) => { for (const t of b) quotes.data.set(t, { _error: e.message }); })
      .finally(() => { for (const t of b) quotes.inflight.delete(t); if (quotes.open && b.includes(quotes.open)) renderQuote(); });
    for (const t of b) quotes.inflight.set(t, p);
    return p;
  }));
}
// 頁面上出現的代號，閒置時順手預載
function prefetchVisible() {
  const run = () => prefetchQuotes([...document.querySelectorAll("[data-q]")].map((e) => e.dataset.q));
  (window.requestIdleCallback || ((f) => setTimeout(f, 200)))(run);
}

function priceChart(el, d, rng) {
  const pts = d.points;
  if (!pts.length) { el.innerHTML = '<div class="empty">此區間沒有成交資料</div>'; return; }
  const W = el.clientWidth || 700, H = el.clientHeight || 300, L = 4, R = 66, T = 14, B = 28;
  const base = d.base, intraday = rng === "1D";
  const vals = pts.map((p) => p[1]).concat(isNum(base) ? [base] : []);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi - lo < 1e-6) { lo -= 0.5; hi += 0.5; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const n = pts.length;
  // 1D 以 09:30–16:00 整個交易時段為橫軸，盤中未走完的部分留白
  const mins = (lab) => { const [h, m] = lab.split(":").map(Number); return h * 60 + m - 570; };
  const pos = intraday ? pts.map((p) => mins(p[0]) / 390) : pts.map((_, i) => (n === 1 ? 0.5 : i / (n - 1)));
  const x = (i) => L + pos[i] * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const last = pts[n - 1][1], up = !isNum(base) || last >= base, col = up ? "var(--up)" : "var(--down)";
  const raw = (hi - lo) / 5, mag = 10 ** Math.floor(Math.log10(raw)), step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((v) => v >= raw);
  const dec = step >= 1 ? (Number.isInteger(step) ? 0 : 1) : Math.min(4, Math.ceil(-Math.log10(step)) + (Number.isInteger(step / mag) ? 0 : 1));
  const grid = [];
  for (let k = Math.ceil(lo / step); k * step <= hi; k++) {
    const v = k * step, near = Math.abs(y(v) - y(last)) < 14;
    grid.push(`<line class="grid-l" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/>${near ? "" : `<text class="axis" x="${W - R + 10}" y="${y(v) + 4}">${v.toFixed(dec)}</text>`}`);
  }
  let xl;
  if (intraday) {
    xl = [["09:30", 0], ["11:00", 90], ["12:30", 180], ["14:00", 270], ["16:00", 390]].map(([t, m], k, a) =>
      `<text class="axis" x="${L + (m / 390) * (W - L - R)}" y="${H - 8}" text-anchor="${k === 0 ? "start" : k === a.length - 1 ? "end" : "middle"}">${t}</text>`);
  } else {
    const lab = (s) => (rng === "10Y" ? s.slice(0, 4) : rng === "1W" ? s.slice(0, 5) : s.slice(5).replace("-", "/"));
    let idx;
    if (rng === "1W") { idx = []; pts.forEach((p, i) => { if (!i || p[0].slice(0, 5) !== pts[i - 1][0].slice(0, 5)) idx.push(i); }); }
    else if (rng === "10Y") { idx = []; pts.forEach((p, i) => { if (i && p[0].slice(0, 4) !== pts[i - 1][0].slice(0, 4) && +p[0].slice(0, 4) % 2 === 0) idx.push(i); }); }
    else { const k = Math.min(n, Math.max(2, Math.floor((W - L - R) / 110))); idx = [...new Set(Array.from({ length: k }, (_, j) => Math.round((j * (n - 1)) / Math.max(k - 1, 1))))]; }
    xl = idx.map((i) => `<text class="axis" x="${x(i)}" y="${H - 8}" text-anchor="${rng === "1W" || rng === "10Y" ? "start" : i === 0 ? "start" : i === n - 1 ? "end" : "middle"}">${esc(lab(pts[i][0]))}</text>`);
  }
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  const area = `${line}L${x(n - 1).toFixed(1)},${H - B}L${x(0).toFixed(1)},${H - B}Z`;
  const gid = `gq${Math.random().toString(36).slice(2, 8)}`;
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:${col};stop-opacity:.16"/><stop offset="1" style="stop-color:${col};stop-opacity:0"/></linearGradient></defs>
    ${grid.join("")}${xl.join("")}
    ${isNum(base) ? `<line class="base" x1="${L}" x2="${W - R}" y1="${y(base)}" y2="${y(base)}"/>` : ""}
    <path d="${area}" fill="url(#${gid})"/><path class="l-acct" d="${line}" style="stroke:${col}"/>
    <circle cx="${x(n - 1)}" cy="${y(last)}" r="3" style="fill:${col}"/>
    <g class="tag" transform="translate(${W - R + 4},${y(last)})"><rect x="0" y="-9" width="${R - 6}" height="18" rx="4" style="fill:${col}"/><text x="6" y="4" style="fill:#fff">${fmtNum(last)}</text></g>
    <g class="hover" visibility="hidden"><line class="cross cx" y1="${T}" y2="${H - B}"/><line class="cross cy" x1="${L}" x2="${W - R}"/>
      <circle r="4" style="fill:${col};stroke:var(--panel);stroke-width:2"/>
      <g class="ylab"><rect x="${W - R + 4}" y="-9" width="${R - 6}" height="18" rx="4" style="fill:var(--ink)"/><text x="${W - R + 10}" y="4" style="fill:var(--panel)" class="axis-h"></text></g></g>
  </svg><div class="tip"></div>`;
  const svg = $("svg", el), hov = $(".hover", el), cx = $(".cx", el), cy = $(".cy", el), dot = $(".hover circle", el), tip = $(".tip", el);
  const yl = $(".ylab", el), ylt = $(".ylab text", el);
  const near = (px) => { let b = 0, bd = Infinity; for (let i = 0; i < n; i++) { const dd = Math.abs(x(i) - px); if (dd < bd) { bd = dd; b = i; } } return b; };
  svg.onmousemove = (e) => {
    const r = svg.getBoundingClientRect();
    const i = near(((e.clientX - r.left) / r.width) * W), v = pts[i][1];
    cx.setAttribute("x1", x(i)); cx.setAttribute("x2", x(i)); cy.setAttribute("y1", y(v)); cy.setAttribute("y2", y(v));
    dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(v));
    yl.setAttribute("transform", `translate(0,${y(v)})`); ylt.textContent = fmtNum(v);
    hov.setAttribute("visibility", "visible");
    const left = (x(i) / W) * r.width, ch = isNum(base) && base ? v / base - 1 : null;
    tip.style.display = "block";
    tip.style.left = `${left + (left > r.width * 0.6 ? -168 : 14)}px`;
    tip.style.top = "8px";
    tip.innerHTML = `<div class="d">${esc(rng === "1D" ? `${d.session || ""} ${pts[i][0]}` : pts[i][0])}</div>
      <div class="row"><span>價格</span><span class="num">${fmtNum(v)}</span></div>
      <div class="row"><span class="muted">${rng === "1D" ? "較昨收" : "較區間起點"}</span><span class="num ${cls(ch)}">${fmtPct(ch)}</span></div>`;
  };
  svg.onmouseleave = () => { tip.style.display = "none"; hov.setAttribute("visibility", "hidden"); };
}

function renderQuote(animate = false) {
  const t = quotes.open, el = $("#quote");
  if (!t) return;
  const all = quotes.data.get(t), rng = quotes.range, d = all && all[rng];
  const h = quotes.holdings.get(t);
  const label = Object.fromEntries(Q_RANGES)[rng];
  const top = d && !d.error && d.points.length
    ? `<div class="q-last">${fmtNum(d.last)}</div><div class="q-chg ${cls(d.change)}">${d.change > 0 ? "+" : d.change < 0 ? "-" : ""}${fmtNum(Math.abs(d.change))} (${fmtPct(d.pct)})<span class="muted">${rng === "1D" ? "較昨收" : label}</span></div>`
    : `<div class="q-last muted">—</div><div class="q-chg muted">&nbsp;</div>`;
  const meta = { "1D": `${esc(d?.session || "")} 正常盤 · 5 分鐘線 · 延遲約 15 分鐘`, "1W": "近 5 個交易日 · 30 分鐘線", "3M": "日線 · 分拆還原",
    "6M": "日線 · 分拆還原", "10Y": "週線 · 分拆還原" }[rng];
  el.innerHTML = `<div class="q-top"><div><div class="q-tk">${esc(t)}</div><div class="q-meta">${meta}</div></div>
      <div class="q-px">${top}</div><button class="btn ghost q-close" aria-label="關閉">✕</button></div>
    <div class="q-bar"><span class="legend"><span><i style="border-color:${d && d.change < 0 ? "var(--down)" : "var(--up)"}"></i>價格</span><span><i class="dash" style="border-color:var(--ink-3)"></i>${rng === "1D" ? "昨收" : "起點"}</span></span>
      <div class="seg" id="q-rng">${Q_RANGES.map(([k, v]) => `<button data-r="${k}" class="${k === rng ? "on" : ""}">${v}</button>`).join("")}</div></div>
    <div class="chart" id="q-chart"></div>
    ${h ? `<div class="q-hold"><div><div class="k">持有</div><div class="v">${esc(h.qty)} 股</div></div><div><div class="k">平均成本</div><div class="v">${fmtNum(h.avg_cost)}</div></div>
      <div><div class="k">市值</div><div class="v">${fmtUSD(h.value)}</div></div><div><div class="k">報酬</div><div class="v ${cls(h.return)}">${fmtPct(h.return, 2)}</div></div></div>` : ""}`;
  const c = $("#q-chart", el);
  if (all && all._error) c.innerHTML = `<div class="empty">讀取失敗：${esc(all._error)}</div>`;
  else if (!d) c.innerHTML = '<div class="empty">載入中…</div>';
  else if (d.error) c.innerHTML = `<div class="empty">讀取失敗：${esc(d.error)}</div>`;
  else priceChart(c, d, rng);
  if (animate) { c.classList.add("fade"); requestAnimationFrame(() => requestAnimationFrame(() => c.classList.remove("fade"))); }
  $(".q-close", el).onclick = closeQuote;
  $("#q-rng", el).onclick = (e) => {
    const r = e.target.dataset.r;
    if (!r || r === quotes.range) return;
    quotes.range = r; pref.set("q-range", r); renderQuote(true);
  };
}
function openQuote(t) {
  quotes.open = t;
  prefetchQuotes([t]);
  renderQuote();
  $("#scrim").classList.add("on");
  requestAnimationFrame(() => $("#quote").classList.add("on"));
}
function closeQuote() {
  if (!quotes.open) return false;
  quotes.open = null;
  $("#quote").classList.remove("on");
  if (!$("#drawer").classList.contains("on")) $("#scrim").classList.remove("on");
  return true;
}

// ---------------------------------------------------------------- 圖表（SVG，看盤風格：Y 軸在右、線尾標值、十字線）
function lineChart(el, points) {
  if (!points.length) { el.innerHTML = '<div class="empty">尚無淨值資料</div>'; return; }
  const W = el.clientWidth || 800, H = el.clientHeight || 340, L = 8, R = 64, T = 16, B = 30;
  const vals = points.flatMap((p) => [p.account, p.spy]).filter(isNum).concat([100]);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi - lo < 1) { lo -= 0.5; hi += 0.5; }
  const pad = (hi - lo) * 0.1; lo -= pad; hi += pad;
  const n = points.length;
  const x = (i) => L + (n === 1 ? (W - L - R) / 2 : (i / (n - 1)) * (W - L - R));
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const path = (k) => points.map((p, i) => (isNum(p[k]) ? `${i && isNum(points[i - 1][k]) ? "L" : "M"}${x(i).toFixed(1)},${y(p[k]).toFixed(1)}` : "")).join("");
  const last = (k) => { for (let i = n - 1; i >= 0; i--) if (isNum(points[i][k])) return [i, points[i][k]]; return null; };
  const la = last("account"), ls = last("spy");
  const acctColor = la && la[1] < 100 ? "var(--down)" : "var(--up)";
  // 刻度：取整齊步距
  const raw = (hi - lo) / 5, mag = 10 ** Math.floor(Math.log10(raw)), step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const grid = [];
  for (let k = Math.ceil(lo / step); k * step <= hi; k++) {
    const v = k * step;
    const near = [la, ls].some((t) => t && Math.abs(y(t[1]) - y(v)) < 14);
    grid.push(`<line class="grid-l" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/>${near ? "" : `<text class="axis" x="${W - R + 10}" y="${y(v) + 4}">${v.toFixed(Number.isInteger(step) ? 0 : 1)}</text>`}`);
  }
  const nx = Math.min(n, Math.max(2, Math.floor((W - L - R) / 120)));
  const xi = [...new Set(Array.from({ length: nx }, (_, k) => Math.round((k * (n - 1)) / Math.max(nx - 1, 1))))];
  const xl = xi.map((i) => `<text class="axis" x="${x(i)}" y="${H - 9}" text-anchor="${i === 0 ? "start" : i === n - 1 ? "end" : "middle"}">${esc(points[i].date.slice(5).replace("-", "/"))}</text>`);
  const tag = (v, fill, ink) => `<g class="tag" transform="translate(${W - R + 4},${y(v)})"><rect x="0" y="-9" width="${R - 6}" height="18" rx="4" style="fill:${fill}"/><text x="6" y="4" style="fill:${ink}">${v.toFixed(2)}</text></g>`;
  const acct = path("account");
  const firstA = points.findIndex((p) => isNum(p.account));
  const area = acct ? `${acct}L${x(la[0])},${H - B}L${x(Math.max(firstA, 0))},${H - B}Z` : "";
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <defs><linearGradient id="gA" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:${acctColor};stop-opacity:.14"/><stop offset="1" style="stop-color:${acctColor};stop-opacity:0"/></linearGradient></defs>
    ${grid.join("")}${xl.join("")}
    <line class="base" x1="${L}" x2="${W - R}" y1="${y(100)}" y2="${y(100)}"/>
    <path d="${area}" fill="url(#gA)"/><path class="l-spy" d="${path("spy")}"/><path class="l-acct" d="${acct}" style="stroke:${acctColor}"/>
    ${la ? `<circle cx="${x(la[0])}" cy="${y(la[1])}" r="3" style="fill:${acctColor}"/>` : ""}
    ${ls ? tag(ls[1], "var(--panel-2)", "var(--ink-2)") : ""}${la ? tag(la[1], acctColor, "#fff") : ""}
    <g class="hover" visibility="hidden"><line class="cross cx" y1="${T}" y2="${H - B}"/><line class="cross cy" x1="${L}" x2="${W - R}"/>
      <circle r="4" style="fill:${acctColor};stroke:var(--panel);stroke-width:2"/>
      <g class="xlab"><rect y="${H - B + 4}" height="18" width="76" rx="4" style="fill:var(--ink)"/><text y="${H - B + 17}" style="fill:var(--panel)" text-anchor="middle" class="axis-h"></text></g></g>
  </svg><div class="tip"></div>`;
  const svg = $("svg", el), hov = $(".hover", el), cx = $(".cx", el), cy = $(".cy", el), dot = $(".hover circle", el), tip = $(".tip", el);
  const xlr = $(".xlab rect", el), xlt = $(".xlab text", el);
  svg.onmousemove = (e) => {
    const r = svg.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    const i = Math.max(0, Math.min(n - 1, Math.round(((px - L) / (W - L - R)) * (n - 1))));
    const p = points[i], v = isNum(p.account) ? p.account : p.spy;
    cx.setAttribute("x1", x(i)); cx.setAttribute("x2", x(i));
    cy.setAttribute("y1", y(v)); cy.setAttribute("y2", y(v));
    dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(v));
    const lx = Math.max(L + 38, Math.min(W - R - 38, x(i)));
    xlr.setAttribute("x", lx - 38); xlt.setAttribute("x", lx); xlt.textContent = p.date;
    hov.setAttribute("visibility", "visible");
    const left = (x(i) / W) * r.width;
    tip.style.display = "block";
    tip.style.left = `${left + (left > r.width * 0.6 ? -172 : 16)}px`;
    tip.style.top = "16px";
    const ex = isNum(p.account) && isNum(p.spy) ? p.account - p.spy : null;
    tip.innerHTML = `<div class="d">${esc(p.date)}</div>
      <div class="row"><span>帳戶</span><span class="num ${cls(p.account - 100)}">${fmtNum(p.account)}</span></div>
      <div class="row"><span class="muted">SPY</span><span class="num">${fmtNum(p.spy)}</span></div>
      <div class="row"><span class="muted">差距</span><span class="num ${cls(ex)}">${isNum(ex) ? (ex > 0 ? "+" : "") + ex.toFixed(2) : "—"}</span></div>`;
  };
  svg.onmouseleave = () => { tip.style.display = "none"; hov.setAttribute("visibility", "hidden"); };
}

// 配置條用灰階（顏色只留給漲跌）
const GRAYS = ["var(--ink)", "color-mix(in srgb, var(--ink) 78%, var(--panel))", "color-mix(in srgb, var(--ink) 60%, var(--panel))",
  "color-mix(in srgb, var(--ink) 44%, var(--panel))", "color-mix(in srgb, var(--ink) 32%, var(--panel))", "color-mix(in srgb, var(--ink) 22%, var(--panel))"];

const pageHead = (title, sub = "", tools = "") =>
  `<div class="page-head"><h1>${title}</h1>${sub ? `<div class="sub">${sub}</div>` : ""}${tools ? `<div class="tools">${tools}</div>` : ""}</div>`;
const panel = (title, body, right = "", flush = true) =>
  `<section class="panel">${title ? `<div class="panel-head"><h2>${title}</h2>${right ? `<div class="right">${right}</div>` : ""}</div>` : ""}<div class="${flush ? "" : "panel-body"}">${body}</div></section>`;
const tbl = (head, rows, empty, ncol) =>
  `<div class="tbl-wrap"><table><thead><tr>${head}</tr></thead><tbody>${rows || `<tr><td colspan="${ncol}" class="empty">${empty}</td></tr>`}</tbody></table></div>`;

// ---------------------------------------------------------------- 頁面
const pages = {};

pages.overview = async () => {
  const [s, holds] = await Promise.all([api("/api/summary"), api("/api/holdings")]);
  const a = s.approvals;
  const banner = a.n_pending
    ? `<div class="banner"><div><div class="t">${esc(a.n_pending)} 檔等待強模型分析</div><div class="s">${esc(a.label)}</div></div>
       <a class="btn primary" href="#approvals">檢視並同意</a></div>` : "";
  if (s.empty) {
    view.innerHTML = `${banner}${pageHead("總覽")}<div class="panel"><div class="empty">尚無淨值資料：第一次 <code>trader daily</code> 之後就會出現。</div></div>`;
    return;
  }
  const kpi = (k, v, sub = "", c = "", hero = false) => `<div class="kpi"><div class="k">${k}</div><div class="v ${hero ? "hero" : ""} ${c}">${v}</div><div class="s">${sub}</div></div>`;
  const hs = holds.slice().sort((p, q) => q.value - p.value);
  const maxW = Math.max(...hs.map((h) => h.weight || 0), 0.0001);
  const alloc = hs.map((h, i) => ({ k: h.ticker, v: h.weight || 0, c: GRAYS[Math.min(i, GRAYS.length - 1)] }));
  alloc.push({ k: "現金", v: s.cash_ratio || 0, c: "var(--line-2)" });
  const holdRows = hs.map((h) => `<tr><td>${tkTag(h.ticker)} <span class="muted" style="font-size:11.5px">${esc(h.grade || "")}</span></td>
    <td class="n">${fmtNum(h.price)}${h.price_missing ? ' <span class="pill warn">估</span>' : ""}</td>
    <td class="n muted">${fmtNum(h.avg_cost)}</td><td class="n">${fmtUSD(h.value)}</td>
    <td class="n"><span class="wbar"><span style="width:${((h.weight || 0) / maxW) * 100}%"></span></span>${fmtPct(h.weight, 1, false)}</td>
    <td class="n ${cls(h.return)}">${fmtPct(h.return, 2)}</td></tr>`).join("");
  const todo = [];
  if (a.n_pending) todo.push(`<div class="row"><a href="#approvals">待同意分析</a><span class="v">${esc(a.n_pending)} 檔</span></div>`);
  if (s.open_anomalies) todo.push(`<div class="row"><a href="#anomalies">未結異常</a><span class="v">${esc(s.open_anomalies)} 筆</span></div>`);
  if (s.missing_close.length) todo.push(`<div class="row"><span>缺收盤（以成本估）</span><span class="v">${esc(s.missing_close.join("、"))}</span></div>`);
  view.innerHTML = `${banner}
  ${pageHead("總覽", `${esc(s.first_date)} 起 · 資料至 ${esc(s.as_of)}`)}
  <div class="kpis">
    ${kpi("淨值", fmtUSD(s.nav), `投入 ${fmtUSD(s.deposits)}`, "", true)}
    ${kpi("帳戶報酬", fmtPct(s.return), "時間加權", cls(s.return))}
    ${kpi("SPY 同期", fmtPct(s.spy_return), "含股利", cls(s.spy_return))}
    ${kpi("超額", fmtPP(s.excess_pp), "帳戶 − SPY", cls(s.excess_pp))}
    ${kpi("現金比", fmtPct(s.cash_ratio, 1, false), `${esc(s.n_positions ?? hs.length)} 檔持股`)}
  </div>
  ${panel(`<span class="legend"><span><i style="border-color:${s.return < 0 ? "var(--down)" : "var(--up)"}"></i>帳戶</span><span><i class="dash" style="border-color:var(--spy)"></i>SPY</span><span class="muted">起點 = 100</span></span>`,
    '<div class="chart" id="chart"></div>',
    `<div class="seg" id="rng">${["1M", "3M", "YTD", "ALL"].map((r) => `<button data-r="${r}" class="${r === state.range ? "on" : ""}">${r === "ALL" ? "全部" : r}</button>`).join("")}</div>`)}
  <div class="grid g-main" style="margin-top:16px">
    ${panel("持股", tbl('<th>代號</th><th class="n">現價</th><th class="n">成本</th><th class="n">市值</th><th class="n">權重</th><th class="n">報酬</th>', holdRows, "尚無持股", 6),
      '<a class="btn ghost sm" href="#holdings">全部 →</a>')}
    <div class="stack">
      ${panel("收益分解", `<div class="list">
        <div class="row total"><span>總收益</span><span class="v ${cls(s.pnl.total)}">${fmtUSD(s.pnl.total, true)}</span></div>
        <div class="row"><span>未實現</span><span class="v ${cls(s.pnl.unrealized)}">${fmtUSD(s.pnl.unrealized, true)}</span></div>
        <div class="row"><span>股利（含應收）</span><span class="v ${cls(s.pnl.dividends)}">${fmtUSD(s.pnl.dividends, true)}</span></div>
        <div class="row"><span>已實現及其他<div class="sub">賣出損益、手續費、調帳</div></span><span class="v ${cls(s.pnl.realized_other)}">${fmtUSD(s.pnl.realized_other, true)}</span></div></div>`, "", false)}
      ${panel("配置", `<div class="alloc">${alloc.map((p) => `<span style="width:${(p.v * 100).toFixed(2)}%;background:${p.c}" title="${esc(p.k)}"></span>`).join("")}</div>
        <div class="list">${alloc.map((p) => `<div class="row"><span><span class="sw" style="background:${p.c}"></span>${esc(p.k)}</span><span class="v">${fmtPct(p.v, 1, false)}</span></div>`).join("")}</div>`, `<span class="sub tnum">${fmtUSD(s.cash)} 現金</span>`, false)}
      ${todo.length ? panel("待辦", `<div class="list">${todo.join("")}</div>`, "", false) : ""}
    </div>
  </div>`;
  const draw = async () => lineChart($("#chart"), (await api(`/api/nav?range=${state.range}`)).points);
  $("#rng").onclick = (e) => {
    const r = e.target.dataset.r;
    if (!r) return;
    state.range = r;
    [...$("#rng").children].forEach((b) => b.classList.toggle("on", b.dataset.r === r));
    draw();
  };
  await draw();
};

pages.holdings = async () => {
  const hs = await api("/api/holdings");
  const bp = (b) => (b && typeof b === "object" ? esc(`剩 ${b.tranches_left ?? "?"} 批`) : "—");
  const total = hs.reduce((a, h) => a + (h.value || 0), 0);
  const rows = hs.map((h) => `<tr><td>${tkTag(h.ticker)} <span class="muted" style="font-size:11.5px">${esc(h.grade || "")}</span></td>
      <td class="n">${esc(h.qty)}</td><td class="n muted">${fmtNum(h.avg_cost)}</td><td class="n">${fmtNum(h.price)}${h.price_missing ? ' <span class="pill warn">估</span>' : ""}</td>
      <td class="n">${fmtUSD(h.value)}</td><td class="n">${fmtPct(h.weight, 1, false)}</td><td class="n ${cls(h.return)}">${fmtPct(h.return, 2)}</td>
      <td class="n">${fmtNum(h.auto_buy_cap)}</td><td class="n">${fmtNum(h.sell_review_above)}</td><td class="n muted">${fmtNum(h.absurd_price_line)}</td>
      <td>${bp(h.build_plan)}${isNum(h.target_weight) ? `<div class="sub">目標 ${fmtPct(h.target_weight, 1, false)}</div>` : ""}</td>
      <td class="wrap">${esc(h.thesis_one_liner || "—")}</td>
      <td class="num">${esc(h.next_kill_check || "—")}<div class="sub">完整 ${esc(h.next_full_review_due || "—")}</div></td>
      <td>${h.paused ? '<span class="pill bad">暫停交易</span> ' : ""}${h.add_blocked ? `<span class="pill warn" title="${esc(h.add_blocked)}">停止加碼</span>` : ""}${!h.paused && !h.add_blocked ? '<span class="pill">正常</span>' : ""}</td></tr>`).join("");
  view.innerHTML = pageHead("持股", `${hs.length} 檔 · 市值 ${fmtUSD(total)}`, '<a class="btn sm" href="/api/export/holdings.csv">匯出 CSV</a>') +
    panel("", tbl(`<th>代號</th><th class="n">股數</th><th class="n">成本</th><th class="n">現價</th><th class="n">市值</th><th class="n">權重</th><th class="n">報酬</th>
    <th class="n">買價上限</th><th class="n">賣出檢視線</th><th class="n">荒謬級線</th><th>建倉</th><th>論點</th><th>下次檢查</th><th>狀態</th>`, rows, "尚無持股", 14));
};

pages.trades = async () => {
  const q = new URLSearchParams();
  if (state.tf.ticker) q.set("ticker", state.tf.ticker);
  if (state.tf.start) q.set("start", state.tf.start);
  if (state.tf.end) q.set("end", state.tf.end);
  const t = await api(`/api/trades?${q}`);
  const TABS = { orders: "委託", fills: "成交", cash: "現金流水", corporate_actions: "公司行動" };
  const cols = {
    orders: [["created_date", "日期"], ["ticker", "代號"], ["side", "方向"], ["qty", "股數", 1], ["limit_price", "限價", 2], ["status", "狀態"], ["cancel_reason", "說明"]],
    fills: [["fill_date", "日期"], ["ticker", "代號"], ["side", "方向"], ["qty", "股數", 1], ["price", "成交價", 2], ["commission", "手續費", 2]],
    cash: [["date", "日期"], ["kind", "類型"], ["amount", "金額", 2], ["ref", "參照"], ["note", "說明"]],
    corporate_actions: [["effective_date", "生效日"], ["ticker", "代號"], ["action_type", "類型"], ["ratio_new_per_old", "比例", 4], ["cash_per_share", "每股現金", 4], ["new_ticker", "新代號"], ["status", "狀態"]],
  };
  const sideCls = (v) => (v === "BUY" ? "up" : v === "SELL" ? "down" : "");
  const cell = (r, c) => {
    const v = r[c[0]];
    if (c[2]) return `<td class="n ${c[0] === "amount" ? cls(v) : ""}">${c[2] === 1 ? esc(v) : fmtNum(v, c[2])}</td>`;
    if (c[0] === "ticker") return `<td>${tkTag(v)}</td>`;
    if (c[0] === "side") return `<td class="${sideCls(v)}" style="font-weight:500">${esc(v ?? "")}</td>`;
    if (/date$/.test(c[0])) return `<td class="num muted">${esc(v ?? "")}</td>`;
    return `<td class="${c[0] === "note" || c[0] === "cancel_reason" ? "wrap" : ""}">${esc(v ?? "")}</td>`;
  };
  const rows = (t[state.tab] || []).map((r) => `<tr>${cols[state.tab].map((c) => cell(r, c)).join("")}</tr>`).join("");
  view.innerHTML = pageHead("交易紀錄", "", `<input id="f-t" placeholder="代號" value="${esc(state.tf.ticker || "")}" size="7">
    <input id="f-s" type="date" value="${esc(state.tf.start || "")}"><input id="f-e" type="date" value="${esc(state.tf.end || "")}">
    <button class="btn sm" id="f-go">篩選</button><a class="btn sm" href="/api/export/${state.tab}.csv">匯出 CSV</a>`) +
    `<section class="panel"><div class="tabs">${Object.entries(TABS).map(([k, v]) => `<button data-k="${k}" class="${k === state.tab ? "on" : ""}">${v}<span class="c">${(t[k] || []).length}</span></button>`).join("")}</div>
    ${tbl(cols[state.tab].map((c) => `<th class="${c[2] ? "n" : ""}">${c[1]}</th>`).join(""), rows, "沒有資料", cols[state.tab].length)}</section>`;
  $(".tabs").onclick = (e) => { const b = e.target.closest("button"); if (b) { state.tab = b.dataset.k; pages.trades(); } };
  $("#f-go").onclick = () => { state.tf = { ticker: $("#f-t").value.trim().toUpperCase(), start: $("#f-s").value, end: $("#f-e").value }; pages.trades(); };
};

const ACTION_PILL = { BUY: "ok", ADD: "ok", HOLD: "", WATCH: "", TRIM: "warn", SELL_ALL: "bad", NO_ACTION: "" };

pages.analyses = async () => {
  const q = state.af ? `?ticker=${encodeURIComponent(state.af)}` : "";
  const [rows, usage] = await Promise.all([api(`/api/analyses${q}`), api("/api/usage")]);
  const dr = rows.map((r) => `<tr class="click" data-id="${esc(r.decision_id)}"><td class="num muted">${esc(r.date)}</td><td>${tkTag(r.ticker)}</td>
    <td><span class="pill ${ACTION_PILL[r.action] ?? ""}">${esc(r.action || "—")}</span></td><td class="muted">${esc(r.review_type || "完整分析")}</td>
    <td class="num">${esc(r.stopped_at_gate || "—")}</td><td>${esc(r.grade || "—")}</td><td class="wrap">${esc(r.reason || r.reason_code || "")}</td></tr>`).join("");
  const ur = usage.map((u) => `<tr><td class="num">${esc(u.month)}</td><td>${esc({ strong: "強", medium: "中", weak: "弱" }[u.tier] || u.tier)}</td><td class="n">${esc(u.calls)}</td>
    <td class="n">${fmtTok(u.input_tokens)}</td><td class="n">${fmtTok(u.output_tokens)}</td><td class="n">${fmtNum(u.latency_s / 60, 1)} 分</td></tr>`).join("");
  view.innerHTML = pageHead("分析紀錄", `${rows.length} 筆決策 · 點一列看報告`, `<input id="a-t" placeholder="代號" value="${esc(state.af || "")}" size="7"><button class="btn sm" id="a-go">篩選</button>
    <a class="btn sm" href="/api/export/analyses.csv">匯出 CSV</a>`) +
    `<div class="stack">${panel("決策日誌", tbl("<th>日期</th><th>代號</th><th>動作</th><th>類型</th><th>停在</th><th>等級</th><th>理由</th>", dr, "尚無決策", 7))}
    ${panel("模型用量（每月）", tbl('<th>月份</th><th>等級</th><th class="n">呼叫</th><th class="n">輸入</th><th class="n">輸出</th><th class="n">耗時</th>', ur, "尚無呼叫紀錄", 6))}</div>`;
  $("#a-go").onclick = () => { state.af = $("#a-t").value.trim().toUpperCase(); pages.analyses(); };
  view.querySelectorAll("tr.click").forEach((tr) => (tr.onclick = () => { history.replaceState(null, "", `#analyses/${encodeURIComponent(tr.dataset.id)}`); openReport(tr.dataset.id); }));
};

async function openReport(id) {
  const d = $("#drawer");
  d.innerHTML = '<div class="empty">載入中…</div>';
  d.classList.add("on");
  $("#scrim").classList.add("on");
  const r = await api(`/api/analyses/${encodeURIComponent(id)}`);
  const card = r.signal_card || {};
  const keys = Object.keys(card).filter((k) => card[k] !== null && card[k] !== "" && !(Array.isArray(card[k]) && !card[k].length));
  d.innerHTML = `<button class="btn sm close">關閉</button>
    <div class="sub mono">${esc(r.decision_id)}</div><h1>${esc(r.ticker)} <span class="muted" style="font-size:22px">· ${esc(r.log.action || "")}</span></h1>
    ${card.one_liner ? `<p style="font:16px/1.6 var(--serif);color:var(--ink-2);margin:0 0 24px">${esc(card.one_liner)}</p>` : ""}
    ${keys.length ? `<details class="card" style="margin-bottom:28px"><summary>訊號卡（${keys.length} 欄）</summary><div class="card-kv" style="margin-top:16px">${keys.map((k) => `<div>${esc(k)}</div><div>${esc(typeof card[k] === "object" ? JSON.stringify(card[k]) : card[k])}</div>`).join("")}</div></details>` : ""}
    ${r.report_md ? `<div class="sub mono">${esc(r.report_file)}</div><div class="md">${md(r.report_md)}</div>` : '<div class="empty">此決策沒有報告檔（例如停在早期關卡、或為觸發檢查）。</div>'}`;
  $(".close", d).onclick = closeDrawer;
}
function closeDrawer() {
  $("#drawer").classList.remove("on");
  $("#scrim").classList.remove("on");
  if (location.hash.startsWith("#analyses/")) history.replaceState(null, "", "#analyses");
}

pages.approvals = async () => {
  const a = await api("/api/approvals");
  const s = a.summary;
  const items = a.items;
  const st = { pending: ["待同意", "warn"], deferred: ["延後", ""], approved: [a.running ? "執行中" : "已同意，等待執行", "ok"] };
  // 執行中時每 10 秒自動更新，跑完的項目會自動從清單消失（結果在「分析」頁）
  clearTimeout(pages.approvals._t);
  if (a.running) pages.approvals._t = setTimeout(() => { if ((location.hash || "") === "#approvals") { pages.approvals(); refreshBadge(); } }, 10000);
  const rows = items.map((x) => `<tr><td>${x.status === "approved" ? "" : `<input type="checkbox" data-id="${esc(x.approval_id)}" data-st="${esc(x.status)}">`}</td>
    <td>${tkTag(x.ticker)}${x.holding ? ' <span class="pill">持股</span>' : ""}</td>
    <td><span class="pill ${st[x.status]?.[1] ?? ""}">${esc(st[x.status]?.[0] || x.status)}</span></td><td class="num">${esc(x.job_type)}</td>
    <td class="num muted">${esc(x.asof)}</td><td class="wrap">${esc(x.reason || "")}</td><td class="num muted">${localTime(x.created_at)}</td></tr>`).join("");
  const kpi = (k, v, sub = "") => `<div class="kpi"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${sub}</div></div>`;
  view.innerHTML = pageHead("待同意", "強模型分析需你同意後才執行", a.running ? '<span class="pill ok">強模型分析執行中</span>' : "") +
    `<div class="kpis" style="grid-template-columns:repeat(4,1fr)">
      ${kpi("待同意", `${esc(s.n_pending)} 檔`)}${kpi("預估輸入", fmtTok(s.est_input_tokens), `輸出 ${fmtTok(s.est_output_tokens)}`)}
      ${kpi("預估時間", `${fmtNum(s.est_minutes, 0)} 分`)}${kpi("每檔估算依據", esc(s.per_ticker.basis), s.per_ticker.samples ? `${esc(s.per_ticker.samples)} 次樣本` : "尚無樣本")}
    </div><div class="sub" style="margin:-4px 0 20px">${esc(s.note)}</div>` +
    panel("清單", tbl('<th style="width:36px"></th><th>代號</th><th>狀態</th><th>工作</th><th>資料日</th><th>觸發原因</th><th>建立</th>', rows, "沒有等待中的項目", 7),
      `<button class="btn ghost sm" id="df-sel" disabled>延後所選</button><button class="btn ghost sm danger" id="rj-sel" disabled>拒絕所選</button>
       <button class="btn sm" id="ap-sel" disabled>同意所選</button><button class="btn primary sm" id="ap-all" ${s.n_pending ? "" : "disabled"}>全部同意（${esc(s.n_pending)}）</button>`);
  const sel = () => [...view.querySelectorAll("input[type=checkbox]:checked")];
  const refreshBtns = () => {
    const c = sel();
    $("#ap-sel").disabled = $("#rj-sel").disabled = !c.length;
    $("#df-sel").disabled = !c.length || c.some((i) => i.dataset.st !== "pending");
  };
  view.querySelectorAll("input[type=checkbox]").forEach((i) => (i.onchange = refreshBtns));
  const ids = () => sel().map((i) => Number(i.dataset.id));
  const act = async (fn) => { try { await fn(); } catch (e) { toast(`失敗：${e.message}`); } await pages.approvals(); refreshBadge(); };
  $("#ap-all").onclick = async () => {
    const c = await confirmDialog(`同意以強模型分析 ${s.n_pending} 檔？`, `${s.label || ""}。延後的項目不包含在內。請先確認 Claude 額度足夠。`, { okText: "同意並執行" });
    if (c.ok) act(async () => { const r = await post("/api/approvals/approve", { all: true, via: "web" }); toast(r.started ? "已同意，背景執行中" : r.note || "已同意"); });
  };
  $("#ap-sel").onclick = async () => {
    const c = await confirmDialog(`同意所選 ${ids().length} 檔？`, "同意後立即在背景執行（含之後可能的 RED_TEAM）。", { okText: "同意並執行" });
    if (c.ok) act(async () => { const r = await post("/api/approvals/approve", { ids: ids(), via: "web" }); toast(r.started ? "已同意，背景執行中" : r.note || "已同意"); });
  };
  $("#df-sel").onclick = () => act(async () => { await post("/api/approvals/defer", { ids: ids(), via: "web" }); toast("已延後"); });
  $("#rj-sel").onclick = async () => {
    const c = await confirmDialog(`拒絕所選 ${ids().length} 檔？`, "該檔記為 NO_ACTION／MISSING_DATA，下次觸發時再排。", { okText: "拒絕", danger: true, note: true });
    if (c.ok) act(async () => { await post("/api/approvals/reject", { ids: ids(), via: "web", note: c.note || null }); toast("已拒絕"); });
  };
};

pages.anomalies = async () => {
  const a = await api("/api/anomalies");
  const detail = (d) => { try { const j = JSON.parse(d); return esc(Object.entries(j).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join("；")); } catch { return esc(d); } };
  const r1 = a.open.map((x) => `<tr><td class="num muted">${esc(x.anomaly_id)}</td><td>${tkTag(x.ticker)}</td><td>${esc(x.kind)}</td>
    <td>${x.pause_trading ? '<span class="pill bad">暫停</span>' : '<span class="pill">否</span>'}</td><td class="wrap">${detail(x.detail)}</td><td class="num muted">${esc(String(x.created_at).slice(0, 10))}</td></tr>`).join("");
  const r2 = a.pending_corp_actions.map((x) => `<tr><td class="num muted">${esc(x.effective_date)}</td><td>${tkTag(x.ticker)}</td><td>${esc(x.action_type)}</td>
    <td class="n">${fmtNum(x.ratio_new_per_old, 4)}</td><td class="n">${fmtNum(x.cash_per_share, 4)}</td><td>${esc(x.new_ticker || "—")}</td><td><span class="pill warn">${esc(x.status)}</span></td></tr>`).join("");
  view.innerHTML = pageHead("異常", `${a.open.length} 筆未結 · ${a.pending_corp_actions.length} 筆公司行動待處理`) +
    `<div class="stack">${panel("未結異常", tbl("<th>#</th><th>代號</th><th>類型</th><th>暫停交易</th><th>內容</th><th>建立</th>", r1, "沒有未結異常", 6) +
      '<div class="sub" style="padding:14px 20px;border-top:1px solid var(--line)">處理方式：<code>trader ledger adjust … --note …</code>，完成後 <code>--resolve ID</code> 或 <code>--unpause 代號</code>。</div>')}
    ${panel("待人工處理的公司行動", tbl('<th>生效日</th><th>代號</th><th>類型</th><th class="n">比例</th><th class="n">每股現金</th><th>新代號</th><th>狀態</th>', r2, "沒有", 7))}</div>`;
};

// ---------------------------------------------------------------- 路由
const state = { range: "3M", tab: "orders", tf: {}, af: "" };

async function refreshBadge() {
  try {
    const s = await api("/api/summary");
    const n = s.approvals.n_pending;
    const a = document.querySelector('nav a[href="#approvals"]');
    a.innerHTML = `待同意${n ? `<span class="dot">${esc(n)}</span>` : ""}`;
    const b = document.querySelector('nav a[href="#anomalies"]');
    b.innerHTML = `異常${s.open_anomalies ? `<span class="dot warn">${esc(s.open_anomalies)}</span>` : ""}`;
    $("#asof").textContent = s.as_of ? `收盤 ${s.as_of}` : "";
  } catch { /* 伺服器暫時不可用 */ }
}

async function route() {
  const [page, arg] = (location.hash || "#overview").slice(1).split("/");
  const fn = pages[page] || pages.overview;
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("on", a.getAttribute("href") === `#${page}`));
  try {
    await fn();
    prefetchVisible();
    if (page === "analyses" && arg) await openReport(decodeURIComponent(arg));
  } catch (e) {
    view.innerHTML = `<div class="panel"><div class="empty">讀取失敗：${esc(e.message)}</div></div>`;
  }
}
window.addEventListener("hashchange", route);
$("#scrim").onclick = () => { if ($("#dialog").classList.contains("on")) return; if (!closeQuote()) closeDrawer(); };
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#dialog").classList.contains("on") && !closeQuote()) closeDrawer(); });
// 代號點擊（捕獲階段，避免同時觸發整列的點擊）；滑過時先預載
document.addEventListener("click", (e) => {
  const q = e.target.closest("[data-q]");
  if (!q) return;
  e.preventDefault(); e.stopPropagation();
  openQuote(q.dataset.q);
}, true);
document.addEventListener("mouseover", (e) => { const q = e.target.closest && e.target.closest("[data-q]"); if (q) prefetchQuotes([q.dataset.q]); });
// 開頁即預載：持股與待同意標的的五種走勢
(async () => {
  try {
    const [hs, ap] = await Promise.all([api("/api/holdings"), api("/api/approvals")]);
    hs.forEach((h) => quotes.holdings.set(h.ticker, h));
    await prefetchQuotes(hs.map((h) => h.ticker).concat(ap.items.map((x) => x.ticker)));
  } catch { /* 走勢預載失敗不影響其他頁面 */ }
})();
$("#t-theme").onclick = () => { const o = ["auto", "light", "dark"]; pref.set("theme", o[(o.indexOf(pref.get("theme", "auto")) + 1) % 3]); applyPrefs(); };
$("#t-colors").onclick = () => { pref.set("colors", pref.get("colors", "us") === "tw" ? "us" : "tw"); applyPrefs(); if ((location.hash || "#overview") === "#overview") route(); };
let rt;
window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { if (quotes.open) renderQuote(); if ((location.hash || "#overview") === "#overview") route(); }, 200); });
setInterval(refreshBadge, 60000);
applyPrefs();
refreshBadge();
route();
