"""A market map as one self-contained web page: no scripts, no outside requests, nothing to host.

``render(summary, terms, share)`` takes what ``Markets.summary`` computed and returns HTML. With
``share`` the page leaves out buyers' names and what is in the pipeline, so it can be sent on.
"""
from __future__ import annotations

import math
from html import escape as esc

CSS = """
:root { --bg:#ffffff; --ink:#1b1c20; --soft:#4b4e58; --mute:#8a8e9b; --line:#e5e7ec; --card:#f6f7f9; --pale:#d9d8d0; --olive:#667338; color-scheme: light; }
@media (prefers-color-scheme: dark) { :root { --bg:#0a0b0d; --ink:#e8e6de; --soft:#b3b1aa; --mute:#7d7e80; --line:#24262a; --card:#121316; --pale:#f1efe7; --olive:#7f8c50; color-scheme: dark; } }
* { box-sizing: border-box; } html, body { margin: 0; background: var(--bg); color: var(--ink); }
body { font: 15px/1.5 Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; max-width: 1060px; margin: 0 auto; padding: 44px 32px 80px; -webkit-font-smoothing: antialiased; }
.mono, th, .eyebrow, .stat small, .bar em, .tag { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px; letter-spacing: .12em; text-transform: uppercase; color: var(--mute); font-style: normal; font-weight: 400; }
h1 { font: 400 clamp(40px, 6vw, 72px)/.98 Didot, "Bodoni 72", "Playfair Display", Georgia, serif; letter-spacing: -.02em; margin: 18px 0 14px; }
h2 { font-size: 13px; font-weight: 600; letter-spacing: -.005em; margin: 0 0 14px; padding-top: 14px; border-top: 1px solid var(--ink); }
p { margin: 0 0 10px; color: var(--soft); max-width: 72ch; } a { color: inherit; text-underline-offset: 3px; text-decoration-color: var(--mute); }
section { margin-top: 44px; } .rule { height: 2px; margin: 28px 0 0; border-radius: 2px; background: linear-gradient(90deg, var(--pale), #8f8f87 46%, var(--olive)); }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 0; border-top: 1px solid var(--line); margin-top: 26px; }
.stat { padding: 16px 16px 4px 0; } .stat b { display: block; font: 400 40px/1 Didot, "Bodoni 72", Georgia, serif; font-variant-numeric: lining-nums; } .stat small { display: block; margin-top: 8px; color: var(--ink); }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 40px; } @media (max-width: 760px) { .two { grid-template-columns: 1fr; } body { padding: 28px 18px 60px; } }
.bar { display: grid; grid-template-columns: 46px 1fr; gap: 10px; align-items: center; padding: 7px 0; border-bottom: 1px solid var(--line); }
.bar b { font-weight: 500; font-variant-numeric: tabular-nums; } .bar span { display: block; } .bar i { display: block; height: 3px; margin-top: 5px; background: var(--ink); border-radius: 2px; }
.bar em { display: block; margin-top: 3px; text-transform: none; letter-spacing: 0; font-family: inherit; font-size: 12.5px; }
table { width: 100%; border-collapse: collapse; } table.co { table-layout: fixed; } table.co th:nth-child(1) { width: 21%; } table.co th:nth-child(2) { width: 9%; }
table.co th:nth-child(3) { width: 36%; } table.co th:nth-child(4) { width: 15%; } th { text-align: left; padding: 0 12px 8px 0; border-bottom: 1px solid var(--line); }
td { vertical-align: top; padding: 11px 12px 11px 0; border-bottom: 1px solid var(--line); font-size: 13.5px; color: var(--soft); } td strong { color: var(--ink); font-weight: 600; }
.fit { white-space: nowrap; letter-spacing: 1px; color: var(--ink); } .tag { display: inline-block; border: 1px solid var(--line); border-radius: 4px; padding: 1px 6px; margin: 3px 4px 0 0; font-size: 10px; }
.tag.on { background: var(--ink); color: var(--bg); border-color: var(--ink); }
ol.calls { margin: 0; padding: 0; list-style: none; counter-reset: c; } ol.calls li { counter-increment: c; display: grid; grid-template-columns: 34px 1fr; padding: 12px 0; border-bottom: 1px solid var(--line); }
ol.calls li::before { content: counter(c, decimal-leading-zero); font: 11px ui-monospace, Menlo, monospace; color: var(--mute); padding-top: 3px; } ol.calls strong { font-weight: 600; }
svg { width: 100%; height: auto; display: block; } svg text { font: 10.5px ui-monospace, Menlo, monospace; letter-spacing: .12em; fill: var(--mute); }
svg .name { font: 500 10.5px Inter, system-ui, sans-serif; letter-spacing: 0; fill: var(--soft); } svg .seg { fill: var(--ink); }
svg circle.ring { fill: none; stroke: var(--line); } svg circle.co { fill: var(--bg); stroke: var(--ink); } svg circle.in { fill: var(--ink); }
svg .ini { fill: var(--ink); letter-spacing: .06em; font-size: 9.5px; } svg .ini.in { fill: var(--bg); }
footer { margin-top: 56px; padding-top: 14px; border-top: 1px solid var(--line); color: var(--mute); font-size: 12.5px; }
"""


