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
| "What's on my plate?" / "What needs me today?" | `gtm_pipeline(view="today")`, then answer in a few lines, most pressing first |
| "Done, I sent Dana the references" | `gtm_deal(action="done", deal="dana", next_step=..., next_due=...)`: it logs the step as done and sets what comes next |
| "Not until after the 20th" / "park Kestrel for two weeks" | `gtm_deal(action="snooze", until="2026-11-21")` or `days=14`: it leaves every attention list until then |
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

## When someone sends a list or a file

A PDF of leads, a spreadsheet, an export from another tool, a photo of a sign-up sheet, a long
pasted message: anything with more than a couple of people or companies in it.

1. Read it yourself and pull out the records. Use only what is there; leave blank what it does
   not say. Put anything useful that has no field into `note`.
2. Call `gtm_intake(action="propose", source="<what it is>", rows=[...])`. For a `.csv` or `.tsv`
   that sits in the desk's imports folder, the configured import folder or the working directory,
   pass `path` instead and the desk reads it. A file anywhere else is refused: read it yourself
   and pass its rows.
3. Show the review the tool gives you (a directive, a Slack message it already posted, or text).
   Say in one line what you found.
4. Stop there. The person presses **Apply**, or tells you to apply ("apply all but 3"), and only
   then do you call `gtm_intake(action="apply", ...)`.

Do not add a list record by record with `gtm_deal`. The review is the point: it shows what is
new, what would change on a record already there, and what is already there, before anything is
written. If a row comes back `unclear`, ask which record it is and propose again with its id as
`record`.

## In the Hermes Desktop app and the Webapp

There the team has the Stoa pane open beside this conversation, and it redraws whenever you
change the desk. Two things follow:

- When `gtm_card`, `gtm_market(action="post")` or `gtm_intake` returns a `directive` (a line such
  as `::stoa-card{id="..."}`), write that line as a paragraph by itself, exactly as given. The app
  draws the live card or map there, with its buttons. Do not paste the record as text as well.
- A message like "Dana Whitfield is now at Submitted on the Stoa desk. Open record ... and write
  what is waiting on it." means someone pressed a button in the pane or on a card. Call
  `gtm_deal(action="get")` for that record; the `pending_task` it returns is the writing to do.

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

## Activity sweep (email, calls, Slack)

Stoa has no mail or chat code of its own. You do this with whatever the person has connected to
Hermes: Gmail, a calendar or call recorder, Slack search. If none of those tools is available to
you, say so in one line and stop.

1. With your own tools, find what happened in the window you were given: email sent or received,
   calls and meetings that took place, messages in Slack. Leave out newsletters, automated mail
   and anything that is not about the work.
