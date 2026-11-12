"""Backend for the Stoa dashboard tab. Mounted by Hermes at ``/api/plugins/stoa/``.

Every handler is a plain ``def`` so FastAPI runs it in a worker thread, off the event loop. The
tab reads one ``/state`` document on a short poll and posts small, deterministic actions; nothing
here calls a model or any outside service.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException

router = APIRouter()
PLUGIN_DIR = Path(__file__).resolve().parent.parent
PACKAGE = "stoa_dashboard_pkg"
_own_desks: dict = {}


def _package():
    """The plugin's own modules, imported as a private package (this file is loaded standalone)."""
    if PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(PACKAGE, PLUGIN_DIR / "__init__.py",
                                                      submodule_search_locations=[str(PLUGIN_DIR)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE] = module
        spec.loader.exec_module(module)
    return sys.modules[PACKAGE]


def _settings(key: str, default: Any = None) -> Any:
    try:
        from hermes_cli.config import load_config_readonly
        entry = ((load_config_readonly().get("plugins") or {}).get("entries") or {}).get("stoa") or {}
        value = (entry.get("settings") or {}).get(key, default)
        return default if value in (None, "") else value
    except Exception:
        return default


def desk():
    """The desk for the profile this request is for.

    Prefer the one the running plugin built for that profile: it is wired to Slack, so a change made
    here redraws the cards there. Otherwise (the dashboard runs on its own, or in a plugin host) build one.
    """
    package = _package()
    home = package._home()
    init = str(PLUGIN_DIR / "__init__.py")
    for module in list(sys.modules.values()):
        try:
            if getattr(module, "__file__", None) == init:
                for live in reversed(getattr(module, "LIVE_DESKS", None) or []):
                    if getattr(live, "home", None) == home:
                        return live
        except Exception:
            continue
    if str(home) not in _own_desks:
        _own_desks[str(home)] = package.build_desk(_settings, lambda key, value: None)
        _own_desks[str(home)].can_configure = False     # no way to save a setting from here: the page hands that to Hermes
    return _own_desks[str(home)]


def _mod(name: str):
    """One of the plugin's own modules, from whichever copy of the plugin the desk in use belongs to."""
    return importlib.import_module(type(desk()).__module__.rsplit(".", 1)[0] + "." + name)


def _card(d, deal: dict, playbook, touches: Optional[dict] = None, pending: Optional[dict] = None, groups: Optional[dict] = None) -> dict:
    """One record for the page. ``touches``, ``pending`` and ``groups`` are passed when many are built at
    once, so the whole board costs three queries instead of three per record."""
    view = d.view(deal, playbook, touches=touches, groups=groups)
    view["position"] = playbook.position(deal["stage"])
    view["stage_at"] = deal["stage_at"]
    view["updated_at"] = deal["updated_at"]
    view["advance"] = playbook.advance_label(deal["stage"]) if deal["status"] == "open" and playbook.next_stage(deal["stage"]) else ""
    view["pending"] = bool(pending.get(deal["id"]) if pending is not None else d.store.meta_get("pending:" + deal["id"]))
    view["created_at"] = deal["created_at"]
    return view


def _ask(d, deal_id: str, verb: str) -> str:
    """The line the Desktop app or Webapp half sends into the open chat after a click that calls
    for writing. The task itself waits on the record; this just points the agent at it."""
    record = d.store.get(deal_id) or {}
    name = record.get("name") or deal_id
    if verb == "draft":
        return "Write the follow-up for %s on the Stoa desk. The task is waiting on record %s." % (name, deal_id)
    if verb == "track":
        return ("%s was just added to the Stoa desk from the market map. Open record %s and do the task waiting on it."
                % (record.get("company") or name, deal_id))
    return ("%s is now at %s on the Stoa desk. Open record %s and write what is waiting on it."
            % (name, d.playbook().label(record.get("stage", "")), deal_id))


@router.post("/hello")
def hello() -> dict:
    """The Desktop app or Webapp half checks in while it is open. That is how the desk knows a card
    or a map can be drawn live in the conversation there."""
    d = desk()
    d.desktop_seen = time.time()
    return {"ok": True, "playbook": d.playbook().name}


@router.get("/state")
def state() -> dict:
    d = desk()
    playbook = d.playbook()
    deals = d.store.list(status="all")
    names = {deal["id"]: deal["name"] for deal in deals}
    touches, pending = d.store.touches(), d.store.meta_prefix("pending:")
    groups = {row["key"]: row for row in d.store.grps()}
    cards = _mod("cards")
    import datetime as _dt
    week_ago = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    markets = []
    for market in d.markets.list_markets():
        summary = d.markets.summary(market["id"], text=False)
        markets.append({
            "id": market["id"], "name": market["name"], "brief": market["brief"],
            "requirements": summary["market"]["requirements"], "reading": summary["market"]["reading"],
            "implications": summary["market"]["implications"], "numbers": summary["numbers"],
            "segments": [{"name": seg["segment"], "description": seg["description"], "count": seg["count"], "share": seg["share"],
                          "avg_fit": seg["avg_fit"], "companies": seg["companies"]} for seg in summary["segments"]],
            "rates": summary["requirements"], "holds": summary["holds"], "holds_of": summary["holds_of"],
            "regions": summary["regions"], "sizes": summary["sizes"], "bridges": summary["bridges"],
            "first_calls": summary["first_calls"],
            "counts": {"mapped": summary["numbers"]["companies"], "tracked": summary["numbers"]["in_pipeline"],
                       "dismissed": summary["numbers"]["dismissed"]}})
    last = d.store.undoables()      # per record, the last action that still stands
    offered = set()                 # one Undo per action, on its first line
    gone: dict = {}                 # records taken off the desk that still have lines in the log: their names, and one offer each to put them back
    activity = []
    for a in d.store.activities(limit=40):
        can = bool(a.get("op")) and not a.get("undone") and last.get(a["deal_id"]) == a.get("op") and a["deal_id"] in names
        if a["deal_id"] not in names and a["deal_id"] not in gone:
            gone[a["deal_id"]] = {"name": (d.store.get(a["deal_id"], archived=True) or {}).get("name", ""), "offered": False}
        off = gone.get(a["deal_id"])
        activity.append({"id": a["id"], "deal": a["deal_id"], "name": names.get(a["deal_id"]) or (off or {}).get("name") or a["deal_id"],
                         "at": a["at"], "kind": a["kind"], "text": a["text"], "by": a["actor"], "op": a.get("op") or "",
                         "undone": bool(a.get("undone")), "can_undo": can and a["op"] not in offered,
                         "can_restore": bool(off and off["name"] and not off["offered"])})
        if can:
            offered.add(a["op"])
        if off:
            off["offered"] = True
    open_deals = [x for x in deals if x["status"] == "open"]
    tour = d.tour()
    return {
        "version": _mod("desk")._version(),
        # sample data on show, and whether this is a desk nobody has set up yet (the page then offers the way in)
        "tour": {"example": tour["example"], "title": _mod("demo").EXAMPLES[tour["example"]]["title"]} if tour else None,
        "fresh": not tour and not deals and d.setting("playbook", None) is None,
        "today": d.today(full=False),
        "playbook": {"name": playbook.name, "title": playbook.title, "terms": playbook.terms, "currency": playbook.currency,
                     "stages": [{"id": s["id"], "label": s["label"], "stale_days": playbook.stale_days(s["id"])} for s in playbook.stages],
                     "won": playbook.won, "lost": {"id": playbook.lost["id"], "label": playbook.lost["label"],
                                                  "reasons": playbook.lost["reasons"]},
                     "fields": playbook.fields, "group_fields": playbook.group_fields},
        "deals": [_card(d, deal, playbook, touches, pending, groups) for deal in deals],
        "markets": markets,
        "companies": d.companies(),
        "groups": d.groups(),
        "cash": d.cash(),
        "intakes": d.intake.waiting(),
        "activity": activity,
        "flow": d.flow(deals, playbook),
        "totals": {"open": len(open_deals), "open_value": _mod("store").total(open_deals),
                   "won": len([x for x in deals if x["status"] == "won"]),
                   "won_value": _mod("store").total(x for x in deals if x["status"] == "won"),
                   "lost": len([x for x in deals if x["status"] == "lost"]),
                   "mapped": sum(m["counts"]["mapped"] for m in markets),
                   "tracked": sum(m["counts"]["tracked"] for m in markets),
                   "due": len([x for x in open_deals if cards.is_due(x)]),
                   "stale": len([x for x in open_deals if not cards.is_due(x)
                                 and cards.is_stale(playbook, x, touches.get(x["id"], ""))]),
                   "moves_week": len([a for a in d.store.activities(limit=400, since=week_ago) if a["kind"] == "stage"]),
                   "new_week": len([x for x in open_deals if x["created_at"] >= week_ago])},
        "integrations": {"enrich": d.can_enrich(playbook), "slack": bool(d.slack is not None and d.slack.available)},
        "stamp": (activity[0]["id"] if activity else 0),
    }


@router.get("/deal/{deal_id}")
def deal(deal_id: str) -> dict:
    d = desk()
    record = d.store.get(deal_id)
    if record is None:
        raise HTTPException(404, "no such record")
    playbook = d.playbook()
    out = _card(d, record, playbook)
    out["activity"] = [{"at": a["at"], "kind": a["kind"], "text": a["text"], "by": a["actor"], "undone": bool(a.get("undone"))}
                       for a in d.store.activities(deal_id, limit=30)]
    out["pending_task"] = d.store.meta_get("pending:" + deal_id)
    out["people"] = d.people(record)[1:]             # the others on the record; its own person is the record itself
    bills = d.store.invoices(deal_id)
    out["invoices"] = [d._bill(bill) for bill in bills]
    out["money"] = d.money(record, bills)
    out["delivery_stages"] = playbook.delivery if record["status"] == "won" else []
    out["undo"] = d.store.undoable(deal_id)          # the last action, if it can be taken back
    return out


@router.post("/deal")
def add_deal(body: dict = Body(...)) -> dict:
    d = desk()
    result = d.deal(dict(body or {}, action="add"), actor="dashboard")
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "could not add the record")
    d.announce()
    if result.get("deal") and not result.get("exists"):
        result["last"] = _last(d, result["deal"]["id"])
    return result