def _initials(name: str) -> str:
    words = "".join(ch if ch.isalnum() or ch == " " else " " for ch in name).split()
    if not words:
        return "?"
    return (words[0][0] + (words[1][0] if len(words) > 1 else words[0][1:2])).upper()


def _sky(segments: list, share: bool) -> str:
    """The bubble map: one cluster per segment, each company sized by fit, filled once it is in the pipeline."""
    count = max(1, len(segments))
    width, height, slot = 1000, 330, 1000 / count
    golden = math.pi * (3 - math.sqrt(5))
    out = ['<svg viewBox="0 0 %d %d" role="img" aria-label="Market map">' % (width, height)]
    for si, seg in enumerate(segments):
        cx, cy = slot * (si + 0.5), 138
        rows = sorted(seg["companies"], key=lambda c: -c["fit"])[:14]
        out.append('<circle class="ring" cx="%.1f" cy="%d" r="%.1f"/>' % (cx, cy, min(slot / 2 - 12, 112)))
        spread = min(34, (slot / 2 - 30) / math.sqrt(max(1, len(rows))))
        for i, c in enumerate(rows):
            radius = 11 + c["fit"] * 3.4
            if len(rows) <= 7:
                mid = 1 if len(rows) >= 5 else 0
                ring, k = len(rows) - mid, i - mid
                dist = 0 if len(rows) == 1 or (mid and i == 0) else min(slot / 2 - 62, 74 + len(rows) * 5)
                angle = (math.pi if ring == 2 else -math.pi / 2) + (2 * math.pi * k) / max(1, ring)
            else:
                dist, angle = (0 if i == 0 else spread * math.sqrt(i) * 1.55), i * golden + si
            x, y = cx + math.cos(angle) * dist, cy + math.sin(angle) * dist * 0.82
            tracked = c.get("status") == "tracked" and not share
            out.append('<circle class="co%s" cx="%.1f" cy="%.1f" r="%.1f"><title>%s · fit %d/5</title></circle>'
                       % (" in" if tracked else "", x, y, radius, esc(c["company"]), c["fit"]))
            out.append('<text class="ini%s" x="%.1f" y="%.1f" text-anchor="middle">%s</text>'
                       % (" in" if tracked else "", x, y + 3.5, esc(_initials(c["company"]))))
            if len(rows) <= 7 or c["fit"] >= 5:
                name = c["company"] if len(c["company"]) <= 20 else c["company"][:19] + "…"
                out.append('<text class="name" x="%.1f" y="%.1f" text-anchor="middle">%s</text>' % (x, y + radius + 13, esc(name)))
        out.append('<text class="seg" x="%.1f" y="%d" text-anchor="middle">%s</text>'
                   % (cx, height - 26, esc(seg["segment"].upper()[:max(10, int(slot / 9))])))
        out.append('<text x="%.1f" y="%d" text-anchor="middle">%d %s</text>'
                   % (cx, height - 8, seg["count"], "COMPANY" if seg["count"] == 1 else "COMPANIES"))
    out.append("</svg>")
    return "".join(out)


