/* Replica runtime.
 *
 * Each visual ships a small cube: the rows Power BI would have aggregated,
 * pre-grouped to the visual's own fields plus whatever the page's slicers
 * filter on. Clicking a slicer re-aggregates in the browser, so the report is
 * live rather than a picture of one.
 *
 * Re-aggregating client-side is only valid for additive measures. The builder
 * marks non-additive ones (averages, distinct counts) and those visuals render
 * at the report's saved filter state instead of following the slicer.
 */
(function () {
  const R = window.REPORT;
  const state = { page: R.activePageIndex || 0, selection: {} };

  const fmt = {
    num(v, dp) {
      if (v === null || v === undefined || Number.isNaN(v)) return "";
      return v.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });
    },
    money(v) { return fmt.num(v, 2); },
    axis(v) {
      const a = Math.abs(v);
      if (a >= 1e9) return (v / 1e9).toFixed(a >= 1e10 ? 0 : 1) + "B";
      if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
      if (a >= 1e3) return (v / 1e3).toFixed(0) + "K";
      return String(Math.round(v * 100) / 100);
    },
    month(v) {
      const d = new Date(v);
      if (Number.isNaN(d.getTime())) return String(v);
      return d.toLocaleString("en-US", { month: "short", timeZone: "UTC" }) + " " + d.getUTCFullYear();
    },
    label(col, v) {
      if (v === null || v === undefined || v === "") return "(Blank)";
      return col && col.isDate ? fmt.month(v) : String(v);
    }
  };

  /* ------------------------------------------------------------ filtering */

  function activeMonths() {
    // The slicer is a Year > Month hierarchy: a checked year contributes all of
    // its months, a checked month contributes itself. Empty means unfiltered.
    const sel = state.selection;
    const s = R.slicer;
    if (!s) return null;
    const months = new Set();
    let any = false;
    (s.tree || []).forEach(node => {
      const yearOn = sel[node.key];
      (node.children || []).forEach(child => {
        if (yearOn || sel[child.key]) { months.add(child.value); any = true; }
      });
      if (yearOn && (!node.children || !node.children.length)) any = true;
    });
    return any ? months : null;
  }

  function rowsFor(v) {
    const months = v.followsSlicer ? activeMonths() : null;
    if (!months || v.slicerCol === null || v.slicerCol === undefined) return v.rows;
    return v.rows.filter(r => months.has(r[v.slicerCol]));
  }

  function aggregate(v) {
    const rows = rowsFor(v);
    const keyIdx = v.dimIdx, sortIdx = v.sortIdx, valIdx = v.valueIdx;
    const map = new Map();
    for (const r of rows) {
      const key = keyIdx.map(i => r[i]).join("");
      let e = map.get(key);
      if (!e) {
        e = {
          key: key,
          dims: keyIdx.map(i => r[i]),
          sort: sortIdx.map(i => (i === null ? null : r[i])),
          vals: valIdx.map(() => null)
        };
        map.set(key, e);
      }
      valIdx.forEach((i, n) => {
        const x = r[i];
        if (x === null || x === undefined) return;
        e.vals[n] = (e.vals[n] || 0) + x;
      });
    }
    const out = [...map.values()];
    applySort(out, v);
    return out;
  }

  function applySort(rows, v) {
    const rules = (v.sortRules && v.sortRules.length) ? v.sortRules : v.defaultSort;
    if (!rules || !rules.length) return;
    rows.sort((a, b) => {
      for (const rule of rules) {
        let av, bv;
        if (rule.kind === "value") { av = a.vals[rule.index]; bv = b.vals[rule.index]; }
        else {
          av = a.sort[rule.index] !== null && a.sort[rule.index] !== undefined ? a.sort[rule.index] : a.dims[rule.index];
          bv = b.sort[rule.index] !== null && b.sort[rule.index] !== undefined ? b.sort[rule.index] : b.dims[rule.index];
        }
        if (av === bv) continue;
        if (av === null || av === undefined) return 1;
        if (bv === null || bv === undefined) return -1;
        const cmp = av < bv ? -1 : 1;
        return rule.desc ? -cmp : cmp;
      }
      return 0;
    });
  }

  /* -------------------------------------------------------------- helpers */

  const NS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) if (attrs[k] !== null && attrs[k] !== undefined) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function el(tag, cls, parent) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (parent) parent.appendChild(e);
    return e;
  }
  function niceTicks(max, count) {
    if (!(max > 0)) return { max: 1, ticks: [0, 1] };
    const raw = max / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
    const top = Math.ceil(max / step) * step;
    const ticks = [];
    for (let t = 0; t <= top + step / 2; t += step) ticks.push(t);
    return { max: top, ticks: ticks };
  }
  function clip(s, n) { return s.length > n ? s.slice(0, n - 1) + "…" : s; }

  /* ------------------------------------------------------------ renderers */

  function renderArea(host, v) {
    const rows = aggregate(v);
    const box = el("div", "legend", host);
    v.measures.forEach(m => {
      const s = el("span", null, box);
      const dot = el("i", null, s);
      dot.style.background = m.color;
      s.appendChild(document.createTextNode(m.label));
    });
    const body = el("div", "vis-body flush", host);
    const svg = svgEl("svg", { preserveAspectRatio: "none" }, body);
    requestAnimationFrame(() => drawArea(svg, v, rows));
  }

  function drawArea(svg, v, rows) {
    const W = svg.clientWidth || 600, H = svg.clientHeight || 220;
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.innerHTML = "";
    const pad = { l: 56, r: 12, t: 10, b: 42 };
    const iw = Math.max(10, W - pad.l - pad.r), ih = Math.max(10, H - pad.t - pad.b);
    let max = 0;
    rows.forEach(r => r.vals.forEach(x => { if (x > max) max = x; }));
    const scale = niceTicks(max, 3);
    const x = i => pad.l + (rows.length <= 1 ? iw / 2 : (i * iw) / (rows.length - 1));
    const y = val => pad.t + ih - (val / scale.max) * ih;

    scale.ticks.forEach(t => {
      svgEl("line", { class: "gridline", x1: pad.l, x2: pad.l + iw, y1: y(t), y2: y(t) }, svg);
      const tx = svgEl("text", { x: pad.l - 8, y: y(t) + 4, "text-anchor": "end", class: "axis" }, svg);
      tx.textContent = fmt.axis(t);
    });
    v.measures.forEach((m, mi) => {
      let area = "", line = "";
      rows.forEach((r, i) => {
        const val = r.vals[mi] || 0;
        area += (i ? "L" : "M") + x(i) + "," + y(val);
        line += (i ? "L" : "M") + x(i) + "," + y(val);
      });
      if (rows.length) area += "L" + x(rows.length - 1) + "," + y(0) + "L" + x(0) + "," + y(0) + "Z";
      svgEl("path", { d: area, fill: m.color, "fill-opacity": 0.45 }, svg);
      svgEl("path", { d: line, fill: "none", stroke: m.color, "stroke-width": 2 }, svg);
    });
    const stride = Math.max(1, Math.ceil((rows.length * 62) / Math.max(iw, 1)));
    rows.forEach((r, i) => {
      if (i % stride) return;
      const t = svgEl("text", { x: x(i), y: pad.t + ih + 16, "text-anchor": "middle", class: "axis" }, svg);
      t.textContent = fmt.label(v.dims[0], r.dims[0]);
    });
    const xt = svgEl("text", { x: pad.l + iw / 2, y: H - 6, "text-anchor": "middle", class: "axis-title" }, svg);
    xt.textContent = v.dims[0].label;
    const yt = svgEl("text", {
      class: "axis-title", "text-anchor": "middle",
      transform: "translate(12, " + (pad.t + ih / 2) + ") rotate(-90)"
    }, svg);
    yt.textContent = clip(v.measures.map(m => m.label).join(" and "), 26);
  }

  function renderBar(host, v) {
    const rows = aggregate(v);
    const body = el("div", "vis-body flush", host);
    const svg = svgEl("svg", { preserveAspectRatio: "none" }, body);
    requestAnimationFrame(() => drawBar(svg, v, rows));
  }

  function drawBar(svg, v, rows) {
    const W = svg.clientWidth || 440, H = svg.clientHeight || 220;
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.innerHTML = "";
    const labelW = Math.min(130, Math.max(70, W * 0.3));
    const pad = { l: labelW + 14, r: 14, t: 8, b: 40 };
    const iw = Math.max(10, W - pad.l - pad.r), ih = Math.max(10, H - pad.t - pad.b);
    let max = 0;
    rows.forEach(r => { if (r.vals[0] > max) max = r.vals[0]; });
    const scale = niceTicks(max, 3);
    const bandH = ih / Math.max(rows.length, 1);
    const barH = Math.min(22, bandH * 0.62);
    const color = v.measures[0].color;

    rows.forEach((r, i) => {
      const cy = pad.t + bandH * i + bandH / 2;
      const w = ((r.vals[0] || 0) / scale.max) * iw;
      svgEl("rect", { x: pad.l, y: cy - barH / 2, width: Math.max(w, 0), height: barH, fill: color }, svg);
      const t = svgEl("text", { x: pad.l - 10, y: cy + 4, "text-anchor": "end", class: "axis" }, svg);
      t.textContent = clip(fmt.label(v.dims[0], r.dims[0]), 22);
    });
    scale.ticks.forEach(tick => {
      const px = pad.l + (tick / scale.max) * iw;
      svgEl("line", { class: "gridline", x1: px, x2: px, y1: pad.t, y2: pad.t + ih }, svg);
      const t = svgEl("text", { x: px, y: pad.t + ih + 16, "text-anchor": "middle", class: "axis" }, svg);
      t.textContent = fmt.axis(tick);
    });
    const xt = svgEl("text", { x: pad.l + iw / 2, y: H - 6, "text-anchor": "middle", class: "axis-title" }, svg);
    xt.textContent = v.measures[0].label;
    const yt = svgEl("text", {
      class: "axis-title", "text-anchor": "middle",
      transform: "translate(10, " + (pad.t + ih / 2) + ") rotate(-90)"
    }, svg);
    yt.textContent = clip(v.dims[0].label, 20);
  }

  function renderMatrix(host, v) {
    const rows = aggregate(v);
    const body = el("div", "vis-body", host);
    const scroll = el("div", "scroll-y", body);
    const table = el("table", "matrix", scroll);
    // Power BI saves explicit column widths; honouring them is what puts the
    // data bars in the same place as the original.
    const widths = v.columnWidth || {};
    if (Object.keys(widths).length) {
      const cg = el("colgroup", null, table);
      el("col", null, cg);
      v.measures.forEach(m => {
        const c = el("col", null, cg);
        if (widths[m.ref]) c.style.width = Math.round(widths[m.ref]) + "px";
      });
    }
    const thead = el("thead", null, table);
    const hr = el("tr", null, thead);
    el("th", null, hr).textContent = v.dims[0].label;
    v.measures.forEach(m => { el("th", "num", hr).textContent = m.label; });

    // Group leaves under their first-level parent, keeping the order the
    // aggregation already produced.
    const groups = new Map();
    rows.forEach(r => {
      const k = String(r.dims[0]);
      if (!groups.has(k)) {
        groups.set(k, {
          label: fmt.label(v.dims[0], r.dims[0]), sort: r.sort[0],
          rows: [], total: v.measures.map(() => 0)
        });
      }
      const g = groups.get(k);
      g.rows.push(r);
      r.vals.forEach((x, i) => { g.total[i] += x || 0; });
    });
    const ordered = [...groups.values()];
    if (v.sortRules && v.sortRules.length && v.sortRules[0].kind === "value") {
      const idx = v.sortRules[0].index, desc = v.sortRules[0].desc;
      ordered.sort((a, b) => (desc ? b.total[idx] - a.total[idx] : a.total[idx] - b.total[idx]));
    }

    let barMax = 0;
    ordered.forEach(g => g.rows.forEach(r => { if ((r.vals[0] || 0) > barMax) barMax = r.vals[0]; }));
    const bar = v.dataBars && v.dataBars[v.measures[0].ref];

    const tbody = el("tbody", null, table);
    const grand = v.measures.map(() => 0);
    ordered.forEach(g => {
      const tr = el("tr", "level0", tbody);
      const td = el("td", null, tr);
      const tog = el("span", "toggle", td);
      tog.textContent = "⊟";
      td.appendChild(document.createTextNode(g.label));
      g.total.forEach((x, i) => {
        el("td", "num", tr).textContent = fmt.money(x);
        grand[i] += x;
      });
      const children = [];
      g.rows.forEach(r => {
        const ctr = el("tr", "child body-row", tbody);
        el("td", null, ctr).textContent = v.dims.length > 1 ? fmt.label(v.dims[1], r.dims[1]) : g.label;
        r.vals.forEach((x, i) => {
          const c = el("td", "num" + (i === 0 && bar ? " databar-cell" : ""), ctr);
          if (i === 0 && bar && barMax > 0) {
            const b = el("div", "databar", c);
            b.style.width = Math.max(0, ((x || 0) / barMax) * 100) + "%";
            b.style.background = bar.positiveColor || "#c4d3e9";
          }
          el("span", "databar-value", c).textContent = fmt.money(x);
        });
        children.push(ctr);
      });
      tog.onclick = () => {
        const hide = children.length && children[0].style.display !== "none";
        children.forEach(c => { c.style.display = hide ? "none" : ""; });
        tog.textContent = hide ? "⊞" : "⊟";
      };
    });
    const tr = el("tr", "total", tbody);
    el("td", null, tr).textContent = "Total";
    grand.forEach(x => { el("td", "num", tr).textContent = fmt.money(x); });
  }

  function renderSlicer(host, v) {
    const wrap = el("div", "slicer", host);
    el("div", "slicer-header", wrap).textContent = v.header;
    const list = el("div", "slicer-list", wrap);

    function setSel(key, on) {
      if (on) state.selection[key] = true; else delete state.selection[key];
    }

    function row(parent, label, checked, partial, onToggle, expandable, node, child) {
      const r = el("div", "slicer-row" + (child ? " child" : "") + (node && node.blank ? " blank" : ""), parent);
      const chev = el("span", "chev" + (expandable ? "" : " leaf"), r);
      chev.textContent = node && node.expanded ? "⌄" : "›";
      if (expandable) chev.onclick = () => { node.expanded = !node.expanded; draw(); };
      const box = el("span", "box" + (checked ? " on" : "") + (partial ? " part" : ""), r);
      const lab = el("label", null, r);
      lab.textContent = label;
      box.onclick = onToggle;
      lab.onclick = onToggle;
      return r;
    }

    function draw() {
      list.innerHTML = "";
      const tree = R.slicer.tree;
      if (v.selectAllCheckbox) {
        const all = tree.every(n => state.selection[n.key]);
        const some = tree.some(n => state.selection[n.key]);
        row(list, "Select all", all, some && !all, () => {
          const on = !all;
          tree.forEach(n => {
            setSel(n.key, on);
            (n.children || []).forEach(c => setSel(c.key, false));
          });
          rerender();
        }, false, null);
      }
      tree.forEach(node => {
        const kids = node.children || [];
        const checked = !!state.selection[node.key];
        const partial = !checked && kids.some(c => state.selection[c.key]);
        row(list, node.label, checked, partial, () => {
          const on = !state.selection[node.key];
          setSel(node.key, on);
          kids.forEach(c => setSel(c.key, false));
          rerender();
        }, kids.length > 0, node);
        if (node.expanded) {
          kids.forEach(child => {
            row(list, child.label, !!state.selection[child.key] || checked, false, () => {
              if (checked) {                  // unpick the parent, keep siblings
                setSel(node.key, false);
                kids.forEach(c => { if (c !== child) setSel(c.key, true); });
              } else {
                setSel(child.key, !state.selection[child.key]);
              }
              rerender();
            }, false, null, true);
          });
        }
      });
    }

    draw();
  }

  function renderTextbox(host, v) {
    const body = el("div", "vis-body", host);
    (v.paragraphs || []).forEach(p => {
      const div = el("div", null, body);
      if (p.align) div.style.textAlign = p.align;
      (p.runs || []).forEach(run => {
        const s = el("span", null, div);
        s.textContent = run.text;
        if (run.color) s.style.color = run.color;
        if (run.fontSize) s.style.fontSize = run.fontSize;
        if (run.fontWeight) s.style.fontWeight = run.fontWeight;
        if (run.fontStyle) s.style.fontStyle = run.fontStyle;
        if (run.fontFamily) s.style.fontFamily = run.fontFamily;
      });
    });
  }

  function renderShape(host, v) {
    host.style.background = v.fillColor || "transparent";
    if (v.roundEdge) host.style.borderRadius = v.roundEdge + "px";
    if (v.lineColor && v.lineTransparency !== 100) host.style.border = "1px solid " + v.lineColor;
  }

  function renderUnsupported(host, v) {
    const body = el("div", "vis-body", host);
    el("div", "unsupported", body).textContent =
      v.type + " is not in the replica renderer set." + (v.note ? " " + v.note : "");
  }

  /* --------------------------------------------------------------- layout */

  const RENDERERS = {
    areaChart: renderArea, barChart: renderBar, columnChart: renderBar,
    pivotTable: renderMatrix, tableEx: renderMatrix, slicer: renderSlicer,
    textbox: renderTextbox, basicShape: renderShape
  };

  function rerender() {
    const page = R.pages[state.page];
    const canvas = document.getElementById("canvas");
    canvas.innerHTML = "";
    canvas.style.width = page.width + "px";
    canvas.style.height = page.height + "px";

    page.visuals.forEach(v => {
      const card = ["textbox", "basicShape"].indexOf(v.type) === -1;
      const host = el("div", "vis " + v.type + (card ? " card" : "") + (v.shadow ? " shadow" : ""), canvas);
      host.style.left = v.x + "px";
      host.style.top = v.y + "px";
      host.style.width = v.width + "px";
      host.style.height = v.height + "px";
      host.style.zIndex = v.z || 0;
      if (v.title && v.title.text) {
        const t = el("div", "vis-title", host);
        t.textContent = v.title.text;
        if (v.title.fontSize) t.style.fontSize = v.title.fontSize + "px";
        if (v.title.color) t.style.color = v.title.color;
      }
      (RENDERERS[v.type] || renderUnsupported)(host, v);
    });
    fit();
  }

  function fit() {
    const page = R.pages[state.page];
    const wrap = document.getElementById("canvas-wrap");
    const avail = wrap.parentElement.clientWidth - 32;
    const s = Math.min(1, avail / page.width);
    const canvas = document.getElementById("canvas");
    canvas.style.transform = "scale(" + s + ")";
    wrap.style.width = page.width * s + "px";
    wrap.style.height = page.height * s + "px";
  }

  function buildTabs() {
    const tabs = document.getElementById("tabs");
    R.pages.forEach((p, i) => {
      const b = el("button", "tab" + (i === state.page ? " active" : ""), tabs);
      b.textContent = p.displayName;
      b.onclick = () => {
        state.page = i;
        [...tabs.children].forEach((c, j) => { c.className = "tab" + (j === i ? " active" : ""); });
        rerender();
      };
    });
  }

  window.addEventListener("resize", fit);
  buildTabs();
  (R.initialSelection || []).forEach(k => { state.selection[k] = true; });
  rerender();
})();