def _last(d, deal_id: str) -> Optional[dict]:
    """What was just done to a record, for the page to offer taking it back."""
    last = d.store.undoable(deal_id)
    record = d.store.get(deal_id)
    return dict(last, deal=deal_id, name=record["name"]) if last and record else None


@router.post("/deal/{deal_id}/undo")
def undo(deal_id: str, body: Optional[dict] = Body(None)) -> dict:
    """Take back the last action on a record. With ``op``, only if that action is still its last."""
    d = desk()
    if d.store.get(deal_id) is None:
        raise HTTPException(404, "no such record")
    result = d.undo(deal_id, op=str((body or {}).get("op") or ""), actor="dashboard")
    if not result.get("ok"):      # nothing to take back, or it would overwrite a later change (the error says which)
        raise HTTPException(409, result.get("error") or "nothing to take back")
    d.announce()
    return {"ok": True, "undid": result["undid"], "removed": result["removed"], "name": result["name"],
            "last": None if result["removed"] else _last(d, deal_id)}


@router.post("/deal/{deal_id}/restore")
def restore(deal_id: str) -> dict:
    """Put back a record that was taken off the desk when its add was undone."""
    d = desk()
    result = d.restore(deal_id, actor="dashboard")
    if not result.get("ok"):
        raise HTTPException(404, result.get("error") or "nothing to put back")
    d.announce()
    return {"ok": True, "deal": result["deal"]["id"], "name": result["deal"]["name"]}