def _bars(rows: list, label, value, note=None, total=None) -> str:
    """Rows of ``value  label`` with a thin bar scaled to the largest value (or to ``total``)."""
    top = total or max([value(r) for r in rows] + [1])
    out = []
    for r in rows:
        extra = "<em>%s</em>" % esc(note(r)) if note and note(r) else ""
        out.append('<div class="bar"><b>%s</b><span>%s<i style="width:%d%%"></i>%s</span></div>'
                   % (esc(str(value(r)) + ("%" if total == 100 else "")), esc(label(r)), max(2, round(100 * value(r) / top)), extra))
    return "".join(out)


def _link(url: str, text: str = "") -> str:
    if not url:
        return ""
    href = url if url.startswith(("http://", "https://")) else "https://" + url
    return '<a href="%s" rel="noreferrer noopener">%s</a>' % (esc(href, quote=True), esc(text or url))


def render(summary: dict, terms: dict, share: bool = False) -> str:
    market, numbers = summary["market"], summary["numbers"]
    date = (market.get("updated_at") or "")[:10]
    stats = [(numbers["companies"], "Companies"), (numbers["segments"], "Segments"), (numbers["with_signal"], "With a signal"),
             (numbers["avg_fit"], "Average fit")]
    if not share:
        stats += [(numbers["buyers_named"], "Buyers named"), (numbers["in_pipeline"], "In the pipeline")]
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
           "<title>%s · Market map</title><style>%s</style></head><body>" % (esc(market["name"]), CSS),
           '<div class="eyebrow">Market map%s</div><h1>%s</h1>' % (" // " + esc(date) if date else "", esc(market["name"]))]
    if market.get("brief"):
        out.append("<p>%s</p>" % esc(market["brief"]))
    out.append('<div class="stats">%s</div><div class="rule"></div>' % "".join(
        '<div class="stat"><b>%s</b><small>%s</small></div>' % (esc(str(v)), esc(l)) for v, l in stats))

    if market.get("reading") or market.get("implications"):
        out.append('<section class="two"><div><h2>Reading the map</h2>%s</div>' % "".join(
            "<p>%s</p>" % esc(par) for par in (market.get("reading") or "").split("\n") if par.strip()))
        out.append("<div><h2>What this means</h2>%s</div></section>" % "".join(
            '<div class="bar" style="grid-template-columns:28px 1fr"><b class="mono">%02d</b><span>%s</span></div>' % (i + 1, esc(x))
            for i, x in enumerate(market.get("implications") or [])))

    if summary["segments"]:
        out.append("<section><h2>The map</h2>%s</section>" % _sky(summary["segments"], share))
        rows = "".join(
            "<tr><td><strong>%s</strong><br>%s</td><td>%d (%d%%)</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (esc(s["segment"]), esc(s["description"]), s["count"], s["share"], esc(str(s["avg_fit"])),
               esc(", ".join(s["regions"])), esc(", ".join(s["uses"])))
            for s in summary["segments"])
        out.append("<section><h2>The segments</h2><table><tr><th>Segment</th><th>Share</th><th>Avg fit</th><th>Where</th>"
                   "<th>Use today</th></tr>%s</table></section>" % rows)

    panels = []
    checked = [r for r in summary["requirements"] if r["of"]]
    if checked:
        panels.append("<div><h2>How rare each requirement is</h2><p>Share of the companies checked for each one that show it.</p>%s</div>"
                      % _bars(checked, lambda r: r["label"], lambda r: r["rate"],
                              note=lambda r: "%d of %d checked" % (r["shown"], r["of"]), total=100))
    if summary["holds"]:
        panels.append("<div><h2>Who holds this market today</h2><p>What the %d companies we could place use now.</p>%s</div>"
                      % (summary["holds_of"], _bars(summary["holds"], lambda r: r["name"], lambda r: r["share"],
                                                    note=lambda r: ", ".join(r["companies"][:4]), total=100)))
    if summary["regions"]:
        panels.append("<div><h2>Where they are</h2>%s</div>" % _bars(summary["regions"], lambda r: r["name"], lambda r: r["count"]))
    if summary["sizes"]:
        panels.append("<div><h2>How big they are</h2>%s</div>" % _bars(summary["sizes"], lambda r: r["band"] + " people", lambda r: r["count"]))
    for i in range(0, len(panels), 2):
        out.append('<section class="two">%s</section>' % "".join(panels[i:i + 2]))

    if summary["bridges"]:
        out.append("<section><h2>Where the segments meet</h2><p>Companies that could sit in two segments: the natural second pool "
                   "when one segment runs thin.</p><table><tr><th>Between</th><th>Companies</th></tr>%s</table></section>" % "".join(
                       "<tr><td><strong>%s ↔ %s</strong></td><td>%s</td></tr>"
                       % (esc(b["between"][0]), esc(b["between"][1]), esc(", ".join(b["companies"]))) for b in summary["bridges"]))

    calls = summary["first_calls"]
    if calls:
        out.append('<section><h2>First calls</h2><ol class="calls">%s</ol></section>' % "".join(
            "<li><div><strong>%s</strong> <span class=\"mono\">%s</span><br>%s%s</div></li>"
            % (esc(c["company"]), esc(c["segment"]), esc(c["why"]),
               "<br><span class=\"mono\">Buyer: %s</span>" % esc(c["buyer"]) if c.get("buyer") and not share else "")
            for c in calls))

    for seg in summary["segments"]:
        rows = []
        for c in seg["companies"]:
            tags = "".join('<span class="tag%s">%s</span>' % (" on" if r["shown"] else "", esc(r["label"])) for r in c.get("requirements") or [])
            if c.get("status") == "tracked" and not share:
                tags += '<span class="tag on">In the pipeline</span>'
            people = ""
            if not share and c.get("people"):
                people = "<br>".join(esc(" · ".join(b for b in (p.get("name"), p.get("title")) if b)) for p in c["people"][:3])
            also = "<br><span class=\"mono\">Also fits %s</span>" % esc(c["also"]) if c.get("also") else ""
            rows.append(
                "<tr><td><strong>%s</strong><br>%s%s</td><td class=\"fit\">%s</td><td>%s%s</td><td>%s%s</td><td>%s</td></tr>"
                % (esc(c["company"]), _link(c.get("domain", "")), also,
                   "■" * c["fit"] + "□" * (5 - c["fit"]),
                   esc(c.get("why", "")), "<br>" + tags if tags else "",
                   esc(" · ".join(b for b in (c.get("size"), c.get("location")) if b)),
                   "<br><span class=\"mono\">Uses %s</span>" % esc(c["uses"]) if c.get("uses") else "",
                   "<br>".join(x for x in (
                       esc(c.get("signal", "")),
                       _link(c["source"], "Source") if str(c.get("source", "")).startswith(("http://", "https://")) else esc(c.get("source", "")),
                       people) if x)))
        out.append("<section><h2>%s · %d</h2><table class=\"co\"><tr><th>Company</th><th>Fit</th><th>Why it fits</th><th>Size and place</th>"
                   "<th>Signal%s</th></tr>%s</table></section>"
                   % (esc(seg["segment"]), seg["count"], "" if share else " and buyers", "".join(rows)))

    out.append("<footer>Compiled with Stoa for Hermes Agent%s from public sources. Fit is a judgement against the brief above, "
               "and each row carries the evidence for it. This is a picture of a market, not a census of it; nobody on it was "
               "contacted to make it.</footer></body></html>" % (" on " + esc(date) if date else ""))
    return "".join(out)
