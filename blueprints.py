"""The automations a team can switch on from /blueprint.

Kept as data, so the Setup tab lists exactly the ones ``register`` hands to Hermes.
"""
from __future__ import annotations

DELIVER = {"name": "deliver", "type": "enum", "label": "Where to deliver?", "default": "origin",
           "options": ["origin", "local"], "strict": False}
SHEET = {"name": "sheet", "type": "text", "label": "Which Google Sheet? (its link)", "default": ""}

BLUEPRINTS = [
    dict(
        name="morning-brief",
        title="Pipeline morning brief",
        description="Each weekday: what is due, what has gone quiet, and what moved, from the Stoa desk.",
        category="work",
        schedule_template="{minute} {hour} * * 1-5",
        prompt_template=("Load the skill stoa:desk, then call gtm_pipeline(view=\"brief\") and write the morning pipeline "
                         "brief: next steps due today first, then records gone quiet with a suggested nudge for each, "
                         "then one line on this week's movement. Under 200 words, no preamble."),
        slots=[{"name": "time", "type": "time", "label": "What time?", "default": "08:30"}, DELIVER],
        tags=["gtm", "daily"],
    ),
    dict(
        name="notes-sweep",
        title="File meeting notes",
        description="A few times a day: read new call notes, file them on the right record, and propose next steps.",
        category="work",
        schedule_template="0 {hours} * * 1-5",
        prompt_template=("Load the skill stoa:desk. Call gtm_notes(action=\"inbox\"). For each waiting note: read it, and if "
                         "it is about a record on the desk, file a two or three sentence summary on that record with the "
                         "next step it implies; skip notes that are not about the pipeline. Then report what you filed and "
                         "any stage changes you recommend. Do not change stages yourself. If the inbox is empty, reply "
                         "with exactly [SILENT]."),
        slots=[{"name": "hours", "type": "text", "label": "Which hours? (cron hour list)", "default": "11,14,17"}, DELIVER],
        tags=["gtm", "notes"],
    ),
    dict(
        name="activity-sweep",
        title="Log email, call and Slack activity",
        description="A few times a day: find new email, calls and messages with people on the desk, log them, and ask before any stage changes.",
        category="work",
        schedule_template="0 {hours} * * 1-5",
        prompt_template=("Load the skill stoa:desk and follow its Activity sweep section, looking back {days} days. Log what "
                         "you find on the right records. Do not change stages yourself: propose them with gtm_intake so "
                         "the person can approve. If you logged nothing and have nothing to propose, reply with exactly "
                         "[SILENT]; otherwise say what you logged in one line and that the proposals are waiting."),
        slots=[{"name": "hours", "type": "text", "label": "Which hours? (cron hour list)", "default": "9,12,15,18"},
               {"name": "days", "type": "text", "label": "How many days back?", "default": "2"}, DELIVER],
        tags=["gtm", "email", "activity"],
    ),
    dict(
        name="market-watch",
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
               {"name": "time", "type": "time", "label": "What time on Mondays?", "default": "07:30"}, DELIVER],
        tags=["gtm", "market", "weekly"],
    ),
    dict(
        name="sheet-mirror",
        title="Mirror the desk to a Google Sheet",
        description="A few times a day: write the whole pipeline to a tab of your Google Sheet, for everyone who lives in Sheets.",
        category="work",
        schedule_template="0 {hours} * * 1-5",
        prompt_template=("Load the skill stoa:desk and follow its Google Sheets section to mirror the desk to the tab \"Stoa\" "
                         "of this Google Sheet: {sheet}. If it worked, reply with exactly [SILENT]. If it did not, say what "
                         "failed in one line."),
        slots=[SHEET, {"name": "hours", "type": "text", "label": "Which hours? (cron hour list)", "default": "8,12,16"}, DELIVER],
        tags=["gtm", "sheets"],
    ),
    dict(
        name="sheet-pull",
        title="Bring Sheet edits back to the desk",
        description="Once a day: read the Google Sheet and propose what people changed there, for you to approve.",
        category="work",
        schedule_template="{minute} {hour} * * 1-5",
        prompt_template=("Load the skill stoa:desk and follow its Google Sheets section to read the tab \"Stoa\" of this "
                         "Google Sheet: {sheet}, and propose its edits with gtm_intake. Do not apply them. If nothing would "
                         "change, reply with exactly [SILENT]; otherwise show the review and say it is waiting."),
        slots=[SHEET, {"name": "time", "type": "time", "label": "What time?", "default": "17:00"}, DELIVER],
        tags=["gtm", "sheets"],
    ),
    dict(
        name="friday-review",
        title="Friday pipeline review",
        description="Every Friday: the week in the pipeline, what is stuck, and the three records to push on Monday.",
        category="work",
        schedule_template="{minute} {hour} * * 5",
        prompt_template=("Load the skill stoa:desk. Call gtm_pipeline(view=\"brief\") and gtm_pipeline(view=\"activity\", "
                         "days=7). Write the week's pipeline review: what was won and lost and why, where records are "
                         "piling up by stage, and the three records most worth pushing on Monday with the reason for "
                         "each. Under 250 words."),
        slots=[{"name": "time", "type": "time", "label": "What time?", "default": "15:30"}, DELIVER],
        tags=["gtm", "weekly"],
    ),
]


def titled(name: str) -> str:
    """The title a blueprint goes by in /blueprint."""
    return next((bp["title"] for bp in BLUEPRINTS if bp["name"] == name), name)