@router.post("/deal/{deal_id}/person")
def person(deal_id: str, body: dict = Body(...)) -> dict:
    """add | update | remove one of the other people on a record."""
    d = desk()
    if d.store.get(deal_id) is None:
        raise HTTPException(404, "no such record")
    verb = str((body or {}).get("verb") or "")
    if verb not in ("add", "update", "remove"):
        raise HTTPException(400, "unknown action")
    allowed = ("name", "email", "title", "role", "linkedin", "phone", "note", "person", "new_name")
    with d.store.operation():
        result = d.contact(dict({k: body[k] for k in allowed if k in body}, action=verb, deal=deal_id), actor="dashboard")
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "could not save")
    d.announce()
    return {"ok": True, "exists": bool(result.get("exists")), "people": result["people"][1:], "last": _last(d, deal_id)}


@router.post("/deal/{deal_id}/invoice")
def invoice(deal_id: str, body: dict = Body(...)) -> dict:
    """add | send | pay | void | update | remove an invoice on a record: the same path as the agent's tool."""
    d = desk()
    if d.store.get(deal_id) is None:
        raise HTTPException(404, "no such record")
    verb = str((body or {}).get("verb") or "")
    if verb not in ("add", "send", "pay", "void", "update", "remove"):
        raise HTTPException(400, "unknown action")
    allowed = ("invoice", "amount", "number", "due", "on", "status", "note")
    result = d.invoice(dict({k: body[k] for k in allowed if k in body}, action=verb, deal=deal_id), actor="dashboard")
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "could not save")
    d.announce()
    return {"ok": True, "invoices": result["invoices"], "money": result["money"], "last": _last(d, deal_id)}


