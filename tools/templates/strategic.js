/* Executive review runtime.
 *
 * Everything on the page is derived from four cubes and the narrative the
 * agent wrote. Two rules hold throughout:
 *
 *  1. Year-over-year is like-for-like. The newest fiscal year in this model is
 *     short by two weeks, so a comparison only counts the fiscal months the
 *     prior year also has. Raw totals are shown as totals and never as growth.
 *  2. Filters are limited to fiscal year and channel, both of which are
 *     order-level attributes, so distinct order counts stay exact. Product
 *     level appears as a breakdown, never as a filter, because an order can
 *     span categories and the order count would quietly stop being a count.
 */
(function () {
  const D = window.DASH;
  const N = D.narrative || {};
  const CARDS = N.cards || {};
  const state = {
    years: new Set(D.defaultYears || D.years),
    channels: new Set(D.channels)
  };

  const money = (v, dp) => (v === null || v === undefined || Number.isNaN(v)) ? "-"
    : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: dp || 0, maximumFractionDigits: dp || 0 });
  const num = (v, dp) => (v === null || v === undefined || Number.isNaN(v)) ? "-"
    : Number(v).toLocaleString(undefined, { minimumFractionDigits: dp || 0, maximumFractionDigits: dp || 0 });
  const pct = (v, dp) => (v === null || v === undefined || Number.isNaN(v)) ? "-"
    : Number(v).toFixed(dp === undefined ? 1 : dp) + "%";
  const signed = (v, dp) => (v === null || v === undefined || Number.isNaN(v)) ? "-"
    : (v > 0 ? "+" : "") + Number(v).toFixed(dp === undefined ? 1 : dp) + "%";
  const compact = v => {
    const a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(1) + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
    if (a >= 1e3) return (v / 1e3).toFixed(0) + "K";
    return String(Math.round(v));
  };
  const monthLabel = s => {
    const d = new Date(s + "T00:00:00Z");
    return d.toLocaleString("en-US", { month: "short", timeZone: "UTC" }) + " " +
      String(d.getUTCFullYear()).slice(2);
  };

  const el = (tag, cls, parent, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    if (parent) parent.appendChild(e);
    return e;
  };
  const NS = "http://www.w3.org/2000/svg";
  const sv = (tag, attrs, parent) => {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) if (attrs[k] !== null && attrs[k] !== undefined) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  };
  const tip = (node, text) => { sv("title", {}, node).textContent = text; return node; };

  function ticks(max, count) {
    if (!(max > 0)) return { max: 1, list: [0, 1] };
    const raw = max / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const n = raw / mag;
    const step = (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
    const top = Math.ceil(max / step) * step;
    const list = [];
    for (let t = 0; t <= top + step / 2; t += step) list.push(t);
    return { max: top, list };
  }

  // A signed scale: monthly gross margin goes negative in this model and a
  // zero-based axis would silently clip those months off the chart.
  function ticksSigned(min, max, count) {
    if (min >= 0) {
      const t = ticks(max, count);
      return { min: 0, max: t.max, list: t.list };
    }
    const span = ticks(Math.max(Math.abs(min), Math.abs(max)), Math.max(2, count - 1));
    const step = span.list.length > 1 ? span.list[1] - span.list[0] : span.max;
    const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
    const list = [];
    for (let t = lo; t <= hi + step / 2; t += step) list.push(t);
    return { min: lo, max: hi, list };
  }

  /* ----------------------------------------------------------- selections */

  const inSel = r => state.years.has(r.fy) && (!r.ch || state.channels.has(r.ch));
  const months = () => D.cubes.months.filter(inSel);

  function roll(rows) {
    const t = { rev: 0, cost: 0, qty: 0, orders: 0, lines: 0 };
    rows.forEach(r => {
      t.rev += r.rev || 0; t.cost += r.cost || 0; t.qty += r.qty || 0;
      t.orders += r.orders || 0; t.lines += r.lines || 0;
    });
    t.gp = t.rev - t.cost;
    t.margin = t.rev ? (t.gp / t.rev) * 100 : null;
    t.aov = t.orders ? t.rev / t.orders : null;
    return t;
  }

  // Like-for-like: for every selected year that has a predecessor in the data,
  // compare only the fiscal months the selected year actually contains.
  function comparison() {
    const years = D.years;
    const sel = [...state.years].sort();
    const pairs = sel.map(y => [y, years[years.indexOf(y) - 1]]).filter(p => p[1]);
    if (!pairs.length) return null;
    const fmOf = {};
    D.cubes.months.forEach(r => {
      (fmOf[r.fy] = fmOf[r.fy] || new Set()).add(r.fm);
    });
    const cur = [], prev = [];
    pairs.forEach(([y, p]) => {
      const want = fmOf[y] || new Set();
      D.cubes.months.forEach(r => {
        if (r.ch && !state.channels.has(r.ch)) return;
        if (r.fy === y) cur.push(r);
        else if (r.fy === p && want.has(r.fm)) prev.push(r);
      });
    });
    return {
      cur: roll(cur), prev: roll(prev),
      label: pairs.map(p => p[1]).join(", "),
      excluded: sel.filter(y => !pairs.some(p => p[0] === y))
    };
  }

  const delta = (a, b) => (b ? ((a - b) / Math.abs(b)) * 100 : null);

  /* -------------------------------------------------------------- sections */

  function masthead(root) {
    const m = el("div", "masthead", root);
    el("div", "eyebrow", m,
      `${D.model.name} · rebuilt from the Power BI model · ${num(D.model.rows)} rows across ${D.model.tables} tables`);
    el("h1", null, m, N.headline || "Executive review");
    if (N.standfirst) el("p", "standfirst", m, N.standfirst);
  }

  function controls(root) {
    const c = el("div", "controls", root);
    const yg = el("div", "ctl-group", c);
    el("span", "ctl-label", yg, "Fiscal year");
    D.years.forEach(y => {
      const b = el("button", "chip" + (state.years.has(y) ? " on" : ""), yg, y);
      b.onclick = () => {
        if (state.years.has(y)) state.years.delete(y); else state.years.add(y);
        if (!state.years.size) state.years.add(y);
        render();
      };
    });
    if (D.channels.length) {
      const cg = el("div", "ctl-group", c);
      el("span", "ctl-label", cg, "Channel");
      D.channels.forEach(ch => {
        const b = el("button", "chip" + (state.channels.has(ch) ? " on" : ""), cg, ch);
        b.onclick = () => {
          if (state.channels.has(ch)) state.channels.delete(ch); else state.channels.add(ch);
          if (!state.channels.size) state.channels.add(ch);
          render();
        };
      });
    }
    el("div", "ctl-note", c,
      "Filters stay at year and channel so order counts and average order value remain exact. Product detail is shown as a breakdown, not a filter.");
  }

  function readFirst(root) {
    if (!(N.readThisFirst || []).length) return;
    const w = el("div", "warnbox", root);
    el("h2", null, w, "Read this first");
    const ul = el("ul", null, w);
    N.readThisFirst.forEach(t => el("li", null, ul, t));
  }

  function kpis(root) {
    const t = roll(months());
    const cmp = comparison();
    const wrap = el("div", "kpis", root);
    const tiles = [
      ["Revenue", money(t.rev), cmp && delta(cmp.cur.rev, cmp.prev.rev)],
      ["Gross profit", D.capabilities.margin ? money(t.gp) : "-",
        cmp && D.capabilities.margin ? delta(cmp.cur.gp, cmp.prev.gp) : null],
      ["Margin", D.capabilities.margin ? pct(t.margin, 2) : "-",
        cmp && D.capabilities.margin && cmp.prev.margin !== null
          ? { pp: t.margin === null ? null : cmp.cur.margin - cmp.prev.margin } : null],
      ["Orders", D.capabilities.orders ? num(t.orders) : "-",
        cmp && D.capabilities.orders ? delta(cmp.cur.orders, cmp.prev.orders) : null],
      ["Avg order value", D.capabilities.orders ? money(t.aov) : "-",
        cmp && D.capabilities.orders ? delta(cmp.cur.aov, cmp.prev.aov) : null],
      ["Units", num(t.qty), cmp ? delta(cmp.cur.qty, cmp.prev.qty) : null]
    ];
    tiles.forEach(([k, v, d]) => {
      const box = el("div", "kpi", wrap);
      el("div", "k", box, k);
      el("div", "v", box, v);
      const line = el("div", "d", box);
      if (d === null || d === undefined) {
        line.textContent = cmp ? "no prior year in range" : "select a comparable year";
      } else if (typeof d === "object") {
        line.className = "d " + (d.pp >= 0 ? "up" : "down");
        line.textContent = `${d.pp >= 0 ? "+" : ""}${d.pp.toFixed(2)} pts like-for-like`;
      } else {
        line.className = "d " + (d >= 0 ? "up" : "down");
        line.textContent = `${signed(d)} like-for-like vs ${cmp.label}`;
      }
    });
    if (CARDS.kpis && CARDS.kpis.so_what) {
      const p = el("p", "sub", root, CARDS.kpis.so_what);
      p.style.margin = "10px 2px 0";
    }
  }

  function card(grid, span, key, fallbackTitle) {
    const c = el("div", "card " + span, grid);
    const meta = CARDS[key] || {};
    el("h3", null, c, meta.title || fallbackTitle);
    if (meta.so_what) el("p", "sub", c, meta.so_what);
    return c;
  }

  /* --------------------------------------------------------------- charts */

  function trendCard(grid) {
    const c = card(grid, "span8", "trend", "Revenue and margin by month");
    const rows = byMonth(months());
    const H = 260, W = c.clientWidth - 36;
    const svg = sv("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, height: H }, c);
    const pad = { l: 54, r: D.capabilities.margin ? 46 : 14, t: 12, b: 34 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    const scale = ticks(Math.max(...rows.map(r => r.rev), 1), 4);
    const bw = Math.max(2, (iw / Math.max(rows.length, 1)) * 0.72);
    scale.list.forEach(t => {
      const y = pad.t + ih - (t / scale.max) * ih;
      sv("line", { class: "gridline", x1: pad.l, x2: pad.l + iw, y1: y, y2: y }, svg);
      sv("text", { class: "axis", x: pad.l - 8, y: y + 4, "text-anchor": "end" }, svg)
        .textContent = compact(t);
    });
    rows.forEach((r, i) => {
      const x = pad.l + (i + 0.5) * (iw / rows.length) - bw / 2;
      const h = (r.rev / scale.max) * ih;
      tip(sv("rect", {
        x, y: pad.t + ih - h, width: bw, height: Math.max(h, 0),
        fill: "var(--c1)", opacity: 0.85, rx: 2
      }, svg), `${monthLabel(r.m)}  ${money(r.rev)}` +
        (D.capabilities.margin ? `  ·  margin ${pct(r.margin, 1)}` : ""));
    });
    if (D.capabilities.margin) {
      const vals = rows.map(r => r.margin || 0);
      const ms = ticksSigned(Math.min(...vals, 0), Math.max(...vals, 1), 4);
      const my = v => pad.t + ih - ((v - ms.min) / (ms.max - ms.min)) * ih;
      if (ms.min < 0) {
        sv("line", { class: "axis-line", x1: pad.l, x2: pad.l + iw, y1: my(0), y2: my(0),
                     "stroke-dasharray": "3 3" }, svg);
      }
      let d = "";
      rows.forEach((r, i) => {
        const x = pad.l + (i + 0.5) * (iw / rows.length);
        d += (i ? "L" : "M") + x + "," + my(r.margin || 0);
      });
      sv("path", { d, fill: "none", stroke: "var(--c2)", "stroke-width": 2 }, svg);
      ms.list.forEach(t => {
        sv("text", { class: "axis", x: pad.l + iw + 8, y: my(t) + 4 }, svg)
          .textContent = t.toFixed(0) + "%";
      });
    }
    const stride = Math.max(1, Math.ceil(rows.length / Math.max(1, Math.floor(iw / 58))));
    rows.forEach((r, i) => {
      if (i % stride) return;
      sv("text", {
        class: "axis", x: pad.l + (i + 0.5) * (iw / rows.length),
        y: pad.t + ih + 16, "text-anchor": "middle"
      }, svg).textContent = monthLabel(r.m);
    });
    const lg = el("div", "legend", c);
    legendItem(lg, "var(--c1)", "Revenue");
    if (D.capabilities.margin) legendItem(lg, "var(--c2)", "Gross margin %");
  }

  function legendItem(parent, color, label) {
    const s = el("span", null, parent);
    el("i", null, s).style.background = color;
    s.appendChild(document.createTextNode(label));
  }

  function byMonth(rows) {
    const map = new Map();
    rows.forEach(r => {
      let e = map.get(r.m);
      if (!e) { e = { m: r.m, fy: r.fy, rev: 0, cost: 0, qty: 0, orders: 0 }; map.set(r.m, e); }
      e.rev += r.rev || 0; e.cost += r.cost || 0; e.qty += r.qty || 0; e.orders += r.orders || 0;
    });
    const out = [...map.values()].sort((a, b) => a.m < b.m ? -1 : 1);
    out.forEach(r => {
      r.gp = r.rev - r.cost;
      r.margin = r.rev ? (r.gp / r.rev) * 100 : 0;
      r.aov = r.orders ? r.rev / r.orders : 0;
    });
    return out;
  }

  function channelCard(grid) {
    if (!D.capabilities.channel) return;
    const c = card(grid, "span4", "channel", "Channel economics");
    const rows = D.channels.map(ch => {
      const t = roll(D.cubes.months.filter(r => r.ch === ch && state.years.has(r.fy)));
      return { ch, ...t };
    });
    const total = roll(D.cubes.months.filter(r => state.years.has(r.fy)));
    const tbl = el("table", "data", el("div", "scroll-x", c));
    const hr = el("tr", null, el("thead", null, tbl));
    ["Channel", "Revenue", "Gross profit", "Margin"].forEach(h => el("th", null, hr, h));
    const tb = el("tbody", null, tbl);
    rows.sort((a, b) => b.rev - a.rev).forEach(r => {
      const tr = el("tr", null, tb);
      el("td", null, tr, r.ch);
      const rc = el("td", null, tr);
      el("div", null, rc, money(r.rev));
      const bar = el("span", "microbar", rc);
      el("i", null, bar).style.width = (total.rev ? (r.rev / total.rev) * 100 : 0) + "%";
      el("td", null, tr, D.capabilities.margin ? money(r.gp) : "-");
      const mc = el("td", "", tr);
      mc.textContent = D.capabilities.margin ? pct(r.margin, 2) : "-";
      if (D.capabilities.margin) mc.className = r.margin >= 0 ? "up" : "down";
    });
    const tr = el("tr", "total", tb);
    el("td", null, tr, "Total");
    el("td", null, tr, money(total.rev));
    el("td", null, tr, D.capabilities.margin ? money(total.gp) : "-");
    el("td", null, tr, D.capabilities.margin ? pct(total.margin, 2) : "-");
    el("p", "sub", c, "Unfiltered by the channel chips above, so the split stays readable.")
      .style.marginTop = "10px";
  }

  function categoryCard(grid) {
    if (!D.capabilities.category) return;
    const c = card(grid, "span6", "category", "Category economics");
    const cur = agg(D.cubes.category.filter(inSel), r => r.cat);
    // Compare each selected year against its predecessor. This cube has no
    // month grain, so the comparison is whole fiscal year against whole fiscal
    // year - which understates a year the data stops part way through.
    const pairs = [...state.years].sort()
      .map(y => [y, D.years[D.years.indexOf(y) - 1]]).filter(p => p[1]);
    const prevYears = new Set(pairs.map(p => p[1]));
    const cmpYears = new Set(pairs.map(p => p[0]));
    const cmpCur = pairs.length ? agg(D.cubes.category.filter(
      r => cmpYears.has(r.fy) && (!r.ch || state.channels.has(r.ch))), r => r.cat) : null;
    const prev = pairs.length ? agg(D.cubes.category.filter(
      r => prevYears.has(r.fy) && (!r.ch || state.channels.has(r.ch))), r => r.cat) : null;
    const prevLabel = [...prevYears].sort().join(", ");
    const partial = (((D.period || {}).periods) || [])
      .filter(p => p.partial && cmpYears.has(p.fiscal_year)).map(p => p.fiscal_year);
    const max = Math.max(...[...cur.values()].map(v => v.rev), 1);
    const tbl = el("table", "data", el("div", "scroll-x", c));
    const hr = el("tr", null, el("thead", null, tbl));
    ["Category", "Revenue", "", "Margin", "Units", prev ? "vs " + prevLabel : "YoY"]
      .forEach(h => el("th", null, hr, h));
    const tb = el("tbody", null, tbl);
    [...cur.entries()].sort((a, b) => b[1].rev - a[1].rev).forEach(([k, v]) => {
      const tr = el("tr", null, tb);
      el("td", null, tr, k);
      el("td", null, tr, money(v.rev));
      const bc = el("td", null, tr);
      const bar = el("span", "microbar", bc);
      el("i", null, bar).style.width = (v.rev / max) * 100 + "%";
      const mc = el("td", null, tr, D.capabilities.margin ? pct(v.margin, 1) : "-");
      if (D.capabilities.margin) mc.className = v.margin >= 0 ? "up" : "down";
      el("td", null, tr, num(v.qty));
      const p = prev && prev.get(k);
      const c0 = cmpCur && cmpCur.get(k);
      const d = (p && c0) ? delta(c0.rev, p.rev) : null;
      const dc = el("td", null, tr, d === null ? "-" : signed(d));
      if (d !== null) dc.className = d >= 0 ? "up" : "down";
    });
    if (partial.length) {
      el("p", "sub", c, `Year-over-year compares whole fiscal years. ${partial.join(", ")} `
        + "stops part way through, so its change is understated.").style.marginTop = "10px";
    }
  }

  function agg(rows, keyFn) {
    const m = new Map();
    rows.forEach(r => {
      const k = keyFn(r) || "(blank)";
      let e = m.get(k);
      if (!e) { e = { rev: 0, cost: 0, qty: 0, orders: 0 }; m.set(k, e); }
      e.rev += r.rev || 0; e.cost += r.cost || 0; e.qty += r.qty || 0; e.orders += r.orders || 0;
    });
    m.forEach(v => {
      v.gp = v.rev - v.cost;
      v.margin = v.rev ? (v.gp / v.rev) * 100 : 0;
    });
    return m;
  }

  function moversCard(grid) {
    const mv = D.movers;
    if (!mv) return;
    const c = card(grid, "span6", "movers", `What moved, ${mv.previous_year} to ${mv.current_year}`);
    const rows = mv.rows.slice(0, 10);
    const H = Math.max(160, rows.length * 24 + 30), W = c.clientWidth - 36;
    const svg = sv("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, height: H }, c);
    const labelW = 118;
    const max = Math.max(...rows.map(r => Math.abs(r.delta)), 1);
    const zero = labelW + (W - labelW - 70) / 2;
    const half = (W - labelW - 70) / 2;
    rows.forEach((r, i) => {
      const y = 14 + i * 24;
      const w = (Math.abs(r.delta) / max) * half;
      sv("rect", {
        x: r.delta >= 0 ? zero : zero - w, y: y - 8, width: w, height: 15, rx: 2,
        fill: r.delta >= 0 ? "var(--c3)" : "var(--c2)"
      }, svg);
      sv("text", { class: "axis", x: labelW - 8, y: y + 4, "text-anchor": "end" }, svg)
        .textContent = r.member.length > 18 ? r.member.slice(0, 17) + "…" : r.member;
      sv("text", {
        class: "axis", x: r.delta >= 0 ? zero + w + 6 : zero - w - 6, y: y + 4,
        "text-anchor": r.delta >= 0 ? "start" : "end"
      }, svg).textContent = (r.delta >= 0 ? "+" : "-") + compact(Math.abs(r.delta));
    });
    sv("line", { class: "axis-line", x1: zero, x2: zero, y1: 2, y2: H - 12 }, svg);
    el("p", "sub", c, "Revenue change by subcategory. This card reads the whole model, not the year chips.")
      .style.marginTop = "8px";
  }

  function paretoCard(grid) {
    if (!D.capabilities.pareto) return;
    const c = card(grid, "span6", "pareto", "Revenue concentration by product");
    const rows = [...agg(D.cubes.pareto.filter(inSel), r => r.member).entries()]
      .map(([member, v]) => ({ member, rev: v.rev }))
      .sort((a, b) => b.rev - a.rev);
    const other = rows.findIndex(r => r.member === "(all other products)");
    if (other > -1) rows.push(rows.splice(other, 1)[0]);
    const total = rows.reduce((s, r) => s + r.rev, 0) || 1;
    // Keep the folded tail in the picture; without it the cumulative line
    // stops short of 100% and reads as if the catalogue ended at 24 products.
    const tail = other > -1 ? rows[rows.length - 1] : null;
    const show = tail ? rows.slice(0, 23).concat([tail]) : rows.slice(0, 24);
    const H = 240, W = c.clientWidth - 36;
    const svg = sv("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, height: H }, c);
    const pad = { l: 48, r: 40, t: 10, b: 46 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    const scale = ticks(show[0] ? show[0].rev : 1, 3);
    scale.list.forEach(t => {
      const y = pad.t + ih - (t / scale.max) * ih;
      sv("line", { class: "gridline", x1: pad.l, x2: pad.l + iw, y1: y, y2: y }, svg);
      sv("text", { class: "axis", x: pad.l - 8, y: y + 4, "text-anchor": "end" }, svg)
        .textContent = compact(t);
    });
    const bw = (iw / Math.max(show.length, 1)) * 0.7;
    let cum = 0;
    let line = "";
    show.forEach((r, i) => {
      const cx = pad.l + (i + 0.5) * (iw / show.length);
      const h = (r.rev / scale.max) * ih;
      tip(sv("rect", {
        x: cx - bw / 2, y: pad.t + ih - h, width: bw, height: Math.max(h, 0), rx: 2,
        fill: r.member === "(all other products)" ? "var(--line-2)" : "var(--c1)", opacity: .85
      }, svg), `${r.member}  ${money(r.rev)}  (${pct((r.rev / total) * 100)})`);
      cum += r.rev;
      const y = pad.t + ih - (cum / total) * ih;
      line += (i ? "L" : "M") + cx + "," + y;
    });
    sv("path", { d: line, fill: "none", stroke: "var(--c2)", "stroke-width": 2 }, svg);
    [0, 25, 50, 75, 100].forEach(p => {
      sv("text", { class: "axis", x: pad.l + iw + 8, y: pad.t + ih - (p / 100) * ih + 4 }, svg)
        .textContent = p + "%";
    });
    const conc = (D.concentration || {}).product;
    el("p", "sub", c, conc
      ? `${conc.members_to_80pct} of ${num(conc.members)} products reach 80% of revenue. Bars are the top ${show.length - (tail ? 1 : 0)}; the grey bar is every remaining product.`
      : "Bars are the largest products; the line is the cumulative share.").style.marginTop = "8px";
  }

  function economicsCard(grid) {
    if (!D.capabilities.orders) return;
    const c = card(grid, "span12", "economics", "Order economics");
    const rows = byMonth(months());
    const H = 230, W = c.clientWidth - 36;
    const svg = sv("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, height: H }, c);
    const pad = { l: 54, r: 60, t: 12, b: 34 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    const os = ticks(Math.max(...rows.map(r => r.orders), 1), 4);
    const as = ticks(Math.max(...rows.map(r => r.aov), 1), 4);
    os.list.forEach(t => {
      const y = pad.t + ih - (t / os.max) * ih;
      sv("line", { class: "gridline", x1: pad.l, x2: pad.l + iw, y1: y, y2: y }, svg);
      sv("text", { class: "axis", x: pad.l - 8, y: y + 4, "text-anchor": "end" }, svg)
        .textContent = compact(t);
    });
    as.list.forEach(t => {
      sv("text", { class: "axis", x: pad.l + iw + 8, y: pad.t + ih - (t / as.max) * ih + 4 }, svg)
        .textContent = "$" + compact(t);
    });
    let lo = "", la = "";
    rows.forEach((r, i) => {
      const x = pad.l + (i + 0.5) * (iw / Math.max(rows.length, 1));
      lo += (i ? "L" : "M") + x + "," + (pad.t + ih - (r.orders / os.max) * ih);
      la += (i ? "L" : "M") + x + "," + (pad.t + ih - (r.aov / as.max) * ih);
      tip(sv("circle", { cx: x, cy: pad.t + ih - (r.orders / os.max) * ih, r: 2.5, fill: "var(--c4)" }, svg),
        `${monthLabel(r.m)}  ${num(r.orders)} orders  ·  AOV ${money(r.aov)}`);
    });
    sv("path", { d: lo, fill: "none", stroke: "var(--c4)", "stroke-width": 2 }, svg);
    sv("path", { d: la, fill: "none", stroke: "var(--c5)", "stroke-width": 2, "stroke-dasharray": "4 3" }, svg);
    const stride = Math.max(1, Math.ceil(rows.length / Math.max(1, Math.floor(iw / 58))));
    rows.forEach((r, i) => {
      if (i % stride) return;
      sv("text", {
        class: "axis", x: pad.l + (i + 0.5) * (iw / rows.length), y: pad.t + ih + 16,
        "text-anchor": "middle"
      }, svg).textContent = monthLabel(r.m);
    });
    const lg = el("div", "legend", c);
    legendItem(lg, "var(--c4)", "Orders (left)");
    legendItem(lg, "var(--c5)", "Average order value (right)");
  }

  function qualityCard(grid) {
    const list = D.quality || [];
    if (!list.length) return;
    const c = card(grid, "span6", "quality", "What changes how these numbers read");
    list.forEach(q => {
      const f = el("div", "finding warn", c);
      el("div", "t", f, q.kind.replace(/_/g, " "));
      el("div", "d", f, q.detail);
      el("div", "e", f, q.effect);
    });
    const un = D.unused;
    if (un && un.tables_unreferenced && un.tables_unreferenced.length) {
      const f = el("div", "finding", c);
      el("div", "t", f, "unused by the source report");
      el("div", "d", f, un.tables_unreferenced.join(", "));
      el("div", "e", f, un.note);
    }
  }

  function actionsCard(grid) {
    const acts = N.actions || [];
    if (!acts.length) return;
    const c = card(grid, "span6", "actions", "What to do about it");
    const ol = el("ol", "actions", c);
    acts.forEach(a => {
      const li = el("li", null, ol);
      el("div", "t", li, a.title);
      el("div", "w", li, a.why);
      if (a.evidence) el("div", "ev", li, a.evidence);
    });
  }

  function provenance(root) {
    const f = el("footer", "prov", root);
    const g = D.generated || {};
    el("div", null, f,
      `Built from ${g.source || "the extracted model"} · ${num(g.rows)} rows · ` +
      `roles detected: ${Object.entries(D.roles).filter(([, v]) => v).map(([k, v]) => k + "=" + v).join(", ")}`);
    if (N.method) el("div", null, f, N.method);
    const det = el("details", "sql", f);
    el("summary", null, det, "The SQL behind every number on this page");
    Object.entries(g.queries || {}).forEach(([k, sql]) => {
      if (!sql) return;
      el("div", null, det, k).style.cssText = "margin-top:10px;font-weight:600";
      el("pre", null, det, sql.trim());
    });
  }

  /* ---------------------------------------------------------------- render */

  function render() {
    const app = document.getElementById("app");
    app.innerHTML = "";
    const root = el("div", "wrap", app);
    masthead(root);
    controls(root);
    readFirst(root);
    kpis(root);
    const grid = el("div", "grid", root);
    trendCard(grid);
    channelCard(grid);
    categoryCard(grid);
    moversCard(grid);
    paretoCard(grid);
    economicsCard(grid);
    qualityCard(grid);
    actionsCard(grid);
    provenance(root);
  }

  let t = null;
  window.addEventListener("resize", () => { clearTimeout(t); t = setTimeout(render, 150); });
  render();
})();
