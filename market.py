"""Market maps: who is in a market, sorted into segments, and which of them are worth pursuing.

A map is the step before the pipeline. The agent researches a market (with its own web tools,
or Exa when a key is set), records companies here with the evidence for each, and the team turns
the ones they like into pipeline records with one click. No Hermes imports.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

from . import sources
from . import store as st

EXA_KEY = "EXA_API_KEY"
PDL_KEY = "PDL_API_KEY"
STATUSES = ("new", "tracked", "dismissed")
MAP_CARD = "map:"  # cards.deal_id prefix for a posted map message
FIELDS = ("domain", "segment", "size", "location", "why", "signal", "source")
ACCOUNT_WORDS = ("account", "client", "company", "customer", "buyer")


def _norm(company: str) -> str:
    name = re.sub(r"[^a-z0-9 ]+", " ", (company or "").lower())
    name = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|gmbh|plc|sa|ag)\b", " ", name)
    return " ".join(name.split())


def target_id(market_id: str, company: str) -> str:
    return "t" + hashlib.sha1(("%s|%s" % (market_id, _norm(company))).encode()).hexdigest()[:10]


def dots(fit: int) -> str:
    fit = max(1, min(int(fit or 3), 5))
    return "●" * fit + "○" * (5 - fit)


class Markets:
    def __init__(self, desk):
        self.desk = desk

    @property
    def store(self) -> st.Store:
        return self.desk.store

    # -- storage --------------------------------------------------------------------------------
    def _market(self, ref: Any) -> Optional[dict]:
        ref = str(ref or "").strip()
        with self.store._conn() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM markets ORDER BY updated_at DESC").fetchall()]
        if not rows:
            return None
        if not ref:
            return _unpack(rows[0]) if len(rows) == 1 else None
        slug = st.slugify(ref)
        for row in rows:
            if row["id"] in (ref, slug) or row["name"].lower() == ref.lower():
                return _unpack(row)
        close = [r for r in rows if ref.lower() in r["name"].lower()]
        return _unpack(close[0]) if len(close) == 1 else None

    def _missing(self, ref: Any) -> dict:
        names = [m["name"] for m in self.list_markets()]
        if not names:
            return {"ok": False, "error": "no market maps yet; start one with gtm_market(action='define')"}
        return {"ok": False, "error": "which market? pass `market`" if not ref else "no market map matches '%s'" % ref,
                "markets": names}

    def list_markets(self) -> list:
        with self.store._conn() as conn:
            rows = conn.execute(
                "SELECT m.*, (SELECT count(*) FROM targets t WHERE t.market_id = m.id AND t.status != 'dismissed') AS companies, "
                "(SELECT count(*) FROM targets t WHERE t.market_id = m.id AND t.status = 'tracked') AS tracked "
                "FROM markets m ORDER BY m.updated_at DESC").fetchall()
        return [{"id": r["id"], "name": r["name"], "brief": r["brief"], "companies": r["companies"], "in_pipeline": r["tracked"]}
                for r in rows]

    def targets(self, market_id: str, status: str = "", segment: str = "") -> list:
        sql, params = "SELECT * FROM targets WHERE market_id = ?", [market_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if segment:
            sql += " AND lower(segment) LIKE ?"
            params.append("%" + segment.lower() + "%")
        with self.store._conn() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY fit DESC, company", params).fetchall()]

    def target(self, ref: str, market_id: str = "") -> Optional[dict]:
        ref = str(ref or "").strip()
        with self.store._conn() as conn:
            row = conn.execute("SELECT * FROM targets WHERE id = ?", (ref,)).fetchone()
            if row is None:
                sql, params = "SELECT * FROM targets WHERE (lower(company) = lower(?) OR lower(domain) = lower(?))", [ref, ref]
                if market_id:
                    sql += " AND market_id = ?"
                    params.append(market_id)
                rows = conn.execute(sql + " ORDER BY status = 'dismissed', updated_at DESC", params).fetchall()
                row = rows[0] if rows else None
        return dict(row) if row else None

    # -- actions --------------------------------------------------------------------------------
    def run(self, args: dict, session: Optional[dict] = None) -> dict:
        action = str(args.get("action") or "show").lower()
        handler = {"define": self.define, "list": self.list_action, "add": self.add, "show": self.show, "track": self.track,
                   "dismiss": self.dismiss, "search": self.search, "people": self.people}.get(action)
        if action == "post":
            return self.post(args, session or {})
        if handler is None:
            return {"ok": False, "error": "unknown action '%s'" % action,
                    "actions": ["define", "list", "add", "show", "search", "people", "track", "dismiss", "post"]}
        return handler(args)

    def define(self, args: dict) -> dict:
        name = str(args.get("name") or args.get("market") or "").strip()
        if not name:
            return {"ok": False, "error": "define needs a name, for example 'Commercial solar installers, US Southwest'"}
        existing = self._market(name)
        segments = []
        for raw in args.get("segments") or []:
            if isinstance(raw, str):
                raw = {"name": raw}
            if isinstance(raw, dict) and raw.get("name"):
                segments.append({"name": str(raw["name"])[:60], "description": str(raw.get("description") or "")[:240]})
        stamp = st.now()
        with self.store._conn() as conn:
            if existing:
                conn.execute("UPDATE markets SET brief = ?, segments = ?, updated_at = ? WHERE id = ?",
                             (str(args.get("brief") or existing["brief"])[:1200],
                              json.dumps(segments or existing["segments"]), stamp, existing["id"]))
                market_id = existing["id"]
            else:
                market_id, n = st.slugify(name), 1
                while conn.execute("SELECT 1 FROM markets WHERE id = ?", (market_id,)).fetchone():
                    n += 1
                    market_id = st.slugify(name)[:st.ID_MAX - 3] + "-%d" % n
                conn.execute("INSERT INTO markets (id, name, brief, segments, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                             (market_id, name[:120], str(args.get("brief") or "")[:1200], json.dumps(segments), stamp, stamp))
        market = self._market(market_id)
        return {"ok": True, "market": {"id": market["id"], "name": market["name"], "brief": market["brief"],
                                       "segments": market["segments"]}, "updated": bool(existing),
                "next": "Research companies for each segment, then gtm_market(action='add') with the evidence for each."}

    def list_action(self, args: dict) -> dict:
        return {"ok": True, "markets": self.list_markets()}

    def add(self, args: dict) -> dict:
        market = self._market(args.get("market"))
        if market is None:
            return self._missing(args.get("market"))
        companies = args.get("companies") or []
        if isinstance(companies, dict):
            companies = [companies]
        added, updated, problems = [], [], []
        stamp = st.now()
        with self.store._conn() as conn:
            for raw in companies[:60]:
                if isinstance(raw, str):
                    raw = {"company": raw}
                company = str((raw or {}).get("company") or (raw or {}).get("name") or "").strip()
                if not company:
                    problems.append("an entry had no company name")
                    continue
                values = {k: " ".join(str(raw.get(k) or "").split())[:400] for k in FIELDS}
                values["domain"] = sources.domain_of(values["domain"] or str(raw.get("website") or ""))
                fit = raw.get("fit")
                tid = target_id(market["id"], company)
                row = conn.execute("SELECT * FROM targets WHERE id = ?", (tid,)).fetchone()
                if row:
                    sets = {k: v for k, v in values.items() if v and v != row[k]}
                    if isinstance(fit, (int, float)):
                        sets["fit"] = max(1, min(int(fit), 5))
                    if sets:
                        conn.execute("UPDATE targets SET %s, updated_at = ? WHERE id = ?" % ", ".join("%s = ?" % k for k in sets),
                                     list(sets.values()) + [stamp, tid])
                    updated.append(company)
                else:
                    conn.execute(
                        "INSERT INTO targets (id, market_id, company, domain, segment, fit, size, location, why, signal, source, "
                        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (tid, market["id"], company[:120], values["domain"], values["segment"],
                         max(1, min(int(fit), 5)) if isinstance(fit, (int, float)) else 3, values["size"], values["location"],
                         values["why"], values["signal"], values["source"], stamp, stamp))
                    added.append(company)
            conn.execute("UPDATE markets SET updated_at = ? WHERE id = ?", (stamp, market["id"]))
        out = {"ok": True, "market": market["name"], "added": added, "updated": updated,
               "total": len(self.targets(market["id"]))}
        if problems:
            out["problems"] = problems
        self._refresh(market["id"])
        return out

    def show(self, args: dict) -> dict:
        market = self._market(args.get("market"))
        if market is None:
            return self._missing(args.get("market"))
        status = str(args.get("status") or "").lower()
        rows = self.targets(market["id"], status if status in STATUSES else "", str(args.get("segment") or ""))
        if not status:
            rows = [r for r in rows if r["status"] != "dismissed"]
        return {"ok": True, "market": {"id": market["id"], "name": market["name"], "brief": market["brief"]},
                "map": map_text(market, rows),
                "segments": [{"segment": name, "companies": [_view(r) for r in group]} for name, group in by_segment(market, rows)]}

    def search(self, args: dict) -> dict:
        key = self.desk.secret(EXA_KEY)
        if not key:
            return {"ok": False, "error": "Exa is not set up (EXA_API_KEY), so use your own web search tool to find companies, "
                                          "then record them with gtm_market(action='add')."}
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "search needs a query describing the kind of company"}
        result = sources.exa_companies(key, query, int(args.get("limit") or 8), http=self.desk.http)
        if result.get("ok"):
            result["note"] = ("These are candidates, not yet on the map. Judge each against the market brief and add the ones "
                              "that fit with gtm_market(action='add'), giving the reason and the source URL.")
        return result

    def people(self, args: dict) -> dict:
        key = self.desk.secret(PDL_KEY)
        if not key:
            return {"ok": False, "error": "People Data Labs is not set up (PDL_API_KEY). Use your web search tool to find the "
                                          "right contact instead."}
        target = self.target(str(args.get("target") or args.get("company") or ""))
        company = target["company"] if target else str(args.get("company") or "")
        domain = target["domain"] if target else sources.domain_of(str(args.get("website") or ""))
        limit = max(1, min(int(args.get("limit") or 3), 5))
        month = "pdl_calls:" + st.today()[:7]
        cap = int(self.desk.setting("pdl_monthly_cap", 50) or 0)
        used = int(self.store.meta_get(month, "0") or 0)
        if cap and used + limit > cap:
            return {"ok": False, "error": "This would pass the monthly People Data Labs cap (%d used of %d). Raise "
                                          "pdl_monthly_cap in the plugin's settings to continue." % (used, cap)}
        titles = args.get("titles") or []
        if isinstance(titles, str):
            titles = [t.strip() for t in titles.split(",")]
        result = sources.pdl_people(key, company=company, domain=domain, titles=titles, limit=limit, http=self.desk.http)
        if result.get("ok") and result.get("credits_used"):
            self.store.bump(month, result["credits_used"])
        if result.get("ok"):
            result["company"] = company
        return result

    def track(self, args: dict, actor: str = "") -> dict:
        """Turn targets into pipeline records at the playbook's first stage."""
        market = self._market(args.get("market"))
        refs = args.get("targets") or ([args["target"]] if args.get("target") else [])
        if isinstance(refs, str):
            refs = [refs]
        if not refs:
            return {"ok": False, "error": "track needs `targets`: ids or company names from the map"}
        playbook = self.desk.playbook()
        actor = actor or self.desk.actor()
        made, already, unknown = [], [], []
        for ref in refs[:25]:
            target = self.target(ref, market["id"] if market else "")
            if target is None:
                unknown.append(str(ref))
                continue
            if target["status"] == "tracked" and target["deal_id"] and self.store.get(target["deal_id"]):
                already.append(target["company"])
                continue
            owner_market = self._market(target["market_id"]) or {"name": ""}
            contact = str(args.get("contact") or "").strip() if len(refs) == 1 else ""
            group = target["company"] if playbook.terms["group"].lower() in ACCOUNT_WORDS else owner_market["name"]
            keys = {f["key"] for f in playbook.fields}
            deal = self.store.add(
                contact or target["company"], playbook.first_stage(), actor=actor, company=target["company"], grp=group,
                email=str(args.get("email") or "") if contact else "", linkedin=str(args.get("linkedin") or "") if contact else "",
                owner=str(args.get("owner") or ""), source="market map: %s" % owner_market["name"],
                next_step="First outreach" if contact else "Find the right contact",
                fields={k: v for k, v in (("title", str(args.get("title") or "") if contact else ""),
                                          ("location", target["location"]), ("company_size", target["size"])) if v and k in keys})
            note = " ".join(b for b in (target["why"], "Signal: %s." % target["signal"] if target["signal"] else "",
                                        "Source: %s" % target["source"] if target["source"] else "") if b)
            if note:
                self.store.log(deal["id"], "note", "From the %s map. %s" % (owner_market["name"], note), actor=actor)
            with self.store._conn() as conn:
                conn.execute("UPDATE targets SET status = 'tracked', deal_id = ?, updated_at = ? WHERE id = ?",
                             (deal["id"], st.now(), target["id"]))
            made.append({"company": target["company"], "deal": deal["id"], "market_id": target["market_id"]})
        for market_id in {m["market_id"] for m in made}:
            self._refresh(market_id)
        out = {"ok": bool(made or already), "added_to_pipeline": [{"company": m["company"], "deal": m["deal"]} for m in made]}
        if already:
            out["already_in_pipeline"] = already
        if unknown:
            out["not_on_a_map"] = unknown
        if not out["ok"]:
            out["error"] = "none of those are on a market map"
        return out

    def dismiss(self, args: dict) -> dict:
        refs = args.get("targets") or ([args["target"]] if args.get("target") else [])
        if isinstance(refs, str):
            refs = [refs]
        market = self._market(args.get("market"))
        done, touched = [], set()
        for ref in refs[:50]:
            target = self.target(ref, market["id"] if market else "")
            if target and target["status"] != "tracked":
                with self.store._conn() as conn:
                    conn.execute("UPDATE targets SET status = 'dismissed', signal = ?, updated_at = ? WHERE id = ?",
                                 (("Dismissed: %s" % args["reason"])[:400] if args.get("reason") else target["signal"],
                                  st.now(), target["id"]))
                done.append(target["company"])
                touched.add(target["market_id"])
        for market_id in touched:
            self._refresh(market_id)
        return {"ok": bool(done), "dismissed": done} if done else {"ok": False, "error": "nothing matched (records already in the pipeline cannot be dismissed)"}

    def post(self, args: dict, session: dict) -> dict:
        market = self._market(args.get("market"))
        if market is None:
            return self._missing(args.get("market"))
        rows = [r for r in self.targets(market["id"]) if r["status"] != "dismissed"]
        text = map_text(market, rows)
        slack = self.desk.slack
        channel = str(args.get("channel") or "").strip()
        if not channel and session.get("platform") == "slack":
            channel = session.get("chat_id") or ""
        if slack is None or not slack.available or not channel:
            return {"ok": True, "posted": False, "map": text, "note": "Slack is not available here, so show this map as text."}
        here = session.get("platform") == "slack" and channel == session.get("chat_id")
        result = slack.post_map(channel, market["id"], session.get("thread_id") or "" if here else "",
                                session.get("session_key") or "" if here else "")
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error") or "Slack did not accept the map", "map": text}
        return {"ok": True, "posted": True, "note": "The map is posted with an Add to pipeline button on each company. "
                                                    "Do not repeat it; one line is enough."}

    # -- for the Slack half ---------------------------------------------------------------------
    def blocks(self, market_id: str) -> list:
        market = self._market(market_id)
        if market is None:
            return []
        rows = [r for r in self.targets(market_id) if r["status"] != "dismissed"]
        return map_blocks(market, rows)

    def _refresh(self, market_id: str) -> None:
        slack = self.desk.slack
        if slack is not None:
            try:
                slack.refresh(MAP_CARD + market_id)
            except Exception:
                pass

    def click_track(self, target_ref: str, actor: str) -> dict:
        """A tap on Add to pipeline: create the record, then ask the agent to find the buyer."""
        target = self.target(target_ref)
        if target is None:
            return {"deal": None, "notice": "That company is no longer on the map."}
        result = self.track({"targets": [target["id"]]}, actor=actor)
        if not result.get("added_to_pipeline"):
            return {"deal": None, "market_id": target["market_id"], "notice": "%s is already in the pipeline." % target["company"]}
        deal = self.store.get(result["added_to_pipeline"][0]["deal"])
        playbook = self.desk.playbook()
        how = ("gtm_market(action=\"people\", target=\"%s\", titles=[...]) (each person returned costs a credit, so ask for at "
               "most three)" % target["id"] if self.desk.secret(PDL_KEY) else "your web search tool")
        followup = ("[Stoa desk] %s added %s to the pipeline from the market map. The record is already created (id %s), so do "
                    "not add it again.\n\nYour task: work out who at %s is the most likely buyer for us, using %s. Put that "
                    "person on the record with gtm_deal(action=\"update\"), then reply here with who you picked and why, and a "
                    "first-touch note of under 90 words. Do not send anything."
                    % (actor or "Someone", target["company"], deal["id"], target["company"], how))
        return {"deal": deal, "market_id": target["market_id"], "followup": followup,
                "notice": "", "label": playbook.terms["deal"]}