@router.post("/company")
def company(body: dict = Body(...)) -> dict:
    """One company: its records, everyone known there, and the notes kept on it. With ``note`` or ``domain``, saves them first."""
    d = desk()
    body = body or {}
    result = d.company(str(body.get("name") or ""), note=body.get("note"), domain=body.get("domain"))
    if not result.get("ok"):
        raise HTTPException(404, result.get("error") or "no such company")
    if body.get("note") is not None or body.get("domain") is not None:
        d.announce()
    return result


@router.post("/group")
def group(body: dict = Body(...)) -> dict:
    """One group (a search, an account, a client): what is known about it, its records and the people on the client's
    side. With ``client``, ``status``, ``value``, ``note`` or ``fields`` it saves them first; with ``to`` it renames it;
    with ``new`` it opens one by that exact name."""
    d = desk()
    body = body or {}
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "say which one")
    changes = {key: body[key] for key in ("client", "status", "value", "note", "fields") if key in body}
    if body.get("to"):
        result = d.group({"action": "rename", "group": name, "to": body["to"]}, actor="dashboard")
    elif changes or body.get("new"):
        result = d.group(dict(changes, action="set", group=name, new=True), actor="dashboard")     # by its exact name: the page never guesses
    else:
        result = d.group({"action": "get", "group": name})
    if not result.get("ok"):
        raise HTTPException(404 if "matches" in str(result.get("error")) else 400, result.get("error") or "could not do that")
    if changes or body.get("to") or body.get("new"):
        d.announce()
    return result


@router.post("/group/person")
def group_person(body: dict = Body(...)) -> dict:
    """add | update | remove someone on a group's side."""
    d = desk()
    body = body or {}
    verb = str(body.get("verb") or "")
    if verb not in ("add", "update", "remove"):
        raise HTTPException(400, "unknown action")
    allowed = ("name", "email", "title", "role", "linkedin", "phone", "note", "person", "new_name")
    result = d.contact(dict({key: body[key] for key in allowed if key in body}, action=verb, group=str(body.get("group") or "")), actor="dashboard")
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "could not save")
    d.announce()
    return result


@router.post("/approvals")
def approvals(body: dict = Body(...)) -> dict:
    """Approve or decline several waiting proposals at once: ``items`` is ``[{intake, n}]``."""
    d = desk()
    result = d.intake.decide_many((body or {}).get("items"), str((body or {}).get("verb") or ""), "app")
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "could not do that")
    return result


@router.post("/autonomy")
def autonomy(body: dict = Body(...)) -> dict:
    """What a scheduled job may change by itself. Set here by a person; no tool can change it."""
    d = desk()
    result = d.set_autonomy((body or {}).get("level"))
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "unknown level")
    d.announce()
    return result


