"""Backend for the Stoa dashboard tab. Mounted by Hermes at ``/api/plugins/stoa/``.

Every handler is a plain ``def`` so FastAPI runs it in a worker thread, off the event loop. The
tab reads one ``/state`` document on a short poll and posts small, deterministic actions; nothing
here calls a model or any outside service.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException

router = APIRouter()
PLUGIN_DIR = Path(__file__).resolve().parent.parent
PACKAGE = "stoa_dashboard_pkg"
_own_desk = None


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
    """Prefer the desk of the plugin instance running in this process: it is the one wired to
    Slack, so a change made here redraws the cards there. Otherwise build a read/write desk."""
    global _own_desk
    init = str(PLUGIN_DIR / "__init__.py")
    for module in list(sys.modules.values()):
        try:
            if getattr(module, "__file__", None) == init and getattr(module, "LIVE_DESKS", None):
                return module.LIVE_DESKS[-1]
        except Exception:
            continue
    if _own_desk is None:
        package = _package()
        Desk = importlib.import_module(PACKAGE + ".desk").Desk
        _own_desk = Desk(data_dir=package._data_dir, get_config=_settings, set_config=lambda key, value: None,
                         get_secret=package._secret, actor=lambda: "dashboard")
    return _own_desk


def _mod(name: str):
    live = desk()
    return sys.modules[type(live).__module__.rsplit(".", 1)[0] + "." + name]


def _card(d, deal: dict, playbook) -> dict:
    view = d.view(deal, playbook)
    view["position"] = playbook.position(deal["stage"])
    view["stage_at"] = deal["stage_at"]
    view["updated_at"] = deal["updated_at"]
    view["advance"] = playbook.advance_label(deal["stage"]) if deal["status"] == "open" and playbook.next_stage(deal["stage"]) else ""
    view["pending"] = bool(d.store.meta_get("pending:" + deal["id"]))
    return view


@router.get("/state")
def state() -> dict:
    d = desk()
    playbook = d.playbook()
    deals = d.store.list(status="all")
    names = {deal["id"]: deal["name"] for deal in deals}
    cards = _mod("cards")
    import datetime as _dt
    week_ago = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    markets = []
    for market in d.markets.list_markets():
        full = d.markets._market(market["id"])
        rows = d.markets.targets(market["id"])
        market_mod = _mod("market")
        markets.append({
            "id": market["id"], "name": market["name"], "brief": market["brief"],
            "segments": [{"name": name, "description": next((s.get("description", "") for s in full["segments"]
                                                             if s["name"].lower() == name.lower()), ""),
                          "companies": [dict(market_mod._view(r), segment=name) for r in group]}
                         for name, group in market_mod.by_segment(full, [r for r in rows if r["status"] != "dismissed"])],
            "counts": {"mapped": len([r for r in rows if r["status"] != "dismissed"]),
                       "tracked": len([r for r in rows if r["status"] == "tracked"]),
                       "dismissed": len([r for r in rows if r["status"] == "dismissed"])}})
    activity = [{"id": a["id"], "deal": a["deal_id"], "name": names.get(a["deal_id"], a["deal_id"]), "at": a["at"],
                 "kind": a["kind"], "text": a["text"], "by": a["actor"]} for a in d.store.activities(limit=40)]
    open_deals = [x for x in deals if x["status"] == "open"]
    return {
        "playbook": {"name": playbook.name, "title": playbook.title, "terms": playbook.terms, "currency": playbook.currency,
                     "stages": [{"id": s["id"], "label": s["label"], "stale_days": playbook.stale_days(s["id"])} for s in playbook.stages],
                     "won": playbook.won, "lost": {"id": playbook.lost["id"], "label": playbook.lost["label"],
                                                  "reasons": playbook.lost["reasons"]},
                     "fields": playbook.fields},
        "playbooks": [p["name"] for p in d.library.catalog() if not p.get("error")],
        "deals": [_card(d, deal, playbook) for deal in deals],
        "markets": markets,
        "activity": activity,
        "totals": {"open": len(open_deals), "open_value": sum(x["value"] for x in open_deals),
                   "won": len([x for x in deals if x["status"] == "won"]),
                   "won_value": sum(x["value"] for x in deals if x["status"] == "won"),
                   "lost": len([x for x in deals if x["status"] == "lost"]),
                   "mapped": sum(m["counts"]["mapped"] for m in markets),
                   "tracked": sum(m["counts"]["tracked"] for m in markets),
                   "due": len([x for x in open_deals if cards.is_due(x)]),
                   "stale": len([x for x in open_deals if not cards.is_due(x)
                                 and cards.is_stale(playbook, x, d.store.last_touch(x["id"]))]),
                   "moves_week": len([a for a in d.store.activities(limit=400, since=week_ago) if a["kind"] == "stage"]),
                   "new_week": len([x for x in open_deals if x["created_at"] >= week_ago])},
        "integrations": {"enrich": d.can_enrich(), "slack": bool(d.slack is not None and d.slack.available)},
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
    out["activity"] = [{"at": a["at"], "kind": a["kind"], "text": a["text"], "by": a["actor"]}
                       for a in d.store.activities(deal_id, limit=30)]
    out["pending_task"] = d.store.meta_get("pending:" + deal_id)
    return out


@router.post("/deal")
def add_deal(body: dict = Body(...)) -> dict:
    result = desk().deal(dict(body or {}, action="add"))
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "could not add the record")
    return result


@router.post("/deal/{deal_id}/act")
def act(deal_id: str, body: dict = Body(...)) -> dict:
    """advance | lost | reopen | snooze3 | snooze7 | enrich go through the same path as a Slack
    button; log and update go through the same path as the agent's tool."""
    d = desk()
    verb = str((body or {}).get("verb") or "")
    if d.store.get(deal_id) is None:
        raise HTTPException(404, "no such record")
    if verb in ("advance", "lost", "reopen", "snooze3", "snooze7", "enrich"):
        result = d.click(verb, deal_id, str(body.get("extra") or ""), "dashboard")
        if result.get("followup"):  # the web cannot start an agent turn: leave the writing task on the record
            d.park_followup(deal_id, result["followup"])
        if result.get("deal"):
            d._changed(result["deal"])
        return {"ok": True, "notice": result.get("notice") or "", "parked": bool(result.get("followup"))}
    if verb in ("log", "update"):
        allowed = ("kind", "text", "next_step", "next_due", "value", "owner", "company", "group", "email", "linkedin", "name")
        args = {k: body[k] for k in allowed if k in body}
        result = d.deal(dict(args, action=verb, deal=deal_id))
        if not result.get("ok"):
            raise HTTPException(400, result.get("error") or "could not save")
        return {"ok": True}
    raise HTTPException(400, "unknown action")


@router.post("/target/{target_id}/{verb}")
def target(target_id: str, verb: str, body: Optional[dict] = Body(None)) -> dict:
    d = desk()
    if verb == "track":
        result = d.markets.click_track(target_id, "dashboard")
        if result.get("deal") and result.get("followup"):
            d.park_followup(result["deal"]["id"], result["followup"])
        return {"ok": bool(result.get("deal")), "notice": result.get("notice") or "",
                "deal": (result.get("deal") or {}).get("id", "")}
    if verb == "dismiss":
        return d.markets.dismiss({"targets": [target_id], "reason": str((body or {}).get("reason") or "")})
    raise HTTPException(400, "unknown action")
