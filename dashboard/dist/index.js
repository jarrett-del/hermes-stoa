(function () {
  "use strict";
  // Stoa dashboard tab. Plain React.createElement against the host's Plugin SDK: no build step,
  // no bundled libraries, no network calls except to this plugin's own /api/plugins/stoa routes.
  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;
  var React = SDK.React;
  var useState = SDK.hooks.useState, useEffect = SDK.hooks.useEffect, useRef = SDK.hooks.useRef,
    useMemo = SDK.hooks.useMemo, useCallback = SDK.hooks.useCallback;
  var h = React.createElement;
  var API = "/api/plugins/stoa";
  var POLL_MS = 4000;

  function get(path) { return SDK.fetchJSON(API + path); }
  function post(path, body) {
    return SDK.fetchJSON(API + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  }

  // -- formatting -------------------------------------------------------------------------------
  function money(value, currency) {
    if (!value) return "";
    var sym = { USD: "$", EUR: "€", GBP: "£" }[currency || "USD"] || "";
    var text = value >= 1e6 ? (value / 1e6).toFixed(1) + "M" : value >= 1e4 ? Math.round(value / 1e3) + "k" : Math.round(value).toLocaleString();
    return sym ? sym + text : text + " " + currency;
  }
  function ago(iso) {
    var t = Date.parse(iso);
    if (!t) return "";
    var s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 90) return "just now";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    if (s < 172800) return "yesterday";
    return Math.round(s / 86400) + "d ago";
  }
  function cap(text) { text = String(text || ""); return text.charAt(0).toUpperCase() + text.slice(1); }
  function initials(name) {
    var words = String(name || "").replace(/[^A-Za-z0-9 ]/g, " ").split(/\s+/).filter(Boolean);
    return ((words[0] || "?").charAt(0) + (words[1] ? words[1].charAt(0) : (words[0] || "").charAt(1) || "")).toUpperCase();
  }
  function today() { var d = new Date(); return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2); }
  function tone(kind) { return { stage: "grad", created: "good", call: "good", email: "good", meeting: "good", snooze: "warn" }[kind] || ""; }
  function isDark() {
    try {
      var m = getComputedStyle(document.body).backgroundColor.match(/[\d.]+/g);
      if (!m || (m[3] !== undefined && parseFloat(m[3]) === 0)) m = getComputedStyle(document.documentElement).backgroundColor.match(/[\d.]+/g);
      if (!m) return false;
      return (0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2]) / 255 < 0.45;
    } catch (e) { return false; }
  }

  // -- small pieces -----------------------------------------------------------------------------
  function Btn(props) {
    return h("button", { className: "stoa-btn", "data-kind": props.kind || "", disabled: props.disabled, onClick: props.onClick, title: props.title }, props.children);
  }
  function Chip(props) { return h("span", { className: "stoa-chip", "data-tone": props.tone || "" }, props.children); }
  function Fit(props) {
    var out = [];
    for (var i = 1; i <= 5; i++) out.push(h("i", { key: i, "data-on": i <= (props.fit || 3) ? "1" : "0" }));
    return h("span", { className: "stoa-fit", title: "Fit " + (props.fit || 3) + " of 5" }, out);
  }

  function Hero(props) {
    var s = props.state, t = s.totals, terms = s.playbook.terms, cur = s.playbook.currency;
    var steps = [
      { n: t.mapped, label: "Mapped", sub: s.markets.length === 1 ? "1 market" : s.markets.length + " markets" },
      { n: t.tracked, label: "Leads", sub: "from the map" },
      { n: t.open, label: "In play", sub: money(t.open_value, cur) || terms.deals },
      { n: t.won, label: cap(s.playbook.won.label), sub: money(t.won_value, cur) || "" }
    ];
    return h("section", { className: "stoa-hero" },
      h("div", null,
        h("div", { className: "stoa-eyebrow" }, "Stoa", h("span", null, "//"), s.playbook.title,
          h("span", { className: "stoa-live", "data-off": props.live ? "0" : "1" }, h("b"), props.live ? "Live" : "Paused"),
          h("button", { className: "stoa-mode", onClick: props.flip, title: "Switch between light and dark" }, props.mode === "dark" ? "Light" : "Dark")),
        h("h1", { className: "stoa-title" }, h("span", null, money(t.open_value, cur) || String(t.open)), h("em", null, "in play")),
        h("p", { className: "stoa-sub" },
          h("span", null, h("b", null, t.open), " open"), h("span", null, h("b", null, t.due), " due"),
          h("span", null, h("b", null, t.stale), " quiet"), h("span", null, h("b", null, t.moves_week), " moved this week"))),
      h("div", { className: "stoa-funnel" }, steps.map(function (step, i) {
        return h("div", { className: "stoa-step", key: i, "data-on": step.n ? "1" : "0" },
          h("div", { className: "stoa-node" }, i + 1),
          h("div", { className: "stoa-num" }, step.n),
          h("small", null, step.label), h("span", null, step.sub));
      })));
  }

  function Attention(props) {
    var s = props.state, open = props.open;
    var due = s.deals.filter(function (d) { return d.due; }).sort(function (a, b) { return (a.next_due || "") < (b.next_due || "") ? -1 : 1; });
    var quiet = s.deals.filter(function (d) { return d.stale && !d.due; });
    var recent = s.activity.filter(function (a) { return a.kind === "stage"; }).slice(0, 5);
    function list(rows, render, empty) {
      return rows.length ? rows.slice(0, 5).map(render) : h("div", { className: "stoa-empty" }, empty);
    }
    return h("section", { className: "stoa-attn" },
      h("div", { className: "stoa-panel" }, h("div", { className: "stoa-h" }, "Due now", h("b", null, due.length)),
        list(due, function (d) {
          return h("button", { className: "stoa-row", key: d.id, onClick: function () { open(d.id); } },
            h("i", { className: "stoa-dot", "data-tone": d.next_due < today() ? "bad" : "warn" }), h("strong", null, d.name),
            h("span", null, d.next_step), h("em", null, d.next_due < today() ? "overdue" : "today"));
        }, "Nothing due.")),
      h("div", { className: "stoa-panel" }, h("div", { className: "stoa-h" }, "Gone quiet", h("b", null, quiet.length)),
        list(quiet, function (d) {
          return h("button", { className: "stoa-row", key: d.id, onClick: function () { open(d.id); } },
            h("i", { className: "stoa-dot", "data-tone": "warn" }), h("strong", null, d.name),
            h("span", null, d.stage_label), h("em", null, ago(d.last_touch || d.updated_at).replace(" ago", "")));
        }, "All active.")),
      h("div", { className: "stoa-panel" }, h("div", { className: "stoa-h" }, "Latest moves", h("b", null, s.totals.moves_week)),
        list(recent, function (a) {
          return h("button", { className: "stoa-row", key: a.id, onClick: function () { open(a.deal); } },
            h("i", { className: "stoa-dot", "data-tone": "grad" }), h("strong", null, a.name), h("span", null, a.text), h("em", null, ago(a.at)));
        }, "No moves yet.")));
  }

  // -- flow (Sankey) ---------------------------------------------------------------------------
  function ribbon(x0, y0, x1, y1, h0, h1) {
    var m = (x0 + x1) / 2;
    return "M" + x0 + "," + y0 + " C" + m + "," + y0 + " " + m + "," + y1 + " " + x1 + "," + y1 +
      " L" + x1 + "," + (y1 + h1) + " C" + m + "," + (y1 + h1) + " " + m + "," + (y0 + h0) + " " + x0 + "," + (y0 + h0) + " Z";
  }
  // The funnel: how many records reach each stage, and what share moves on to the next.
  function Flow(props) {
    var f = props.state.flow, pb = props.state.playbook, cur = pb.currency;
    if (!f || !f.total) return null;
    var steps = f.stages.map(function (s) { return { label: s.label, reached: s.reached, here: s.here, lost: s.lost, value: s.value }; })
      .concat([{ label: f.won_label, reached: f.won, won: true, value: f.won_value }]);
    var W = 1000, H = 184, mid = 96, tall = 92, col = W / steps.length, block = col * 0.5;
    var height = function (step) { return Math.max(3, tall * step.reached / f.total); };
    var closed = f.won + f.lost, parts = [];
    steps.forEach(function (step, i) {
      var x = i * col, hgt = height(step), next = steps[i + 1];
      parts.push(h("rect", { key: "b" + i, className: step.won ? "stoa-fn-won" : "stoa-fn-block", x: x, y: mid - hgt / 2, width: block, height: hgt, rx: 2 }));
      if (next) {
        var after = height(next), right = (i + 1) * col;
        parts.push(h("polygon", { key: "l" + i, className: "stoa-fn-link",
          points: [x + block, mid - hgt / 2, right, mid - after / 2, right, mid + after / 2, x + block, mid + hgt / 2].join(" ") }));
        parts.push(h("text", { key: "p" + i, className: "stoa-fn-pct", x: x + block + (col - block) / 2, y: mid + 4, textAnchor: "middle" },
          step.reached ? Math.round(100 * next.reached / step.reached) + "%" : "–"));
      }
      parts.push(h("text", { key: "t" + i, className: "stoa-flow-label", x: x, y: 11 }, step.label.toUpperCase().slice(0, Math.max(6, Math.floor(col / 7.6)))));
      parts.push(h("text", { key: "n" + i, className: "stoa-fn-num", x: x, y: 32 }, String(step.reached)));
      parts.push(h("text", { key: "s" + i, className: "stoa-flow-count", x: x, y: mid + tall / 2 + 20 },
        step.won ? (step.value ? money(step.value, cur) : "") : step.here + " here" + (step.value ? "  ·  " + money(step.value, cur) : "")));
      if (step.lost) parts.push(h("text", { key: "x" + i, className: "stoa-flow-count", x: x, y: mid + tall / 2 + 33 }, step.lost + " lost here"));
    });
    return h("div", { className: "stoa-flowwrap" },
      h("svg", { className: "stoa-flow", viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "How many records reach each stage, and what share moves on" },
        h("defs", null, h("linearGradient", { id: "stoa-spec", x1: "0", y1: "0", x2: "1", y2: "0" },
          h("stop", { offset: "0", stopColor: "#2f56e8" }), h("stop", { offset: "0.38", stopColor: "#6a3fd6" }),
          h("stop", { offset: "0.72", stopColor: "#d6413f" }), h("stop", { offset: "1", stopColor: "#e6b93a" }))),
        parts),
      h("div", { className: "stoa-flowfoot" },
        h("span", null, h("b", null, closed ? Math.round(100 * f.won / closed) + "%" : "–"), " win rate" + (closed ? " (" + f.won + " of " + closed + " closed)" : "")),
        h("span", null, h("b", null, Math.round(100 * f.won / f.total) + "%"), " of everything that entered has been " + f.won_label.toLowerCase()),
        h("span", null, "Percentages are the share that reached the next stage.")));
  }

  // -- pipeline board ---------------------------------------------------------------------------
  function DealCard(props) {
    var d = props.deal, cur = props.currency;
    return h("button", { className: "stoa-card", "data-moved": props.moved ? "1" : "0", onClick: function () { props.open(d.id); } },
      h("h5", null, h("span", null, d.name), d.value ? h("b", null, money(d.value, cur)) : null),
      h("p", null, [d.company, d.group && d.group !== d.company ? d.group : ""].filter(Boolean).join(" · ") || " "),
      d.next_step ? h("div", { className: "stoa-next" }, h("i", { className: "stoa-dot", style: { marginTop: 5 }, "data-tone": d.due ? (d.next_due < today() ? "bad" : "warn") : "" }),
        h("span", null, d.next_step)) : null,
      (props.tag || d.due || d.stale || d.pending || d.snoozed_until || d.owner) ? h("div", { className: "stoa-chips" },
        props.tag ? h(Chip, null, props.tag) : null,
        d.due ? h(Chip, { tone: d.next_due < today() ? "bad" : "warn" }, d.next_due < today() ? "overdue" : "due today") : null,
        d.stale && !d.due ? h(Chip, { tone: "warn" }, "quiet " + ago(d.last_touch || d.updated_at).replace(" ago", "")) : null,
        d.pending ? h(Chip, { tone: "grad" }, "draft waiting") : null,
        d.snoozed_until && d.snoozed_until > today() ? h(Chip, null, "snoozed") : null,
        d.owner ? h(Chip, null, d.owner) : null) : null);
  }

  function Board(props) {
    var s = props.state, cur = s.playbook.currency, q = props.query.toLowerCase();
    var match = function (d) { return !q || [d.name, d.company, d.group, d.next_step, d.owner].join(" ").toLowerCase().indexOf(q) >= 0; };
    var open = s.deals.filter(function (d) { return d.status === "open" && match(d); });
    var max = Math.max.apply(null, s.playbook.stages.map(function (st) { return open.filter(function (d) { return d.stage === st.id; }).length; }).concat([1]));
    var closed = s.deals.filter(function (d) { return d.status !== "open" && match(d); }).sort(function (a, b) { return a.stage_at < b.stage_at ? 1 : -1; });
    var known = {}; s.playbook.stages.forEach(function (st) { known[st.id] = 1; });
    var strays = open.filter(function (d) { return !known[d.stage]; });
    var columns = s.playbook.stages.concat(strays.length ? [{ id: "~other", label: "Other stages", other: true }] : []);
    if (!s.deals.length) {
      return h("div", { className: "stoa-panel stoa-blank" }, h("h3", null, "Empty desk"),
        h("p", null, "Tell Hermes about a " + s.playbook.terms.deal + ", or try ", h("code", null, "hermes stoa demo"), "."));
    }
    return h("div", null,
      h(Flow, { state: s }),
      h("div", { className: "stoa-board" }, columns.map(function (stage) {
        var here = stage.other ? strays : open.filter(function (d) { return d.stage === stage.id; });
        var value = here.reduce(function (a, d) { return a + (d.value || 0); }, 0);
        return h("div", { className: "stoa-col", key: stage.id },
          h("div", { className: "stoa-colhead" },
            h("div", null, h("h4", null, stage.label), h("em", null, here.length + (value ? " · " + money(value, cur) : ""))),
            h("u", { style: { "--fill": Math.round(100 * here.length / max) + "%" } })),
          here.length ? here.map(function (d) { return h(DealCard, { key: d.id, deal: d, currency: cur, open: props.open, moved: props.moved[d.id], tag: stage.other ? d.stage_label : "" }); })
            : h("div", { className: "stoa-colempty" }, "—"));
      })),
      closed.length ? h("div", { className: "stoa-closed" }, closed.slice(0, 12).map(function (d) {
        return h("button", { className: "stoa-btn", key: d.id, onClick: function () { props.open(d.id); } },
          h("i", { className: "stoa-dot", "data-tone": d.status === "won" ? "good" : "bad" }), d.name,
          h("span", { style: { color: "var(--st-mute)", fontWeight: 500 } }, d.stage_label + (d.lost_reason ? " · " + d.lost_reason : "")));
      })) : null);
  }

  // -- market map -------------------------------------------------------------------------------
  function Sky(props) {
    var segments = props.market.segments, n = Math.max(1, segments.length);
    var W = 1000, H = 330, slot = W / n;
    var golden = Math.PI * (3 - Math.sqrt(5));
    return h("svg", { className: "stoa-sky", viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "Market map" },
      segments.map(function (seg, si) {
        var cx = slot * (si + 0.5), cy = 138;
        var rows = seg.companies.slice().sort(function (a, b) { return b.fit - a.fit; }).slice(0, 14);
        var spread = Math.min(34, (slot / 2 - 30) / Math.sqrt(Math.max(1, rows.length)));
        return h("g", { key: seg.name },
          h("circle", { className: "stoa-ring", cx: cx, cy: cy, r: Math.min(slot / 2 - 12, 112) }),
          rows.map(function (c, i) {
            var r = 11 + c.fit * 3.4, dist, ang;
            if (rows.length <= 7) {  // a few companies: best fit in the middle, the rest evenly around it
              var mid = rows.length >= 5 ? 1 : 0, ring = rows.length - mid, k = i - mid;
              dist = rows.length === 1 || (mid && i === 0) ? 0 : Math.min(slot / 2 - 62, 74 + rows.length * 5);
              ang = (ring === 2 ? Math.PI : -Math.PI / 2) + (2 * Math.PI * k) / Math.max(1, ring);
            } else {
              dist = i === 0 ? 0 : spread * Math.sqrt(i) * 1.55; ang = i * golden + si;
            }
            var x = cx + Math.cos(ang) * dist, y = cy + Math.sin(ang) * dist * 0.82;
            var named = rows.length <= 7 || c.fit >= 5;
            return h("g", { key: c.id, className: "stoa-bub", "data-tracked": c.status === "tracked" ? "1" : "0", "data-on": props.selected === c.id ? "1" : "0",
              style: { animationDelay: (-(i * 0.7 + si)) + "s" }, onClick: function () { props.open(c); } },
              h("title", null, c.company + " · fit " + c.fit + "/5" + (c.why ? " — " + c.why : "")),
              h("circle", { cx: x, cy: y, r: r }),
              h("text", { x: x, y: y + 3.5, textAnchor: "middle" }, initials(c.company)),
              named ? h("text", { className: "stoa-bubname", x: x, y: y + r + 13, textAnchor: "middle" }, c.company.length > 20 ? c.company.slice(0, 19) + "…" : c.company) : null);
          }),
          h("text", { className: "stoa-seglabel", x: cx, y: H - 26, textAnchor: "middle" }, seg.name.toUpperCase().slice(0, Math.max(10, Math.floor(slot / 9)))),
          h("text", { x: cx, y: H - 8, textAnchor: "middle" }, seg.companies.length + (seg.companies.length === 1 ? " company" : " companies")));
      }));
  }

  function Bars(props) {
    var rows = props.rows || [];
    var top = props.max || Math.max.apply(null, rows.map(function (r) { return r.value; }).concat([1]));
    return h("div", { className: "stoa-bars" }, rows.map(function (r, i) {
      return h("div", { className: "stoa-rate", key: i }, h("b", null, r.value + (props.unit || "")),
        h("span", null, r.label, h("i", { style: { width: Math.max(2, Math.round(100 * r.value / top)) + "%" } }), r.note ? h("em", null, r.note) : null));
    }));
  }

  // A second Sankey: each segment on the left, what its companies use today on the right.
  function MarketFlow(props) {
    var market = props.market, pairs = {}, left = {}, right = {}, total = 0, SEP = "\u0001";
    var known = (market.holds || []).map(function (x) { return x.name; });
    market.segments.forEach(function (seg) {
      seg.companies.forEach(function (c) {
        if (!c.uses) return;
        var use = known.filter(function (n) { return n.toLowerCase() === c.uses.toLowerCase(); })[0] || "Other";
        pairs[seg.name + SEP + use] = (pairs[seg.name + SEP + use] || 0) + 1;
        left[seg.name] = (left[seg.name] || 0) + 1; right[use] = (right[use] || 0) + 1; total++;
      });
    });
    var L = market.segments.map(function (x) { return x.name; }).filter(function (n) { return left[n]; });
    var R = known.filter(function (n) { return right[n]; });
    if (right.Other) R.push("Other");
    if (total < 3 || R.length < 2) return null;
    var W = 1000, gap = 12, top = 8, bar = 5, x0 = 270, x1 = 700, most = Math.max(L.length, R.length);
    var u = Math.min(22, (190 - gap * (most - 1)) / total), H = top + total * u + gap * (most - 1) + 10;
    var ly = {}, ry = {}, lo = {}, ro = {}, parts = [], y = top;
    L.forEach(function (n) { ly[n] = y; y += left[n] * u + gap; });
    y = top;
    R.forEach(function (n) { ry[n] = y; y += right[n] * u + gap; });
    L.forEach(function (ln) {
      R.forEach(function (rn) {
        var c = pairs[ln + SEP + rn];
        if (!c) return;
        parts.push(h("path", { key: ln + SEP + rn, className: "stoa-flow-on", d: ribbon(x0 + bar, ly[ln] + (lo[ln] || 0), x1, ry[rn] + (ro[rn] || 0), c * u, c * u) }));
        lo[ln] = (lo[ln] || 0) + c * u; ro[rn] = (ro[rn] || 0) + c * u;
      });
    });
    L.forEach(function (n) {
      parts.push(h("rect", { key: "l" + n, className: "stoa-flow-node", x: x0, y: ly[n], width: bar, height: left[n] * u }));
      parts.push(h("text", { key: "lt" + n, className: "stoa-flow-label", x: x0 - 12, y: ly[n] + left[n] * u / 2 + 3, textAnchor: "end" }, n.toUpperCase().slice(0, 30)));
    });
    R.forEach(function (n) {
      parts.push(h("rect", { key: "r" + n, className: "stoa-flow-node", x: x1, y: ry[n], width: bar, height: right[n] * u }));
      parts.push(h("text", { key: "rt" + n, className: "stoa-flow-label", x: x1 + bar + 12, y: ry[n] + right[n] * u / 2 + 3 }, n.toUpperCase().slice(0, 30) + "  " + right[n]));
    });
    return h("div", { className: "stoa-panel stoa-pad stoa-wide2" }, h("div", { className: "stoa-h" }, "Where the market sits today", h("em", null, "segment → what they use")),
      h("svg", { className: "stoa-flow", viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "Segments and what their companies use today" }, parts));
  }

  function savePage(marketId, share, say) {
    get("/market/" + encodeURIComponent(marketId) + "/page?share=" + (share ? 1 : 0)).then(function (r) {
      var url = URL.createObjectURL(new Blob([r.html], { type: "text/html" }));
      var link = document.createElement("a");
      link.href = url; link.download = r.filename;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
      say("Saved " + r.filename);
    }).catch(function (e) { say(String(e && e.message || e)); });
  }

  function MarketView(props) {
    var s = props.state, q = props.query.toLowerCase();
    if (!s.markets.length) {
      return h("div", { className: "stoa-panel stoa-blank" }, h("h3", null, "No map yet"),
        h("p", null, "Ask Hermes: “We sell X to Y. Map the market.”"));
    }
    var market = s.markets.filter(function (m) { return m.id === props.marketId; })[0] || s.markets[0];
    var n = market.numbers || {}, byId = {};
    market.segments.forEach(function (seg) { seg.companies.forEach(function (c) { byId[c.id] = c; }); });
    var rates = (market.rates || []).filter(function (r) { return r.of; });
    var holds = market.holds || [], calls = market.first_calls || [], bridges = market.bridges || [];
    var regions = market.regions || [], sizes = market.sizes || [], notes = market.implications || [];
    var panels = [];
    if (rates.length) panels.push(h("div", { className: "stoa-panel stoa-pad", key: "rates" },
      h("div", { className: "stoa-h" }, "How rare each requirement is", h("em", null, n.assessed + " checked")),
      h(Bars, { unit: "%", max: 100, rows: rates.map(function (r) { return { label: r.label, value: r.rate, note: r.shown + " of " + r.of }; }) })));
    if (holds.length) panels.push(h("div", { className: "stoa-panel stoa-pad", key: "holds" },
      h("div", { className: "stoa-h" }, "Who holds this market today", h("em", null, "of " + market.holds_of + " placed")),
      h(Bars, { unit: "%", max: 100, rows: holds.map(function (x) { return { label: x.name, value: x.share, note: x.companies.slice(0, 3).join(", ") }; }) })));
    if (calls.length) panels.push(h("div", { className: "stoa-panel stoa-pad", key: "calls" },
      h("div", { className: "stoa-h" }, "First calls", h("em", null, calls.length)),
      calls.map(function (c, i) {
        return h("button", { className: "stoa-row stoa-call", key: c.id, onClick: function () { if (byId[c.id]) props.openTarget(byId[c.id]); } },
          h("em", null, ("0" + (i + 1)).slice(-2)), h("strong", null, c.company), h("span", null, c.why), c.buyer ? h("em", null, c.buyer) : null);
      })));
    var spread = regions.length && regions[0].count > 1;   // bars only say something when places repeat
    if (sizes.length || regions.length) panels.push(h("div", { className: "stoa-panel stoa-pad", key: "where" },
      h("div", { className: "stoa-h" }, "Where they are, how big", h("em", null, regions.length + (regions.length === 1 ? " place" : " places"))),
      sizes.length ? h(Bars, { rows: sizes.map(function (z) { return { label: z.band + " people", value: z.count }; }) }) : null,
      !regions.length ? null : spread ? h(Bars, { rows: regions.slice(0, 5).map(function (r) { return { label: r.name, value: r.count }; }) })
        : h("div", { className: "stoa-imp" }, h("b", null, "IN"), h("span", null, regions.map(function (r) { return r.name; }).join(", ")))));
    if (bridges.length) panels.push(h("div", { className: "stoa-panel stoa-pad", key: "meet" },
      h("div", { className: "stoa-h" }, "Where the segments meet", h("em", null, n.bridges)),
      bridges.map(function (b, i) {
        return h("div", { className: "stoa-imp", key: i }, h("b", null, "↔"), h("span", null, h("strong", null, b.between.join(" and ")), h("small", null, b.companies.join(", "))));
      })));
    return h("div", null,
      h("div", { className: "stoa-panel stoa-mapwrap" },
        h("div", { className: "stoa-maphead" },
          h("div", null, h("h3", null, market.name), market.brief ? h("p", null, market.brief) : null),
          h("div", { className: "stoa-mapside" },
            h("div", { className: "stoa-chips" }, h(Chip, null, market.counts.mapped + " mapped"), h(Chip, { tone: "grad" }, market.counts.tracked + " in pipeline"),
              n.with_signal ? h(Chip, null, n.with_signal + " with a signal") : null, n.buyers_named ? h(Chip, null, n.buyers_named + " buyers named") : null,
              market.counts.dismissed ? h(Chip, null, market.counts.dismissed + " dismissed") : null),
            h("div", { className: "stoa-line" },
              h(Btn, { kind: "ghost", title: "The whole map as one web page, for the team", onClick: function () { savePage(market.id, false, props.say); } }, "Download page"),
              h(Btn, { kind: "ghost", title: "The same page without buyers' names or pipeline status", onClick: function () { savePage(market.id, true, props.say); } }, "Share version")))),
        h(Sky, { market: market, open: props.openTarget, selected: props.selected })),
      (market.reading || notes.length) ? h("div", { className: "stoa-panel stoa-pad stoa-reading" },
        h("div", null, h("div", { className: "stoa-h" }, "Reading the map"),
          String(market.reading || "").split("\n").filter(function (x) { return x.trim(); }).map(function (x, i) { return h("p", { key: i }, x); })),
        h("div", null, h("div", { className: "stoa-h" }, "What this means"),
          notes.map(function (x, i) { return h("div", { className: "stoa-imp", key: i }, h("b", null, ("0" + (i + 1)).slice(-2)), h("span", null, x)); }))) : null,
      panels.length ? h("div", { className: "stoa-grid2" }, panels, h(MarketFlow, { market: market, key: "flow" })) : null,
      h("div", { className: "stoa-tiles" }, market.segments.map(function (seg) {
        var rows = seg.companies.filter(function (c) { return !q || [c.company, c.why, c.location, c.signal, c.uses].join(" ").toLowerCase().indexOf(q) >= 0; });
        if (!rows.length) return null;
        return [h("div", { className: "stoa-segtitle", key: "h" + seg.name }, h("b", null, seg.name), rows.length, seg.description ? h("span", null, seg.description) : null)]
          .concat(rows.map(function (c) {
            var reqs = c.requirements || [], shown = reqs.filter(function (r) { return r.shown; }).length;
            return h("button", { className: "stoa-tile", key: c.id, onClick: function () { props.openTarget(c); } },
              h("h5", null, h("span", null, c.company), h(Fit, { fit: c.fit })),
              c.why ? h("p", null, c.why) : null,
              h("div", { className: "stoa-chips" },
                c.status === "tracked" ? h(Chip, { tone: "grad" }, "in pipeline") : null,
                c.signal ? h(Chip, { tone: "good" }, c.signal.slice(0, 34)) : null,
                reqs.length ? h(Chip, null, shown + " of " + reqs.length + " shown") : null,
                c.uses ? h(Chip, null, "uses " + c.uses.slice(0, 22)) : null,
                c.size ? h(Chip, null, c.size) : null, c.location ? h(Chip, null, c.location) : null));
          }));
      })));
  }

  // -- activity ---------------------------------------------------------------------------------
  function Feed(props) {
    var rows = props.state.activity;
    if (!rows.length) return h("div", { className: "stoa-panel stoa-blank" }, h("h3", null, "Quiet so far"), h("p", null, "Moves, calls and clicks land here."));
    return h("div", { className: "stoa-panel stoa-feed" }, rows.map(function (a) {
      return h("div", { className: "stoa-event", key: a.id },
        h("time", null, ago(a.at)), h("i", { className: "stoa-dot", "data-tone": tone(a.kind) }),
        h("div", null, h("button", { onClick: function () { props.open(a.deal); } }, a.name),
          h("p", null, a.text), h("small", null, a.kind + (a.by ? " · " + a.by : ""))));
    }));
  }

  // -- drawers ----------------------------------------------------------------------------------
  function Shell(props) {
    useEffect(function () {
      function key(e) { if (e.key === "Escape") props.close(); }
      window.addEventListener("keydown", key);
      return function () { window.removeEventListener("keydown", key); };
    }, []);
    return h(React.Fragment, null,
      h("div", { className: "stoa-scrim", onClick: props.close }),
      h("aside", { className: "stoa-drawer", role: "dialog", "aria-label": props.label },
        h("button", { className: "stoa-x", onClick: props.close, "aria-label": "Close" }, "×"), props.children));
  }

  function DealDrawer(props) {
    var s = props.state, pb = s.playbook;
    var _d = useState(null), deal = _d[0], setDeal = _d[1];
    var _n = useState(""), note = _n[0], setNote = _n[1];
    var _k = useState("note"), kind = _k[0], setKind = _k[1];
    // null means "not edited": the field shows the saved value and follows it. Once someone types,
    // their text stays put however often the page refreshes underneath, until they save.
    var _s = useState(null), stepEdit = _s[0], setStep = _s[1];
    var _u = useState(null), dueEdit = _u[0], setDue = _u[1];
    var _b = useState(false), busy = _b[0], setBusy = _b[1];
    var load = useCallback(function () {
      return get("/deal/" + encodeURIComponent(props.id)).then(function (d) { setDeal(d); })
        .catch(function () { props.say("That record is no longer on the desk."); props.close(); });
    }, [props.id]);
    useEffect(function () { load(); }, [props.id, props.stamp]);
    function act(body, done) {
      setBusy(true);
      post("/deal/" + encodeURIComponent(props.id) + "/act", body).then(function (r) {
        if (r && r.notice) props.say(r.notice);
        else if (r && r.parked) props.say("Saved. Draft waiting for Hermes.");
        return props.refresh().then(load).catch(function () {}).then(function () { if (done) done(); });
      }).catch(function (e) { props.say(String(e && e.message || e)); }).then(function () { setBusy(false); });
    }
    if (!deal) return h(Shell, { close: props.close, label: "Record" }, h("header", null, h("p", null, "Loading…")));
    var open = deal.status === "open";
    var step = stepEdit === null ? (deal.next_step || "") : stepEdit, due = dueEdit === null ? (deal.next_due || "") : dueEdit;
    var stray = open && deal.position < 0;
    var facts = [[cap(pb.terms.group), deal.group], [pb.terms.value, money(deal.value, pb.currency)], [cap(pb.terms.owner), deal.owner]]
      .concat(pb.fields.map(function (f) { return [f.label, (deal.fields || {})[f.key]]; }))
      .concat([["Email", deal.email], ["LinkedIn", deal.linkedin], ["Source", deal.source]]).filter(function (p) { return p[1]; });
    return h(Shell, { close: props.close, label: deal.name },
      h("header", null,
        h("div", { className: "stoa-eyebrow" }, pb.terms.deal, deal.status !== "open" ? h(Chip, { tone: deal.status === "won" ? "good" : "bad" }, deal.stage_label) : null,
          deal.due ? h(Chip, { tone: "warn" }, "due") : null, deal.stale ? h(Chip, { tone: "warn" }, "quiet") : null),
        h("h3", null, deal.name), h("p", null, (deal.company && deal.company !== deal.name ? deal.company : deal.group && deal.group !== deal.name ? deal.group : " ")),
        h("div", { className: "stoa-meter" }, pb.stages.map(function (st, i) { return h("i", { key: st.id, "data-on": deal.status === "won" || (open && i <= deal.position) ? "1" : "0" }); })),
        h("div", { className: "stoa-meterlabel" }, h("span", null, deal.stage_label + (deal.lost_reason ? " · " + deal.lost_reason : "")),
          h("span", null, open && deal.position >= 0 ? (deal.position + 1) + " of " + pb.stages.length : "in stage " + ago(deal.stage_at).replace(" ago", "")))),
      h("div", { className: "stoa-body" },
        deal.pending_task ? h("div", { className: "stoa-note" }, h("b", null, "Draft waiting. "), "Ask Hermes: “show me " + deal.name.split(" ")[0] + "”.") : null,
        stray ? h("div", { className: "stoa-note" }, h("b", null, "Stage from another playbook. "), "Ask Hermes to move it to one of this playbook’s stages.") : null,
        facts.length ? h("div", { className: "stoa-facts" }, facts.map(function (p) {
          var v = String(p[1]);
          return h("div", { key: p[0] }, h("small", null, p[0]),
            h("span", null, p[0] === "LinkedIn" ? h("a", { href: /^https?:/.test(v) ? v : "https://" + v, target: "_blank", rel: "noreferrer" }, v.replace(/^https?:\/\/(www\.)?/, "")) : v));
        })) : null,
        open ? h("div", { className: "stoa-sec" }, h("label", null, "Next step"),
          h("div", { className: "stoa-line" },
            h("input", { className: "stoa-input", value: step, placeholder: "Next action", onChange: function (e) { setStep(e.target.value); } }),
            h("input", { className: "stoa-input", type: "date", style: { flex: "none", width: 150 }, value: due, onChange: function (e) { setDue(e.target.value); } }),
            h(Btn, { disabled: busy || (step === (deal.next_step || "") && due === (deal.next_due || "")), onClick: function () { act({ verb: "update", next_step: step, next_due: due }, function () { setStep(null); setDue(null); }); } }, "Save"))) : null,
        h("div", { className: "stoa-sec" }, h("label", null, "Log"),
          h("textarea", { className: "stoa-area", value: note, placeholder: "What happened?", onChange: function (e) { setNote(e.target.value); } }),
          h("div", { className: "stoa-line", style: { marginTop: 8 } },
            h("select", { className: "stoa-select", value: kind, onChange: function (e) { setKind(e.target.value); } },
              ["note", "call", "email", "meeting", "task"].map(function (k) { return h("option", { key: k, value: k }, cap(k)); })),
            h("span", { className: "stoa-grow" }),
            h(Btn, { disabled: busy || !note.trim(), onClick: function () { act({ verb: "log", kind: kind, text: note.trim() }, function () { setNote(""); }); } }, "Add to log"))),
        h("div", { className: "stoa-sec" }, h("label", null, "Activity"),
          (deal.activity || []).map(function (a, i) {
            return h("div", { className: "stoa-time", key: i }, h("i", { className: "stoa-dot", "data-tone": tone(a.kind) }),
              h("div", null, h("p", null, a.text), h("small", null, ago(a.at) + " · " + a.kind + (a.by ? " · " + a.by : ""))));
          }))),
      h("div", { className: "stoa-foot" },
        open && deal.advance ? h(Btn, { kind: "primary", disabled: busy, onClick: function () { act({ verb: "advance", extra: deal.stage }); } }, deal.advance + "  →") : null,
        open ? h("select", { className: "stoa-select", value: "", disabled: busy, onChange: function (e) { if (e.target.value !== "") act({ verb: "lost", extra: e.target.value }); } },
          [h("option", { key: "x", value: "" }, pb.lost.label + "…")].concat(pb.lost.reasons.map(function (r, i) { return h("option", { key: i, value: String(i) }, r); }))) : null,
        open ? h(Btn, { kind: "ghost", disabled: busy, title: "Hide from the quiet list for a week", onClick: function () { act({ verb: "snooze7" }); } }, "Snooze") : null,
        open && s.integrations.enrich && !(deal.fields || {}).enriched ? h(Btn, { kind: "ghost", disabled: busy, title: "People Data Labs lookup (uses one credit)", onClick: function () { act({ verb: "enrich" }); } }, "Enrich") : null,
        !open ? h(Btn, { kind: "primary", disabled: busy, onClick: function () { act({ verb: "reopen" }); } }, "Reopen") : null));
  }

  function TargetDrawer(props) {
    var c = props.target, pb = props.state.playbook;
    var _b = useState(false), busy = _b[0], setBusy = _b[1];
    var people = c.people || [], reqs = c.requirements || [];
    function go(verb) {
      setBusy(true);
      post("/target/" + encodeURIComponent(c.id) + "/" + verb, {}).then(function (r) {
        if (verb === "track") props.say(!r.ok ? (r.notice || "Could not add it.") : people.length ? c.company + " added, with " + people[0].name + " as the contact." : c.company + " added. Hermes will find the buyer.");
        else props.say(c.company + " dismissed.");
        props.close(); return props.refresh().then(function () { if (verb === "track" && r.deal) props.openDeal(r.deal); });
      }).catch(function (e) { props.say(String(e && e.message || e)); setBusy(false); });
    }
    function web(url) { return /^https?:\/\//.test(url) ? url : "https://" + url; }
    var facts = [["Segment", c.segment], ["Also fits", c.also], ["Uses today", c.uses], ["Size", c.size], ["Location", c.location], ["Website", c.domain],
      ["Signal", c.signal]].filter(function (p) { return p[1]; });
    return h(Shell, { close: props.close, label: c.company },
      h("header", null, h("div", { className: "stoa-eyebrow" }, "Fit", h(Fit, { fit: c.fit }), c.status === "tracked" ? h(Chip, { tone: "grad" }, "in pipeline") : null),
        h("h3", null, c.company), h("p", null, [c.size, c.location].filter(Boolean).join(" · ") || " ")),
      h("div", { className: "stoa-body" },
        c.why ? h("div", { className: "stoa-sec" }, h("label", null, "Why it fits"), h("p", { className: "stoa-quote" }, "“" + c.why + "”")) : null,
        h("div", { className: "stoa-facts" }, facts.map(function (p) {
          return h("div", { key: p[0] }, h("small", null, p[0]), h("span", null, p[0] === "Website" ? h("a", { href: web(p[1]), target: "_blank", rel: "noreferrer" }, p[1]) : p[1]));
        })),
        reqs.length ? h("div", { className: "stoa-sec" }, h("label", null, "Requirements"), reqs.map(function (r, i) {
          return h("div", { className: "stoa-check", "data-on": r.shown ? "1" : "0", key: i }, h("i", null, r.shown ? "■" : "□"),
            h("div", null, h("span", null, r.label), r.evidence ? h("small", null, r.evidence) : null));
        })) : null,
        people.length ? h("div", { className: "stoa-sec" }, h("label", null, "Buyers"), people.map(function (p, i) {
          return h("div", { className: "stoa-check", "data-on": "1", key: i }, h("i", null, i + 1),
            h("div", null, h("span", null, p.name + (p.title ? " · " + p.title : "")), p.why ? h("small", null, p.why) : null,
              p.linkedin ? h("small", null, h("a", { href: web(p.linkedin), target: "_blank", rel: "noreferrer" }, "LinkedIn")) : null));
        })) : null,
        c.source ? h("div", { className: "stoa-sec" }, h("label", null, "Evidence"),
          /^https?:\/\//.test(c.source) ? h("a", { href: c.source, target: "_blank", rel: "noreferrer", style: { wordBreak: "break-all" } }, c.source) : h("span", null, c.source)) : null,
        c.status === "tracked" ? null : h("div", { className: "stoa-note" }, "Enters at ", h("b", null, pb.stages[0].label),
          people.length ? ", with " + people[0].name + " as the contact." : ". Hermes finds the buyer.")),
      h("div", { className: "stoa-foot" },
        c.status === "tracked"
          ? h(Btn, { kind: "primary", onClick: function () { props.close(); if (c.deal) props.openDeal(c.deal); } }, "Open in pipeline  →")
          : [h(Btn, { key: "t", kind: "primary", disabled: busy, onClick: function () { go("track"); } }, "Add to pipeline  →"),
            h(Btn, { key: "d", kind: "ghost", disabled: busy, onClick: function () { go("dismiss"); } }, "Not a fit")]));
  }

  function NewDrawer(props) {
    var pb = props.state.playbook;
    var _f = useState({ name: "", company: "", group: "", value: "", stage: pb.stages[0].id, next_step: "" }), f = _f[0], setF = _f[1];
    var _b = useState(false), busy = _b[0], setBusy = _b[1];
    function field(key, label, extra) {
      return h("div", { className: extra && extra.wide ? "stoa-wide" : "" , key: key}, h("div", { className: "stoa-sec" }, h("label", null, label),
        h("input", Object.assign({ className: "stoa-input", style: { width: "100%" }, value: f[key], onChange: function (e) { var n = Object.assign({}, f); n[key] = e.target.value; setF(n); } }, (extra && extra.input) || {}))));
    }
    function save() {
      setBusy(true);
      post("/deal", { name: f.name.trim(), company: f.company, group: f.group, value: f.value ? Number(f.value) : undefined, stage: f.stage, next_step: f.next_step })
        .then(function (r) { props.say(r.exists ? "Already on the desk." : f.name.trim() + " added."); props.close(); return props.refresh().then(function () { props.openDeal(r.deal.id); }); })
        .catch(function (e) { props.say(String(e && e.message || e)); setBusy(false); });
    }
    return h(Shell, { close: props.close, label: "New record" },
      h("header", null, h("div", { className: "stoa-eyebrow" }, "New " + pb.terms.deal), h("h3", null, f.name || "New"), h("p", null, f.company || " ")),
      h("div", { className: "stoa-body" }, h("div", { className: "stoa-form" },
        field("name", "Name", { wide: true, input: { autoFocus: true, placeholder: "Dana Whitfield" } }),
        field("company", pb.terms.company), field("group", cap(pb.terms.group)),
        field("value", pb.terms.value, { input: { type: "number", min: 0, placeholder: "0" } }),
        h("div", { key: "stage" }, h("div", { className: "stoa-sec" }, h("label", null, "Stage"),
          h("select", { className: "stoa-select", style: { width: "100%" }, value: f.stage, onChange: function (e) { setF(Object.assign({}, f, { stage: e.target.value })); } },
            pb.stages.map(function (st) { return h("option", { key: st.id, value: st.id }, st.label); })))),
        field("next_step", "Next step", { wide: true, input: { placeholder: "Next action" } }))),
      h("div", { className: "stoa-foot" }, h(Btn, { kind: "primary", disabled: busy || !f.name.trim(), onClick: save }, "Add  →")));
  }

  // -- page -------------------------------------------------------------------------------------
  function StoaPage() {
    var _s = useState(null), state = _s[0], setState = _s[1];
    var _e = useState(""), error = _e[0], setError = _e[1];
    var _v = useState("pipeline"), view = _v[0], setView = _v[1];
    var _q = useState(""), query = _q[0], setQuery = _q[1];
    var _m = useState(""), marketId = _m[0], setMarketId = _m[1];
    var _p = useState(null), panel = _p[0], setPanel = _p[1];
    var _t = useState(""), toast = _t[0], setToast = _t[1];
    var _mv = useState({}), moved = _mv[0], setMoved = _mv[1];
    var _l = useState(true), live = _l[0], setLive = _l[1];
    var _md = useState(function () {
      try { var saved = window.localStorage.getItem("stoa.mode"); if (saved === "light" || saved === "dark") return saved; } catch (e) {}
      return isDark() ? "dark" : "light";
    }), mode = _md[0], setMode = _md[1];
    var flip = useCallback(function () {
      setMode(function (m) { var next = m === "dark" ? "light" : "dark"; try { window.localStorage.setItem("stoa.mode", next); } catch (e) {} return next; });
    }, []);
    var stages = useRef(null);
    var timer = useRef(null);

    var refresh = useCallback(function () {
      return get("/state").then(function (next) {
        var now = {}, changed = {};
        next.deals.forEach(function (d) { now[d.id] = d.stage; if (stages.current && stages.current[d.id] !== d.stage) changed[d.id] = true; });
        stages.current = now;
        if (Object.keys(changed).length) { setMoved(changed); setTimeout(function () { setMoved({}); }, 1800); }
        setState(next); setError(""); setLive(true);
      }).catch(function (e) { setError(String(e && e.message || e)); setLive(false); });
    }, []);
    useEffect(function () {
      refresh();
      var id = setInterval(function () { if (!document.hidden) refresh(); }, POLL_MS);
      return function () { clearInterval(id); };
    }, []);
    var say = useCallback(function (text) {
      setToast(text); clearTimeout(timer.current); timer.current = setTimeout(function () { setToast(""); }, 4200);
    }, []);
    var openDeal = useCallback(function (id) { setPanel({ kind: "deal", id: id }); }, []);
    var close = useCallback(function () { setPanel(null); }, []);

    var body;
    if (!state) {
      body = h("div", { className: "stoa-panel stoa-blank" }, h("h3", null, error ? "No answer" : "Opening…"),
        error ? h("p", null, error) : null);
    } else {
      var tabs = [["pipeline", "Pipeline", state.totals.open], ["market", "Market map", state.totals.mapped], ["activity", "Activity", null]];
      body = h(React.Fragment, null,
        h(Hero, { state: state, live: live, mode: mode, flip: flip }),
        h(Attention, { state: state, open: openDeal }),
        h("div", { className: "stoa-bar" },
          h("div", { className: "stoa-seg", role: "tablist" }, tabs.map(function (t) {
            return h("button", { key: t[0], role: "tab", "data-on": view === t[0] ? "1" : "0", onClick: function () { setView(t[0]); } }, t[1], t[2] !== null ? h("i", null, t[2]) : null);
          })),
          view === "market" && state.markets.length > 1 ? h("select", { className: "stoa-select", value: marketId || state.markets[0].id, onChange: function (e) { setMarketId(e.target.value); } },
            state.markets.map(function (m) { return h("option", { key: m.id, value: m.id }, m.name); })) : null,
          h("span", { className: "stoa-grow" }),
          view !== "activity" ? h("input", { className: "stoa-input", style: { width: 220 }, placeholder: "Filter…", value: query, onChange: function (e) { setQuery(e.target.value); } }) : null,
          h(Btn, { kind: "primary", onClick: function () { setPanel({ kind: "new" }); } }, "New")),
        view === "pipeline" ? h(Board, { state: state, query: query, open: openDeal, moved: moved }) : null,
        view === "market" ? h(MarketView, { state: state, query: query, marketId: marketId, say: say, selected: panel && panel.kind === "target" ? panel.target.id : "",
          openTarget: function (c) { setPanel({ kind: "target", target: c }); } }) : null,
        view === "activity" ? h(Feed, { state: state, open: openDeal }) : null,
        panel && panel.kind === "deal" ? h(DealDrawer, { key: panel.id, id: panel.id, state: state, stamp: state.stamp, close: close, refresh: refresh, say: say }) : null,
        panel && panel.kind === "target" ? h(TargetDrawer, { key: panel.target.id, target: panel.target, state: state, close: close, refresh: refresh, say: say, openDeal: openDeal }) : null,
        panel && panel.kind === "new" ? h(NewDrawer, { state: state, close: close, refresh: refresh, say: say, openDeal: openDeal }) : null);
    }
    return h("div", { className: "stoa", "data-mode": mode },
      h("div", { className: "stoa-aurora", "aria-hidden": "true" }, h("i"), h("i"), h("i"), h("i")),
      body,
      toast ? h("div", { className: "stoa-toast", role: "status" }, toast) : null);
  }

  window.__HERMES_PLUGINS__.register("stoa", StoaPage);
})();