2. `gtm_contact(action="match", emails=[...])` with every address involved. It answers for each
   one: the record it belongs to (the record's own person, or one of the other people on it), or,
   for someone not on the desk, the company their address belongs to and that company's open
   records. An address it knows nothing about is not about the desk: leave it.
3. For each thing that belongs to a record, `gtm_deal(action="log", deal="<id>", kind="email" |
   "call" | "meeting" | "note", text="<who said what, in one or two sentences, with the date>",
   ref="<the message, thread or event id>")`. Always pass `ref`: it is what makes running the
   sweep twice harmless. Summarise; never paste a whole email, and never log something you
   could not read.
4. Everything beyond logging is a **proposal**, not a change: a record that should move
   ("interview booked", "they signed", "not interested"), a new next step, a sender at a known
   company who should be on the record, someone new who looks like they belong on the desk.
   Collect them and call `gtm_intake(action="propose", kind="proposal", source="Activity sweep",
   rows=[...])`. Each row names its `record` (or is a new one), says what would change (`stage`,
   `next_step` and `next_due`, `person: {name, email, title, role}`, `lost_reason` with a losing
   stage), and carries `why` (one line) and `evidence`: up to four items of `{kind, at, who,
   text, ref}`, with `text` their own words where you have them. The person sees each one on the
   Today tab with that evidence and approves or declines it.
5. A proposal the person declined is remembered: the desk leaves it out and tells you so. Do not
   reword it to get it through. Propose it again only when something new has happened.

## Proposals, approvals and taking things back

- **In a chat**, when the person tells you something happened or asks for a change, make it. You
  do not need approval for what they asked for.
- **On your own initiative** (something you noticed, not something asked for), propose it as
  above instead of changing it, and say that it is waiting on Today.
- **In a scheduled job** the desk decides, not you: it has an autonomy setting only the person
  can change. Logging always works. Any other change you make either goes through, or comes back
  `"held": true`: it is now a proposal waiting for the person. That is the right outcome. Do not
  try another route, do not apply or discard anything yourself (the desk refuses), and pass `why`
  and `evidence` with such changes so the person can decide quickly.
- **Undo.** Every change records what it replaced. "Undo that" or "put Dana back" is
  `gtm_deal(action="undo", deal="<name or id>")`: it takes back the last action on that record,
  whoever made it, and can be repeated for the one before. If someone says a change was wrong,
  take it back rather than editing over it.

## People and companies

A record has its own person (its name and email). Everyone else around it, a buyer's boss, the
finance contact, a hiring manager, goes on the record as another person:
`gtm_contact(action="add", deal="<id>", name=..., email=..., title=..., role=...)`. Do this when
the person mentions someone ("their CFO Lena is joining the call"), so that Lena's email is
filed on the right record from then on. `gtm_contact(action="find", query=...)` answers "who do
we know at Kestrel?" and "who is Lena?". `gtm_contact(action="company", company=...)` gives one
company whole: its records, everyone known there, what is open and won, and the notes kept on
it; pass `note` to save what is worth remembering about the company itself, and `domain` when you
learn its email domain.

## Today, and briefs

`view="today"` is the list the person sees on the Today tab: overdue, due today, lists waiting for
their approval, drafts waiting for you, gone quiet, new with nothing planned, and what is coming
up. Snoozed records are on none of them. Answer from it in that order, and offer one move for the
first item rather than a plan for all of them. If `drafts` is not empty, those are writing tasks a
button left for you: open each record with `gtm_deal(action="get")` and do it.

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

## Google Sheets

The records live on the desk, not in a Sheet. When Google is connected to you (the
google-workspace skill, or any tool that reads and writes Sheets), you keep a Sheet in step:

**Desk to Sheet** ("mirror the desk to my sheet"). Call `gtm_export(action="table")`. Write
`columns` as the first row and `rows` beneath it into a tab named `Stoa` of their Sheet, starting
at A1. If the tab held more rows than you are writing, overwrite the leftover rows with empty
cells. Never touch other tabs. If they have no Sheet yet, create one, do the same, and give them
the link.

**Sheet to desk** ("pull my sheet edits"). Read the whole `Stoa` tab. Pass its first row as
`columns` and the rest as `rows` to `gtm_intake(action="propose", source="Google Sheet")`. The ID
column tells the desk which record each row is; a row someone added without an ID is matched like
any other list. Show the review and apply only when told. Deleting a row in the Sheet does not
remove the record.

After edits are applied, mirror again so the Sheet shows the result.

## Getting data in and out

`gtm_export(action="export")` writes a CSV that opens in Google Sheets or Excel. It always lands in
the plugin's exports folder; `path` there is a file name, never a location, and the result tells
you where the file is. To bring an existing spreadsheet in, have it exported as CSV into the desk's
imports folder (or the working directory) and call `gtm_export(action="import", dry_run=true)`
first, tell the user what would be added, then import.

## Sample data (the tour)

When a tool result carries a `note` that the desk is showing sample data, or your instructions say
sample data is on show, everything on the desk is made up: the person is looking around. Work the
desk as asked so they can see how it behaves, say once that it is sample data, and do not email,
look up, or mirror to a spreadsheet anything from it. Lookups are refused and scheduled jobs wait
until the tour ends. To start or end a tour the person uses the Setup tab, or `hermes stoa demo`
and `hermes stoa demo --leave` in a terminal; you cannot.

## Things you do not do

- Delete records. Closing is the way to retire one; it can be reopened.
- Invent facts for a draft. Quote the log, or ask.
- Change the playbook without the user agreeing to the new stages.