@router.post("/declined/forget")
def forget_declined() -> dict:
    """Forget what was declined, so those proposals may be made again."""
    d = desk()
    forgotten = d.own_store.declined_clear()
    d.announce()
    return {"ok": True, "forgotten": forgotten}


@router.post("/deal/{deal_id}/act")
def act(deal_id: str, body: dict = Body(...)) -> dict:
    """advance | draft | lost | reopen | snooze3 | snooze7 | enrich go through the same path as a
    Slack button; log, update, move, done and snooze (days, or until a date) go through the same path
    as the agent's tool."""
    d = desk()
    verb = str((body or {}).get("verb") or "")
    if d.store.get(deal_id) is None:
        raise HTTPException(404, "no such record")
    if verb in ("advance", "draft", "lost", "reopen", "snooze3", "snooze7", "enrich"):
        result = d.click(verb, deal_id, str(body.get("extra") or ""), "dashboard")
        if result.get("followup"):  # the web cannot start an agent turn: leave the writing task on the record
            d.park_followup(deal_id, result["followup"])
        if result.get("deal"):
            d._changed(result["deal"])
        d.announce()
        return {"ok": True, "notice": result.get("notice") or "", "parked": bool(result.get("followup")),
                "ask": _ask(d, deal_id, verb) if result.get("followup") else "",
                "last": _last(d, deal_id) if verb not in ("draft", "enrich") else None}
    if verb in ("log", "update", "move", "done", "snooze"):
        allowed = ("kind", "text", "next_step", "next_due", "value", "owner", "company", "group", "email", "linkedin", "name", "stage",
                   "days", "until", "close_on", "delivery")
        args = {k: body[k] for k in allowed if k in body}
        result = d.deal(dict(args, action=verb, deal=deal_id), actor="dashboard")
        if not result.get("ok"):
            raise HTTPException(400, result.get("error") or "could not save")
        d.announce()
        return {"ok": True, "last": _last(d, deal_id)}
    raise HTTPException(400, "unknown action")


@router.get("/setup")
def setup() -> dict:
    """What is set up and the rules in force, for the Setup tab. Keys are reported as set or not set only.
    If it cannot be put together at all, the answer says why: this is the tab people open when something is wrong."""
    try:
        return desk().setup()
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger("hermes.plugins.stoa").error("stoa: the Setup tab could not be put together: %s", exc, exc_info=True)
        raise HTTPException(500, "Setup could not be put together: %s: %s" % (type(exc).__name__, str(exc)[:300]))


@router.post("/tour")
def tour(body: dict = Body(...)) -> dict:
    """start: show a worked example (sample data in a file of its own). leave: back to the team's own
    desk. tidy: take sample records out of the team's own desk (versions before 0.10 loaded them there)."""
    d = desk()
    action = str((body or {}).get("action") or "")
    if action == "start":
        result = d.tour_start(str(body.get("example") or ""))
    elif action == "leave":
        result = {"ok": True, "left": d.tour_end()}
    elif action == "tidy":
        result = d.tidy_samples()
    else:
        raise HTTPException(400, "unknown action")
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "could not do that")
    d.announce()
    return result


@router.post("/playbook")
def use_playbook(body: dict = Body(...)) -> dict:
    """Switch the active playbook. Where this page cannot save a setting, it says so and the page asks Hermes instead."""
    d = desk()
    name = str((body or {}).get("name") or "")
    if not d.can_configure:
        return {"ok": False, "ask": "Switch the Stoa desk to the %s playbook." % name}
    result = d.playbook_tool({"action": "use", "name": name})
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "could not switch")
    d.announce()
    return {"ok": True, "active": result["active"], "warning": result.get("warning") or ""}


@router.get("/export")
def export() -> dict:
    """The whole pipeline as CSV text, for the page to hand to the browser as a download."""
    return _export("")


@router.post("/export")
def export_part(body: dict = Body(...)) -> dict:
    """The same for one group (a search, an account), or for everything that is for one client."""
    return _export(str((body or {}).get("group") or ""))