def _unpack(row: dict) -> dict:
    try:
        row["segments"] = json.loads(row.get("segments") or "[]")
    except ValueError:
        row["segments"] = []
    return row


def _view(row: dict) -> dict:
    out = {k: row[k] for k in ("id", "company", "domain", "fit", "size", "location", "why", "signal", "source", "status")}
    if row["deal_id"]:
        out["deal"] = row["deal_id"]
    return {k: v for k, v in out.items() if v not in ("", None)}


def by_segment(market: dict, rows: list) -> list:
    """``[(segment name, rows)]`` in the market's declared order, then any others, then unsorted."""
    order = [s["name"] for s in market.get("segments") or []]
    groups: dict = {}
    for row in rows:
        groups.setdefault(row["segment"] or "Unsorted", []).append(row)
    lowered = {name.lower(): name for name in groups}
    names = [lowered[o.lower()] for o in order if o.lower() in lowered]
    names += sorted(n for n in groups if n not in names and n != "Unsorted")
    if "Unsorted" in groups and "Unsorted" not in names:
        names.append("Unsorted")
    return [(name, groups[name]) for name in names]


def _facts(row: dict) -> str:
    return " · ".join(b for b in (row["size"], row["location"]) if b)


def map_text(market: dict, rows: list) -> str:
    tracked = len([r for r in rows if r["status"] == "tracked"])
    lines = ["Market map · %s" % market["name"], "%d companies · %d in the pipeline" % (len(rows), tracked)]
    if market.get("brief"):
        lines.append(market["brief"])
    if not rows:
        lines.append("")
        lines.append("Nothing on the map yet.")
    for name, group in by_segment(market, rows):
        lines.append("")
        lines.append("%s (%d)" % (name, len(group)))
        for row in group[:15]:
            facts = _facts(row)
            line = "  %s %s%s" % (dots(row["fit"]), row["company"], " · " + facts if facts else "")
            if row["status"] == "tracked":
                line += "  [in pipeline]"
            lines.append(line)
            if row["why"]:
                lines.append("        %s" % row["why"][:160])
        if len(group) > 15:
            lines.append("  … and %d more" % (len(group) - 15))
    return "\n".join(lines)


