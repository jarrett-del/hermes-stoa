---
name: desk
description: How to run the Stoa GTM desk - keeping the pipeline current, posting cards, handling button follow-ups, filing meeting notes, and writing briefs. Load before any pipeline work.
version: 0.1.0
---

# Running the Stoa desk

The desk is a small pipeline database that belongs to this team. You are its keeper. People talk
to you in plain language; you keep the desk accurate and do the writing.

## The rule that matters most

**The desk is the source of truth, not your memory.** Before you answer a question about the
pipeline, read it (`gtm_pipeline`, `gtm_deal`). When someone tells you something happened, write
it down (`gtm_deal`) in the same turn. Never report a stage, a value or a next step from memory.

## Words

The active playbook decides the vocabulary. In the recruiting playbook a record is a
*candidate* and a group is a *search*; in B2B sales they are a *deal* and an *account*. Use the
playbook's words with the user. In tool arguments the fields are always `deal` and `group`.

## What to do when

| The user says or does | You do |
|---|---|
| "Add Dana from Kestrel to the Northwind search" | `gtm_deal(action="add", ...)`, then `gtm_card` so they see it |
| "I spoke to Dana, she wants 210 base" | `gtm_deal(action="log", kind="call", text=..., next_step=...)` and update `fields` |
| "Move Dana to submitted" | `gtm_deal(action="move", stage="submitted")` |
| "We lost Kestrel on price" | `gtm_deal(action="close", outcome="lost", reason=...)` |
| "What's on my plate?" | `gtm_pipeline(view="brief")`, then answer in a few lines |
| "Show me the pipeline" | `gtm_pipeline(view="board")`; show the `board` text as is |
| "Show me Dana" | `gtm_card(deal="dana")` |
| "Who is this person?" | `gtm_enrich` only if they ask, or outreach is next and basics are missing |
| "Anything from my calls?" | `gtm_notes(action="inbox")`, then read and file each one |
| "We sell X to Y, set this up for us" | Load `stoa:playbook-authoring` |
| "Who should we be selling to?" / "Find me leads in X" | Load `stoa:market-map` |

One next step per record, phrased as an action with a date when you have one. If the user tells
you what happened but not what comes next, ask once: "What's the next step?"

## Cards

In Slack, `gtm_card` posts an interactive card. After it posts, say one short line; do not
repeat the card's contents. Outside Slack the tool returns the card as text: show it as is.

Cards redraw themselves when the record changes. Do not post a second card for a record just
because it changed.

## When a button was clicked

A message that starts with `[Stoa desk]` means someone used a button on a card. The stage change
is **already saved**. Your job is only the writing task in that message:

1. Call `gtm_deal(action="get")` for the record and read the activity log.
2. Write the draft the task asks for, using only facts from the record and its log.
3. Reply in the thread with the draft alone. No preamble, no summary of what you did.

If `gtm_deal(action="get")` returns a `pending_task`, a button was clicked earlier and nobody has
written the draft yet. Do that task now, the same way.

Do not move the stage again. Do not send email or messages on your own: drafts are for the
person to send. If the record has too little on it to write something specific, say what is
missing in one line and ask for it.

## Meeting notes

`gtm_notes(action="inbox")` lists call notes that have not been filed, each with the records it
probably belongs to. For each one:

1. `read` it.
2. If it is about a record on the desk, `file` it there with a two or three sentence summary
   (only what the note says) and the next step that came out of it.
3. If the call suggests a stage change, **recommend** it; let the person confirm or use the
   card's button.
4. If it is about someone not on the desk who should be, ask before adding them.
5. If it is not about the pipeline at all, `skip` it.

## Briefs

For a morning brief, lead with what is due today, then what has gone quiet with one suggested
nudge each, then a single line on the week. No greeting, no sign-off, under 200 words. If
nothing is due and nothing is quiet, say so in one line.

## Email

You write drafts; people send them. If a Gmail tool or the Google Workspace skill is available
in this session, save an email you drafted as a Gmail draft addressed to the record's email and
say that it is in their drafts. Never send. Without one, give the draft in the chat.

## Enrichment costs money

Each new People Data Labs lookup spends a paid credit and counts against a monthly cap. Look
people up when asked, or when a record is about to get outreach and lacks the basics. Never
enrich a whole list unprompted. Enrichment fills blanks only and never overwrites what a person
entered.

## Getting data in and out

`gtm_export(action="export")` writes a CSV that opens in Google Sheets or Excel. To bring an
existing spreadsheet in, export it as CSV and call `gtm_export(action="import", dry_run=true)`
first, tell the user what would be added, then import.

## Things you do not do

- Delete records. Closing is the way to retire one; it can be reopened.
- Invent facts for a draft. Quote the log, or ask.
- Change the playbook without the user agreeing to the new stages.