def _export(group: str) -> dict:
    import csv
    import io
    d = desk()
    scope, label, error = d.export_scope(group)
    if error:
        raise HTTPException(404, error.get("error") or "no such group")
    header, rows = d.store.export_rows([field["key"] for field in d.playbook().fields], scope)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["group" if name == "grp" else name for name in header])
    writer.writerows(rows)
    name = _mod("store").slugify(label)[:60] if label else "stoa"
    return {"filename": "%s-%s.csv" % (name, time.strftime("%Y%m%d")), "csv": out.getvalue(), "rows": len(rows), "only": label}


@router.post("/intake")
def intake_file(body: dict = Body(...)) -> dict:
    """A CSV dropped on the page: the desk reads it and holds it for approval, like a list the agent read."""
    d = desk()
    text = str((body or {}).get("text") or "")
    if len(text) > 2_000_000:
        raise HTTPException(413, "that list is over 2 MB; send it in parts")
    result = d.intake.propose_file(str((body or {}).get("name") or ""), text)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "could not read that list")
    counts = result.get("counts") or {}
    waiting = bool(counts.get("new") or counts.get("update"))
    return {"ok": True, "id": result.get("id"), "counts": counts, "waiting": waiting, "problems": result.get("problems") or [],
            "headline": (d.intake.headline(d.store.intake_get(result["id"])) + ". It is waiting for your approval on Today.") if waiting
            else "Nothing in %s would change the desk." % (str((body or {}).get("name") or "that list"))}


@router.get("/intake/{intake_id}")
def intake(intake_id: str) -> dict:
    """A list waiting for approval (or already dealt with): what each row would do."""
    view = desk().intake.view(intake_id)
    if view is None:
        raise HTTPException(404, "no such list")
    return view


@router.post("/intake/{intake_id}/row")
def intake_row(intake_id: str, body: dict = Body(...)) -> dict:
    """Approve or decline one row by itself; the rest of the list stays waiting."""
    d = desk()
    result = d.intake.decide(intake_id, (body or {}).get("n"), str((body or {}).get("verb") or ""), "app")
    if not result.get("ok"):
        raise HTTPException(404 if "no such" in str(result.get("error")) else 400, result.get("error") or "could not do that")
    if result.get("applied") and result.get("deal"):
        result["last"] = _last(d, result["deal"])
    return result


@router.post("/intake/{intake_id}/{verb}")
def intake_act(intake_id: str, verb: str, body: Optional[dict] = Body(None)) -> dict:
    d = desk()
    if verb == "apply":
        result = d.intake.apply({"id": intake_id, "rows": (body or {}).get("rows")}, "app")
    elif verb == "discard":
        result = d.intake.discard(intake_id)
    else:
        raise HTTPException(400, "unknown action")
    if not result.get("ok"):
        raise HTTPException(404, result.get("error") or "no such list")
    return result


@router.get("/market/{market_id}/page")
def market_page(market_id: str, share: int = 0) -> dict:
    """The market map as one self-contained web page, for the tab to hand to the browser as a download."""
    d = desk()
    market = d.markets._market(market_id)
    if market is None:
        raise HTTPException(404, "no such market map")
    html = _mod("page").render(d.markets.summary(market["id"]), d.playbook().terms, share=bool(share))
    return {"filename": "%s%s.html" % (market["id"], "-share" if share else ""), "html": html}


@router.post("/target/{target_id}/{verb}")
def target(target_id: str, verb: str, body: Optional[dict] = Body(None)) -> dict:
    d = desk()
    if verb == "track":
        result = d.markets.click_track(target_id, "dashboard")
        if result.get("deal") and result.get("followup"):
            d.park_followup(result["deal"]["id"], result["followup"])
        d.announce()
        return {"ok": bool(result.get("deal")), "notice": result.get("notice") or "",
                "deal": (result.get("deal") or {}).get("id", ""),
                "ask": _ask(d, result["deal"]["id"], "track") if result.get("deal") and result.get("followup") else ""}
    if verb == "dismiss":
        return d.markets.dismiss({"targets": [target_id], "reason": str((body or {}).get("reason") or "")})
    raise HTTPException(400, "unknown action")
