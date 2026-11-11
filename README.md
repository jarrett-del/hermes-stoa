![Stoa](docs/banner.png)

# Stoa

**A go-to-market desk for small teams, inside [Hermes Agent](https://github.com/NousResearch/hermes-agent).**

> *στοά*: the covered walk beside the agora, where the day's business was done.

A two-person sales team does not need a CRM with forty tabs. It needs the pipeline kept honest,
the follow-ups written, and the calls turned into next steps, in the place the team already
talks. Stoa gives Hermes a small pipeline of its own and puts it in Slack as cards with buttons:

- **Click a button, the stage changes at once.** No model in the loop, no waiting, no wrong stage.
- **Then Hermes does the writing.** The same click hands the follow-up (the outreach note, the
  proposal email, the decline) to the agent, in the card's thread.
- **One file fits it to your business.** A playbook names your stages, your button labels and
  what gets drafted at each step. Recruiting, B2B sales and agency playbooks ship with it, and
  Hermes can interview you and write a new one.

It grew out of a setup a recruiting agency runs its searches on every day. This is that idea,
rebuilt as a plugin anyone can install.

```
hermes plugins install stoa
hermes plugins enable stoa
hermes stoa demo          # optional: seven fictional records to try it on
```

Then, in any Hermes chat: *"What's on my plate?"* or `/gtm`.

## What it looks like

A card in Slack (shown here as the text version every other surface gets):

```
Dana Whitfield · Kestrel Labs
▰▰▰▱▱▱  Screened · 3 of 6
Search: Staff Backend · Northwind
Fee: $42k
Current title: Senior Backend Engineer
Comp expectation: $210k base
Next: Send submission note to Northwind (due 2026-11-10)

[ Submit to client ]  [ Draft follow-up ]  [ Enrich ]  [ Passed… ▾ ]  [ ⋯ ]
```

Press **Submit to client** and three things happen, in this order:

1. The record moves to *Submitted* and the activity log records who clicked. Instant, in SQLite.
2. The card redraws itself, in every channel it was posted to.
3. Hermes gets a turn in the card's thread with the playbook's instruction for that stage
   (*"Draft the submission note to the client: three bullet points on fit, comp and
   availability, and one honest risk"*) and replies with the draft.

Two people clicking the same button at once move the stage once, not twice.

## What you get

| | |
|---|---|
| **7 tools** | `gtm_pipeline`, `gtm_deal`, `gtm_card`, `gtm_enrich`, `gtm_notes`, `gtm_playbook`, `gtm_export` |
| **Slack cards** | Advance, Draft follow-up, Enrich, close with a reason, snooze, reopen |
| **`/gtm`** | The board, the brief, or one record, on every surface Hermes runs on (Slack, Telegram, CLI, Desktop…) |
| **`hermes stoa`** | Status, board, brief, sample data, CSV import and export from a terminal |
| **3 automations** | Morning brief, meeting-notes sweep, Friday review, under `/blueprint` |
| **2 skills** | `stoa:desk` (how the agent runs the desk) and `stoa:playbook-authoring` |
| **3 playbooks** | `b2b-sales`, `recruiting`, `agency`, plus any you or the agent write |

### The pipeline

Everything lives in one SQLite file under `<hermes home>/plugin-data/stoa/desk.db`: records,
their activity log, which cards were posted where, and the enrichment cache. It is per profile,
it survives plugin updates, and you can open it with any SQLite tool.

Talk to it in plain language. *"Add Dana from Kestrel to the Northwind search." "I spoke to
Marcus, he wants 210 base, follow up Thursday." "We lost Lumen on price."* The agent writes each
of those to the desk in the same turn; the bundled skill teaches it to treat the desk, not its
memory, as the source of truth.

### Playbooks

A playbook is a short YAML file:

```yaml
name: recruiting
terms: {deal: candidate, deals: candidates, group: search, groups: searches, value: Fee}
stages:
  - {id: screened, label: Screened, stale_days: 3, advance: Submit to client}
  - {id: submitted, label: Submitted, stale_days: 4, advance: Client interview}
won:  {id: placed, label: Placed}
lost: {id: passed, label: Passed, reasons: [Not a fit, Comp mismatch, Client passed]}
drafts:
  on_enter:
    submitted: >
      Draft the submission note to the client for {name}: three bullet points on fit, comp and
      availability, and one honest risk. Quote only what is in the notes.
```

`stale_days` decides when a record counts as gone quiet at that stage. `advance` is the label on
the button. `drafts.on_enter` is what Hermes writes when a record arrives at a stage.

To make your own, tell Hermes how you sell: *"We install commercial solar. Set the desk up for
us."* It asks a few questions, shows you the stages, and saves the playbook. Or copy one from
[`playbooks/`](playbooks/) into `<hermes home>/plugin-data/stoa/playbooks/` and edit it.

### Meeting notes

Point `notes_dir` at any folder of Markdown or text call notes (an Obsidian folder, the export
folder of an open-source meeting recorder, files you drop in by hand), or add a Granola API key.
`gtm_notes` lists the notes that have not been filed and which record each one is probably
about. The agent reads a note, files a short summary on the right record with the next step,
and recommends a stage change for you to confirm. The *File meeting notes* automation does this
a few times a day.

### Enrichment

With a [People Data Labs](https://www.peopledatalabs.com/) key, the **Enrich** button and
`gtm_enrich` look up a person or company and fill the record's blank fields. It never overwrites
what a person typed, results are cached for 90 days, and a monthly cap (default 50 lookups)
keeps the bill predictable.

### Spreadsheets

`gtm_export` writes the whole pipeline to a CSV that opens in Google Sheets or Excel, and reads
one back in (with a dry run first). That is how you move in from a spreadsheet, and how you get
a copy out for a board meeting.

## Settings

All optional. Set them in the Desktop app under **Settings → Plugins → Stoa**, or in
`config.yaml` under `plugins.entries.stoa.settings`.

| Setting | Default | What it does |
|---|---|---|
| `playbook` | `b2b-sales` | Which playbook the desk runs on |
| `channels` | empty | Chat ids where the desk is available; empty means everywhere |
| `notes_dir` | empty | Folder of call notes to watch |
| `notes_days` | `7` | How far back the notes inbox looks |
| `pdl_monthly_cap` | `50` | Most paid lookups per calendar month (0 = no cap) |
| `PDL_API_KEY` (secret) | empty | Turns on enrichment |
| `GRANOLA_API_KEY` (secret) | empty | Adds Granola notes to the inbox |

One permission is worth granting. For a button click to start the agent's writing turn, Hermes
needs to let the plugin queue a message:

```yaml
plugins:
  entries:
    stoa:
      allow_gateway_injection: true
```

Without it everything else still works: the stage moves, the card updates, and the card's thread
gets a line saying *"Saved. Reply here and I'll write it up."*

Hermes lets a plugin continue a conversation a person started, never open one. So a card you
asked for in a conversation hands its drafts straight back to that conversation. A card that
arrived on its own (from a scheduled job, say) parks the writing task on the record and waits
for your reply in its thread; the task is picked up the moment you answer.

## Disclosure

What the plugin does that you would want to know before installing it:

- **Network calls.** Only when you add the matching key, and only to two hosts:
  `api.peopledatalabs.com` (the name, email, LinkedIn URL or company of the one record being
  enriched) and `public-api.granola.ai` (reads your note list and note summaries; transcripts
  are never requested). No telemetry, no usage reporting, no other outbound requests.
- **Slack.** Cards are posted and updated through the Slack app Hermes is already connected
  with. The plugin never reads, stores or sees the Slack token.
- **Credentials.** `PDL_API_KEY` and `GRANOLA_API_KEY` are read through Hermes's secret scope
  (`agent.secret_scope.get_secret`). The plugin reads no `.env` or credential file.
- **Files outside the plugin's own data.** It reads Markdown and text files in the folder you
  set as `notes_dir`, and a CSV at a path you (or the agent, on your request) pass to import.
  Export writes a CSV to the plugin's `exports/` folder unless a path is given.
- **Messages to the agent.** With `allow_gateway_injection` granted, a button click queues one
  instruction for the agent in the card's thread. The agent drafts; it is told not to send.
- **Stored data.** Names, emails, notes and enrichment results for the people in your pipeline
  sit unencrypted in `plugin-data/stoa/desk.db`. Treat that file like the customer list it is.
- **No shell commands, no background processes, no self-updates, no Python dependencies.**

## Limits in 0.1

- Buttons are Slack-only. Other platforms get the text card and `/gtm`.
- One Slack workspace per profile.
- There is no direct Google Sheets sync yet; CSV is the bridge.
- A team shares one desk. There are owners on records, but no per-person permissions.

## Development

Standard library only. The desk logic has no Hermes imports, so the tests run anywhere:

```
python3 -m unittest discover -s tests     # 48 tests, no network
hermes plugins validate . --install-deps
hermes plugins doctor . --ci
```

| File | What it holds |
|---|---|
| `__init__.py` | `register(ctx)`: tools, Slack handlers, `/gtm`, CLI, blueprints, skills |
| `desk.py` | Everything the desk can do |
| `store.py` | The SQLite ledger |
| `playbook.py`, `playbooks/` | Playbook loading, validation, and the built-ins |
| `cards.py` | Block Kit cards and their text twins |
| `slackdesk.py` | Posting cards and answering button clicks |
| `sources.py` | People Data Labs and meeting-note sources |

## License

MIT