def map_blocks(market: dict, rows: list, budget: int = 44) -> list:
    """Block Kit for a map: one row per company with an Add to pipeline button."""
    from .cards import PREFIX, _escape
    tracked = len([r for r in rows if r["status"] == "tracked"])
    head = "*Market map · %s*\n`%d companies · %d in the pipeline`" % (_escape(market["name"]), len(rows), tracked)
    if market.get("brief"):
        head += "\n%s" % _escape(market["brief"][:600])
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]
    groups = by_segment(market, rows)
    per_segment = max(2, (budget - 1 - 2 * len(groups)) // max(1, len(groups)))
    hidden = 0
    for name, group in groups:
        if len(blocks) + 3 > budget:
            hidden += len(group)
            continue
        blocks.append({"type": "divider"})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "*%s*  ·  %d" % (_escape(name.upper()), len(group))}]})
        for row in group[:per_segment]:
            if len(blocks) + 1 > budget:
                hidden += 1
                continue
            facts = _facts(row)
            text = "`%s`  *%s*%s" % (dots(row["fit"]), _escape(row["company"]), "  ·  " + _escape(facts) if facts else "")
            if row["why"]:
                text += "\n%s" % _escape(row["why"][:220])
            block = {"type": "section", "text": {"type": "mrkdwn", "text": text}}
            if row["status"] == "tracked":
                block["text"]["text"] += "\n:white_check_mark: _in the pipeline_"
            else:
                block["accessory"] = {"type": "button", "action_id": PREFIX + "track", "value": row["id"],
                                      "text": {"type": "plain_text", "text": "Add to pipeline"}}
            blocks.append(block)
        hidden += max(0, len(group) - per_segment)
    if hidden:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "%d more on the map. Ask me to show a segment." % hidden}]})
    return blocks
