"""Stoa: a go-to-market desk for small teams, inside Hermes.

    στοά, the covered walk beside the agora where the day's business was done.

Map a market, find the leads, run the pipeline. Market maps that become leads with one click,
one SQLite ledger, playbooks that fit it to a niche, Slack cards whose buttons move the pipeline
at once and hand the writing to the agent, meeting notes that become next steps, and optional
People Data Labs enrichment. ``register`` only wires things up; the work lives in ``desk``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import schemas
from .desk import Desk

log = logging.getLogger("hermes.plugins.stoa")
PLUGIN = "stoa"
TOOLSET = "stoa"


def _data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir(PLUGIN)


def _secret(name: str):
    from agent.secret_scope import get_secret
    return get_secret(name)


def _session() -> dict:
    """Where the current turn is happening (empty outside a gateway session)."""
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return {}
    return {"platform": (get_session_env("HERMES_SESSION_PLATFORM") or "").lower(),
            "chat_id": get_session_env("HERMES_SESSION_CHAT_ID") or "",
            "thread_id": get_session_env("HERMES_SESSION_THREAD_ID") or "",
            "session_key": get_session_env("HERMES_SESSION_KEY") or "",
            "actor": get_session_env("HERMES_SESSION_ACTOR_NAME") or get_session_env("HERMES_SESSION_USER_NAME") or ""}


AWAY = {"ok": False, "error": "The Stoa desk is not available in this conversation (the team limits it to certain "
                                "chats). Do not retry here; use your other tools."}


def _tool(fn, desk=None):
    """Tool handlers return JSON strings and never raise into the agent loop."""
    def handler(args: dict, **kwargs) -> str:
        try:
            if desk is not None and not desk.allowed(_session()):
                return json.dumps(AWAY)
            return json.dumps(fn(args or {}), ensure_ascii=False, default=str)
        except Exception as exc:
            log.error("stoa: %s failed: %s", getattr(fn, "__name__", "tool"), exc, exc_info=True)
            return json.dumps({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
    return handler


def build_desk(ctx) -> Desk:
    return Desk(data_dir=_data_dir, get_config=ctx.get_config, set_config=ctx.set_config, get_secret=_secret,
                actor=lambda: _session().get("actor", ""))


def register(ctx) -> None:
    desk = build_desk(ctx)

    # -- tools ----------------------------------------------------------------------------------
    for schema, fn, emoji in (
        (schemas.GTM_PIPELINE, desk.pipeline, "🏛️"),
        (schemas.GTM_DEAL, desk.deal, "🏛️"),
        (schemas.GTM_CARD, lambda args: desk.card(args, _session()), "🏛️"),
        (schemas.GTM_MARKET, lambda args: desk.market(args, _session()), "🗺️"),
        (schemas.GTM_ENRICH, desk.enrich, "🔎"),
        (schemas.GTM_NOTES, desk.notes, "📝"),
        (schemas.GTM_PLAYBOOK, desk.playbook_tool, "📜"),
        (schemas.GTM_EXPORT, desk.export, "📤"),
    ):
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=_tool(fn, desk), emoji=emoji)

    # -- Slack cards and their buttons ----------------------------------------------------------
    from .slackdesk import ACTIONS, SlackDesk
    slack = SlackDesk(desk, ctx.inject_message)
    desk.slack = slack
    ctx.register_platform_handler("slack", slack.wire)
    ctx.register_slack_action_handler(ACTIONS, slack.on_action)

    # -- /gtm on every surface, `hermes stoa` in a terminal -------------------------------------
    def slash(raw_args: str) -> str:
        return desk.slash(raw_args) if desk.allowed(_session()) else "The Stoa desk is not available in this chat."

    ctx.register_command("gtm", handler=slash, description="Pipeline board, brief, or one record's card",
                         args_hint="[brief|stale|due|playbooks|<name>]")
    from . import cli
    ctx.register_cli_command(name="stoa", help="Stoa GTM desk: status, sample data, CSV import and export",
                             setup_fn=cli.setup, handler_fn=lambda args: cli.run(desk, args))

    # -- what the agent knows without being told ------------------------------------------------
    def section(info) -> str:
        session = dict(_session(), platform=str((info or {}).get("platform") or "").lower())
        return desk.prompt_section() if desk.allowed(session) else ""

    ctx.register_system_prompt_section("stoa.desk", section, max_chars=900)
    skills = Path(__file__).parent / "skills"
    for child in sorted(skills.iterdir()):
        if child.is_dir() and (child / "SKILL.md").exists():
            ctx.register_skill(child.name, child / "SKILL.md")

    # -- automations the team can switch on from /blueprint -------------------------------------
    deliver = {"name": "deliver", "type": "enum", "label": "Where to deliver?", "default": "origin",
               "options": ["origin", "local"], "strict": False}
    ctx.register_automation_blueprint(
        "morning-brief",
        title="Pipeline morning brief",
        description="Each weekday: what is due, what has gone quiet, and what moved, from the Stoa desk.",
        category="work",
        schedule_template="{minute} {hour} * * 1-5",
        prompt_template=("Load the skill stoa:desk, then call gtm_pipeline(view=\"brief\") and write the morning pipeline "
                         "brief: next steps due today first, then records gone quiet with a suggested nudge for each, "
                         "then one line on this week's movement. Under 200 words, no preamble."),
        slots=[{"name": "time", "type": "time", "label": "What time?", "default": "08:30"}, deliver],
        tags=["gtm", "daily"],
    )
    ctx.register_automation_blueprint(
        "notes-sweep",
        title="File meeting notes",
        description="A few times a day: read new call notes, file them on the right record, and propose next steps.",
        category="work",
        schedule_template="0 {hours} * * 1-5",
        prompt_template=("Load the skill stoa:desk. Call gtm_notes(action=\"inbox\"). For each waiting note: read it, and if "
                         "it is about a record on the desk, file a two or three sentence summary on that record with the "
                         "next step it implies; skip notes that are not about the pipeline. Then report what you filed and "
                         "any stage changes you recommend. Do not change stages yourself. If the inbox is empty, reply "
                         "with exactly [SILENT]."),
        slots=[{"name": "hours", "type": "text", "label": "Which hours? (cron hour list)", "default": "11,14,17"}, deliver],
        tags=["gtm", "notes"],
    )
    ctx.register_automation_blueprint(
        "market-watch",
        title="Market watch",
        description="Once a week: look for companies that newly fit one of your market maps and add the best few.",
        category="work",
        schedule_template="{minute} {hour} * * 1",
        prompt_template=("Load the skill stoa:market-map. For the market map \"{market}\": call gtm_market(action=\"show\") to see "
                         "what is already there, then research companies that fit its brief and are not on it yet, favouring "
                         "ones with a fresh buying signal. Add at most five, each with its evidence and source URL. Report "
                         "what you added and why in under 150 words. If you found nothing worth adding, reply with exactly "
                         "[SILENT]."),
        slots=[{"name": "market", "type": "text", "label": "Which market map?", "default": "my market"},
               {"name": "time", "type": "time", "label": "What time on Mondays?", "default": "07:30"}, deliver],
        tags=["gtm", "market", "weekly"],
    )
    ctx.register_automation_blueprint(
        "friday-review",
        title="Friday pipeline review",
        description="Every Friday: the week in the pipeline, what is stuck, and the three records to push on Monday.",
        category="work",
        schedule_template="{minute} {hour} * * 5",
        prompt_template=("Load the skill stoa:desk. Call gtm_pipeline(view=\"brief\") and gtm_pipeline(view=\"activity\", "
                         "days=7). Write the week's pipeline review: what was won and lost and why, where records are "
                         "piling up by stage, and the three records most worth pushing on Monday with the reason for "
                         "each. Under 250 words."),
        slots=[{"name": "time", "type": "time", "label": "What time?", "default": "15:30"}, deliver],
        tags=["gtm", "weekly"],
    )
