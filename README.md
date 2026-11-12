![Stoa. Where the day's business gets done.](docs/banner.jpg)

# Stoa

**The CRM for [Hermes Agent](https://github.com/NousResearch/hermes-agent): a lightweight one you talk to. Hermes runs it; you approve.**


A two-person sales team does not need a CRM with forty tabs. It needs to know who to sell to,
the pipeline kept honest, the follow-ups written, and the calls turned into next steps, in the
places the team already works. Tell it what happened, send it a list, or press a button, and it
keeps the pipeline. Stoa is that, in one plugin:

**Map the market → find the leads → run the pipeline.**

- **Map any market.** Tell Hermes what you sell. It segments the market, researches companies,
  scores each against your brief with the evidence, and posts the map. One tap on a company puts
  it in the pipeline and sets Hermes to finding the buyer and drafting the first note.
- **Send it anything.** A PDF of leads, a spreadsheet, a photo of a sign-up sheet. Hermes reads it,
  Stoa shows what it would add and what it would change, and nothing is written until you press Apply.
- **Click a button, the stage changes at once.** No model in the loop, no waiting, no wrong stage.
- **Hermes proposes, you approve.** What it finds in your email, calls and Slack is logged on the
  right record. Anything more (a stage it thinks should move, a person to add) waits for your yes
  or no with the evidence beside it, a no is remembered, and any change can be taken back.
- **Then Hermes does the writing.** The same click hands the follow-up (the outreach note, the
  proposal email, the decline) to the agent, in the card's thread.
- **One file fits it to your business.** A playbook names your stages, your button labels and
  what gets drafted at each step. Recruiting, B2B sales and agency playbooks ship with it, and
  Hermes can interview you and write a new one.

It grew out of a setup a recruiting agency runs its searches on every day. This is that idea,
rebuilt as a plugin anyone can install.

```
hermes plugins install stoa                        # from the Hermes plugin catalog
hermes plugins install jarrett-del/hermes-stoa     # or straight from this repository
hermes plugins enable stoa
hermes stoa demo saas     # optional: look around a worked example first (or: hermes stoa demo recruiting)
hermes gateway restart    # so the Slack buttons bind
```

Then, in any Hermes chat: *"Map the market for us"*, *"What's on my plate?"*, or `/gtm`.
In Slack the command is `!gtm` (Slack keeps the slash for its own commands), and
`!gtm Dana Whitfield` or `!gtm map` posts the card or the map itself, buttons included.

![Today: what is waiting for your approval, then what is due, with one move for each](docs/desktop-today.jpg)

![The pipeline board, here for the recruiting example](docs/desktop-pipeline.jpg)

![Searches: who each one is for, where it stands, and who is on the client's side](docs/desktop-searches.jpg)

![Cash: what came in, what is owed, and what is likely to come in](docs/desktop-cash.jpg)

## The first five minutes

1. Install and enable it (above) and restart the gateway.
2. In the Desktop app or the Webapp, switch it on under **Capabilities → Plugins → Stoa** and open
   the Stoa page. In a terminal or any chat, `/gtm` works straight away.
3. The first screen offers a tour of sample data (a recruiting agency, or a company selling
   software) or your own desk on the playbook that fits. A tour lives in a file of its own and never
   touches your desk.
4. Tell it something: *"We are starting a Staff Backend search for Northwind. Add Dana Whitfield,
   call her Friday."* Or drop a spreadsheet of leads on the page and approve what it would add.
5. Open **Setup** to see what is connected, what a scheduled job may do by itself, and how to work
   the desk. `/blueprint` switches on the sweeps that keep it current from your email, calls and
   notes; what they find waits for your approval.

## Read more

- **[Using Stoa](docs/guide.md)**: two worked examples, talking to it, and what it looks like in chat.
- **[What is in the plugin](docs/reference.md)**: the tools, market maps, intake, the pipeline,
  approvals and undo, cash, people, searches and accounts, playbooks, and every setting.
- **[The web dashboard tab](docs/dashboard.md)**.
- **[Limits](docs/limits.md)**: what it does not do yet.
- **[Development](docs/development.md)**: tests, and how it is put together.

## Disclosure

What the plugin does that you would want to know before installing it:

- **Network calls.** Only when you add the matching key, and only to three hosts:
  `api.peopledatalabs.com` (the name, email, LinkedIn URL or company of the one record being
  enriched, or a company name and job titles for a buyer lookup), `api.exa.ai` (the search
  phrase for a market-map search) and `public-api.granola.ai` (reads your note list and note
  summaries; transcripts are never requested). No telemetry, no usage reporting, no other
  outbound requests. Research the agent does with its own web tools is Hermes's, not the plugin's.
- **Dictation.** The microphone in the page's dock uses your browser's own speech recognition,
  not anything of the plugin's or the agent's. In Chrome that means the audio goes to Google's
  speech service while you dictate. It listens only while the button is on, and the button is not
  shown where the browser has no speech recognition.
- **Paid lookups.** People Data Labs charges per result. Every lookup counts against
  `pdl_monthly_cap`, and a buyer lookup returns at most five people.
- **Slack.** Cards are posted and updated through the Slack app Hermes is already connected
  with. The plugin never reads, stores or sees the Slack token. A button moves a stage, applies a
  list and can spend a paid lookup, so a press is taken only from the person the card was posted
  for, or, when you list people in `slack_users`, only from them, and only in a chat the desk is
  available in. Anyone else gets a private "Not allowed" and nothing changes.
- **Credentials.** `PDL_API_KEY`, `EXA_API_KEY` and `GRANOLA_API_KEY` are read through Hermes's secret scope
  (`agent.secret_scope.get_secret`). The plugin reads no `.env` or credential file. A button click
  or a web-tab request arrives outside any chat turn; on a gateway serving several profiles the
  plugin then installs its own profile's scope around the read, the way Hermes does for its own
  background work (`set_secret_scope(build_profile_secret_scope(home))`), and removes it after.
- **Desktop app and Webapp.** `desktop/plugin.js` is one file with no build step. It imports only
  the Hermes plugin SDK and React and calls the plugin's own backend (`/api/plugins/stoa/`). After a
  click that calls for writing it sends one line into the open chat through `host.composer.submit`,
  the same as typing it. The dock on the page keeps one conversation of its own, named **Stoa
  desk**: it is created and resumed through the gateway (`host.request`: `session.create`,
  `session.resume`, `prompt.submit`), it is sent only what you type or dictate there (with a short
  note of context on its first message, which you can read in that conversation), and the page listens
  to the gateway's events for that conversation alone to show you the reply. A file is read only
  when you pick it or drop it on the page: a CSV's text goes to the plugin's backend to be held as a
  review, and a small text file goes to the agent with your message. It does not touch the app's
  DOM or other plugins' storage, and it checks in with the backend every 30 seconds while open so
  the agent knows a card can be drawn in the conversation.
- **Web dashboard.** The tab's backend adds routes under `/api/plugins/stoa/`, behind the
  dashboard's own sign-in. They read and change the pipeline, start and end a tour of the sample
  data, hand the pipeline to your browser as a CSV download when you press Export, and switch the
  active playbook (the plugin's own `playbook` setting, saved through `ctx.set_config`; where the
  page cannot save it, it asks Hermes to in the dock instead). They call no model and no outside
  service, except that the Enrich button makes the same People Data Labs lookup as in Slack.
- **The Setup tab.** It shows, to anyone signed in to your Hermes, which chats and Slack members
  the desk is limited to, the folders it reads and writes, where its database file is, and whether
  each key is set. It never shows a key or a token.
- **Sample data.** A tour writes two things in the plugin's own data folder: `tour.json`, which
  says a tour is on, and one `tour-<id>.db` holding the made-up records. Both are deleted when
  you leave. While they are there the plugin does not read or write your own `desk.db`, apart
  from counting what is in it for the Setup tab.
- **Files outside the plugin's own data.** It reads Markdown and text files in the folder you
  set as `notes_dir`. By path it reads only `.csv` and `.tsv` files, and only from the plugin's
  own `imports/` folder, the folder you name in `import_dir`, and the working directory when that
  is a project folder; a path the agent passes anywhere else is refused. (At a terminal,
  `hermes stoa import <file>` reads the CSV you name.) It writes nowhere outside its own data
  folder: exports (the pipeline as CSV, a market map as a web page) always go to its `exports/`
  folder, and a file name is all the agent can choose.
- **Messages to the agent.** With `allow_gateway_injection` granted, a button click queues one
  instruction for the agent in the card's thread. The agent drafts; it is told not to send.
- **Stored data.** Names, emails, notes and enrichment results for the people in your pipeline,
  the other people you add to a record or to a search or account (the client's side), what you
  keep on a search or account itself (its client, its terms, its notes), and the invoices and
  payments you record (amounts, numbers and dates; no bank or card details, which Stoa never asks
  for) sit unencrypted in `plugin-data/stoa/desk.db`.
- **Daily copies.** Once a day, the first time the desk is opened, a copy of that file is written
  beside it in `plugin-data/stoa/backups/` and the fourteen newest are kept. They are as
  unencrypted as the file itself, and they are on the same disk: they guard against a mistake, not
  against losing the machine.
  Every change is kept on a ledger in that file with the values before and after it, and the
  ledger is never trimmed; a record or a person taken off the desk is archived there, not erased;
  what you declined is kept for 90 days; evidence quoted in a proposal is kept with it. To erase
  someone for good, delete the file or edit it with a SQLite tool. Treat it like the customer
  list it is.
- **What runs without you.** A scheduled job reaches the desk only if you list `cron` in
  `channels`, and then changes only what the autonomy setting allows; the rest is held for you.
- **No shell commands, no background processes, no self-updates, no Python dependencies.**

## License

MIT
