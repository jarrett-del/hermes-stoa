"""The Slack half: posts cards and answers their buttons.

Hermes hands the plugin its own ``slack_bolt.AsyncApp`` through
``ctx.register_platform_handler("slack", ...)``; cards are posted and updated with that app's
client, so the plugin never sees or stores a Slack token. Button clicks arrive through
``ctx.register_slack_action_handler``.

A click has two halves. The deterministic half (stage change, activity log, card redraw) happens
here, at once, with no model involved. The writing half (the outreach note, the proposal email)
is handed to the agent as a turn in the card's thread.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Optional

from . import cards

log = logging.getLogger("hermes.plugins.stoa")
ACTIONS = re.compile(r"^%s(advance|draft|enrich|lost|more|reopen|track)$" % cards.PREFIX)
MAP = "map:"  # key under which a posted market map is remembered
POST_TIMEOUT = 20
HANDOFF_GRACE = 45  # seconds an accepted hand-off gets to open the record before the thread is told how to ask
HANDOFF_SETTLE = 8  # seconds between the agent opening the record and its draft landing in the thread


class SlackDesk:
    def __init__(self, desk, inject: Callable[..., bool]):
        self.desk = desk
        self.inject = inject
        self.app: Any = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.grace = HANDOFF_GRACE
        self.settle = HANDOFF_SETTLE
        self._watching: set = set()

    # -- wiring ---------------------------------------------------------------------------------
    def wire(self, native: Any, adapter: Any = None) -> None:
        """Platform-handler factory: called by the Slack adapter when it connects."""
        self.app = native
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    @property
    def available(self) -> bool:
        return self.app is not None and self.loop is not None and not self.loop.is_closed()

    @property
    def client(self) -> Any:
        return self.app.client

    # -- called from tool threads ---------------------------------------------------------------
    def _run(self, coro) -> Any:
        """Run a coroutine on the gateway loop from a worker thread and wait for it."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:  # already on the loop: cannot block on it
            task = self.loop.create_task(coro)
            self._watching.add(task)        # keep a reference, or the loop may drop the task half way
            task.add_done_callback(self._watching.discard)
            return None
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(POST_TIMEOUT)

    def post_card(self, channel: str, deal_id: str, thread_ts: str = "", session_key: str = "") -> dict:
        if not self.available:
            return {"ok": False, "error": "Slack is not connected"}
        try:
            result = self._run(self._post(channel, deal_id, thread_ts, session_key))
        except Exception as exc:
            log.warning("stoa: could not post a card to %s: %s", channel, exc)
            return {"ok": False, "error": _slack_error(exc)}
        return result or {"ok": True, "channel": channel}

    def post_map(self, channel: str, market_id: str, thread_ts: str = "", session_key: str = "") -> dict:
        return self.post_card(channel, MAP + market_id, thread_ts, session_key)

    def refresh(self, deal_id: str) -> None:
        """Redraw every card already posted for a deal or a market map (best effort, never raises)."""
        if not self.available or not self.desk.store.cards(deal_id):
            return
        try:
            self._run(self._refresh(deal_id))
        except Exception:
            log.debug("stoa: card refresh failed", exc_info=True)

    # -- coroutines on the gateway loop ---------------------------------------------------------
    def _render(self, key: str) -> tuple:
        """``(blocks, notification text)`` for a deal id or a ``map:<market id>`` key; ``(None, "")`` if gone."""
        if key.startswith(MAP):
            blocks = self.desk.markets.blocks(key[len(MAP):])
            return (blocks, "Market map") if blocks else (None, "")
        deal = self.desk.store.get(key)
        if deal is None:
            return None, ""
        return self.desk.blocks(deal), _fallback(deal)

    async def _post(self, channel: str, deal_id: str, thread_ts: str = "", session_key: str = "") -> dict:
        blocks, text = await asyncio.to_thread(self._render, deal_id)
        if blocks is None:
            return {"ok": False, "error": "no such record"}
        kwargs = {"channel": channel, "blocks": blocks, "text": text, "unfurl_links": False}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        response = await self.client.chat_postMessage(**kwargs)
        await asyncio.to_thread(self.desk.store.remember_card, deal_id, response["channel"], response["ts"], session_key)
        return {"ok": True, "channel": response["channel"], "ts": response["ts"]}

    async def _update(self, channel: str, ts: str, deal_id: str) -> None:
        blocks, text = await asyncio.to_thread(self._render, deal_id)
        if blocks is None:
            return
        try:
            await self.client.chat_update(channel=channel, ts=ts, blocks=blocks, text=text)
        except Exception as exc:
            if "message_not_found" in str(exc) or "channel_not_found" in str(exc):
                await asyncio.to_thread(self.desk.store.forget_card, channel, ts)
            else:
                log.warning("stoa: could not update the card in %s: %s", channel, exc)

    async def _refresh(self, deal_id: str, skip: tuple = ()) -> None:
        for card in await asyncio.to_thread(self.desk.store.cards, deal_id):
            if (card["channel"], card["ts"]) != skip:
                await self._update(card["channel"], card["ts"], deal_id)

    async def _say(self, channel: str, thread_ts: str, text: str) -> None:
        try:
            await self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text, unfurl_links=False)
        except Exception as exc:
            log.warning("stoa: could not post in the card's thread: %s", exc)

    # -- button clicks --------------------------------------------------------------------------
    async def on_action(self, ack, body, action) -> None:
        await ack()  # Slack wants this within 3 seconds; the work follows
        try:
            self.loop = asyncio.get_running_loop()
            await self._handle(body or {}, action or {})
        except Exception as exc:  # a click must never take the gateway down
            log.error("stoa: click handling failed: %s", exc, exc_info=True)

    async def _handle(self, body: dict, action: dict) -> None:
        action_id = action.get("action_id", "")
        verb = action_id[len(cards.PREFIX):]
        value = action.get("value") or (action.get("selected_option") or {}).get("value", "")
        user = body.get("user") or {}
        channel = (body.get("channel") or {}).get("id", "") or (body.get("container") or {}).get("channel_id", "")
        message = body.get("message") or {}
        ts = message.get("ts") or (body.get("container") or {}).get("message_ts", "")
        team = (body.get("team") or {}).get("id", "")
        actor = user.get("name") or user.get("username") or user.get("id", "")

        thread = message.get("thread_ts") or ts
        if verb == "track":  # market map: "<target id>"
            if value and channel and ts:
                await self._track(value, channel, ts, thread, user.get("id", ""), actor, team)
            return

        extra = ""
        if verb == "more":  # overflow menu: "<verb>|<deal id>"
            verb, _, deal_id = value.partition("|")
        elif verb == "lost":  # reason menu: "<deal id>|<reason index>"
            deal_id, _, extra = value.rpartition("|")
        else:  # buttons: "<deal id>" or "<deal id>|<stage shown on the card>"
            deal_id, _, extra = value.partition("|")
        if not deal_id or not channel or not ts:
            return

        result = await asyncio.to_thread(self.desk.click, verb, deal_id, extra, actor)
        log.info("stoa: %s clicked %s on %s", actor, verb, deal_id)
        if result.get("deal") is None:
            await self._say(channel, thread, result.get("notice") or "That record is no longer on the desk.")
            return
        await asyncio.to_thread(self.desk.store.remember_card, deal_id, channel, ts)
        followup = result.get("followup") or ""
        state = await self._begin(deal_id, followup, channel, ts, thread, user.get("id", ""), actor, team) if followup else ""
        await self._update(channel, ts, deal_id)        # one redraw: the new stage, and where the draft stands
        await self._refresh(deal_id, skip=(channel, ts))

        if followup:
            hint = "Saved %s. Reply here (\"draft it\" is enough) and I'll write it up." % result["deal"]["name"]
            await self._follow(state, deal_id, followup, hint, channel, thread)
        elif result.get("notice"):
            await self._say(channel, thread, result["notice"])

    async def _track(self, target: str, channel: str, ts: str, thread: str, user_id: str, actor: str, team: str) -> None:
        result = await asyncio.to_thread(self.desk.markets.click_track, target, actor)
        log.info("stoa: %s added map target %s to the pipeline", actor, target)
        if result.get("market_id"):
            key = MAP + result["market_id"]
            await asyncio.to_thread(self.desk.store.remember_card, key, channel, ts)
            await self._update(channel, ts, key)
            await self._refresh(key, skip=(channel, ts))
        if result.get("followup") and result.get("deal"):
            hint = ("Added %s to the pipeline. Reply here and I'll find the right contact and draft the first note."
                    % result["deal"]["company"])
            state = await self._begin(result["deal"]["id"], result["followup"], channel, ts, thread, user_id, actor, team)
            await self._follow(state, result["deal"]["id"], result["followup"], hint, channel, thread)
        elif result.get("notice"):
            await self._say(channel, thread, result["notice"])

    async def _begin(self, deal_id: str, followup: str, channel: str, ts: str, thread: str,
                     user_id: str, actor: str, team: str) -> str:
        """Give the writing task to the agent without ever losing it. Returns where it stands:
        ``drafting`` (handed over), ``waiting`` (Hermes would not start a turn), or ``same`` (this
        very task is already being drafted: a second press of the button, which changes nothing).

        The task is parked on the record first. The agent's first step in any turn about the record
        is ``gtm_deal(get)``, which hands the task over and clears it.
        """
        if await asyncio.to_thread(self.desk.handoff_same, deal_id, followup):
            return "same"
        await asyncio.to_thread(self.desk.park_followup, deal_id, followup)
        await asyncio.to_thread(self.desk.set_handoff, deal_id, "drafting", followup)
        if await self._hand_off(followup, channel, ts, thread, user_id, actor, team):
            return "drafting"
        await asyncio.to_thread(self.desk.set_handoff, deal_id, "waiting", followup)
        return "waiting"

    async def _follow(self, state: str, deal_id: str, followup: str, hint: str, channel: str, thread: str) -> None:
        """After the card is redrawn: say how to ask when Hermes refused, and keep an eye on a
        hand-off it accepted. Hermes says yes before it has routed the turn, and a turn that never
        starts makes no sound."""
        if state == "waiting":
            await self._say(channel, thread, hint)
        elif state == "drafting":
            watch = asyncio.get_running_loop().create_task(self._check_started(deal_id, followup, channel, thread, hint))
            self._watching.add(watch)
            watch.add_done_callback(self._watching.discard)

    async def _check_started(self, deal_id: str, followup: str, channel: str, thread: str, hint: str) -> None:
        """Watch an accepted hand-off. Once the agent has opened the record, give the draft time to
        land and take "Drafting" off the card. If it never does, the card and the thread say so."""
        try:
            step = min(2.0, max(self.grace / 10.0, 0.005))
            waited = 0.0
            while waited < self.grace:
                await asyncio.sleep(step)
                waited += step
                if await asyncio.to_thread(self.desk.store.meta_get, "pending:" + deal_id) != followup:
                    await asyncio.sleep(self.settle)
                    await asyncio.to_thread(self.desk.clear_handoff, deal_id, followup)
                    await self._refresh(deal_id)
                    return
            log.info("stoa: no agent turn picked up the task for %s; telling the thread how to ask", deal_id)
            await asyncio.to_thread(self.desk.set_handoff, deal_id, "waiting", followup)
            await self._refresh(deal_id)
            await self._say(channel, thread, hint)
        except Exception:
            log.debug("stoa: hand-off check failed", exc_info=True)

    async def _hand_off(self, followup: str, channel: str, ts: str, thread: str, user_id: str, actor: str, team: str) -> bool:
        """Give the writing task to the agent. Hermes lets a plugin continue a conversation a
        person started, not open one, so the first choice is the session the card was posted
        from; next, the card's thread, which works once someone has spoken there."""
        session_key = await asyncio.to_thread(self.desk.store.card_session, channel, ts)
        attempts = []
        if session_key:
            attempts.append({"session_key": session_key})
        attempts.append({"origin": {
            "platform": "slack", "chat_id": channel, "chat_type": "dm" if channel.startswith("D") else "group",
            "user_id": user_id, "user_name": actor, "thread_id": thread, "message_id": ts, "scope_id": team or None}})
        for target in attempts:
            try:
                if self.inject(followup, **target):
                    return True
            except Exception:
                log.warning("stoa: handing the follow-up to the agent failed", exc_info=True)
        log.info("stoa: the gateway did not start the follow-up turn (no conversation to continue, or "
                 "plugins.entries.stoa.allow_gateway_injection is not true); the task stays parked on the record")
        return False


def _fallback(deal: dict) -> str:
    """The notification line, and what screen readers get."""
    return "%s%s — %s" % (deal["name"], " · " + deal["company"] if deal.get("company") else "", deal["stage"])


def _slack_error(exc: Exception) -> str:
    text = str(exc)
    for code, meaning in (("not_in_channel", "the bot is not in that channel; invite it first"),
                          ("channel_not_found", "Slack does not know that channel; pass its id (C…) or invite the bot"),
                          ("is_archived", "that channel is archived"),
                          ("missing_scope", "the Slack app is missing the chat:write scope")):
        if code in text:
            return meaning
    return "Slack did not accept the card (%s)" % text[:160]
