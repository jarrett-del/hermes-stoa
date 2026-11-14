# Using Stoa

How the desk works day to day. For everything in the plugin, tool by tool, see [the reference](reference.md); for what it does not do yet, [the limits](limits.md).

## Any pipeline, and two worked examples

A pipeline has one of three shapes, named for how the work moves:

| | Close deals | Fill roles | Generate leads |
|---|---|---|---|
| **For** | B2B and SaaS sales, agency and consulting new business, partnerships, fundraising | Contingency and contract recruiting, retained and executive search | Outbound and SDR agencies, fractional sales leaders working for clients, appointment setting |
| **A record is** | a deal with a person at a company | a candidate in a search, for a client | a lead in a campaign, for a client |
| **Billed** | when a deal is won, or monthly on the account | on placement, or monthly on the client | monthly on the client; a lead is never invoiced |

The first time you open the page you pick one, then set its stages in your own words: rename them,
put them in your order, add or take away (two to seven), and say what a win and a loss are called.
Not sure? Describe your business and Hermes sets the stages for you. **Setup → Edit stages** changes
them later, and **+ Pipeline** opens another pipeline the same way: contract work beside contingency,
your own sales beside the leads you find for clients. Two pipelines of the same shape can each have
their own stages.

Underneath, each shape runs on a playbook (`b2b-sales`, `recruiting`, `client-leads`; `agency` and
`partnerships` still ship for desks that use them), and a pipeline whose stages you changed gets its
own copy. Hermes can write a whole playbook from a short conversation when you want more than stages.

The first time you open the page, it asks what the pipeline is for, and offers a look around first:

![The first time you open the desk](desktop-welcome.jpg)

The sample data shows two businesses at a realistic size. Everything in it is made up.

| | A recruiting agency | A B2B software company selling a fraud prevention tool |
|---|---|---|
| **Tour it** | `hermes stoa demo recruiting` | `hermes stoa demo saas` |
| **A record is** | a candidate in a client's search | a deal with a buyer at an account |
| **Stages** | Sourced → Contacted → Screened → Submitted → Client interview → Offer → Placed | Lead → Qualified → Discovery → Proposal → Negotiation → Closed won |
| **On the desk** | 24 open candidates across 8 searches, **$1.2M** in fees, plus 3 placed and 5 passed | 28 open deals, **$2.5M**, plus 4 won and 6 lost |
| **Its market map** | Where the candidates for one search work now: 10 companies, and adding one puts its named person in that search | Fintechs and marketplaces exposed to payment fraud: 14 companies, what each uses today, and who to call first |

A tour never touches your own desk. The sample data lives in a file of its own, with its own
playbook; every surface says it is sample data while it is on show; lookups are off and scheduled
jobs wait. **Leave the tour** on the page, or `hermes stoa demo --leave`, brings your desk back
exactly as it was and throws the sample file away. (Versions before 0.10 loaded the examples into
the desk itself. If yours did, the Setup tab offers to take them out, and so does `--leave`.)

The recruiting agency:

![The recruiting example: $1.2M in open fees across eight searches](desktop-pipeline.jpg)

The company selling a fraud prevention tool:

![The software example: $2.5M of open pipeline](desktop-saas.jpg)

## Talk to it, and watch the desk change

In the Hermes Desktop app and the Webapp, Stoa sits beside the conversation.

![The desk beside the conversation](desktop-pane.jpg)

- **A pane beside the chat.** What needs you today, every stage, the first calls from your map.
  Tell Hermes *"I spoke to Marcus, follow up Thursday"* and the pane redraws as the agent saves it.
  Narrow it by owner, by search or account, to what is due or what has gone quiet, or by any text.
  While the Stoa page is open the pane does not repeat it: it shows what is coming up this week and
  what has just changed on the desk, and a name pressed there opens on the page.
- **A click saves, then Hermes writes.** Press **Draft follow-up** or the stage button on a
  record. The change is saved at once, one line goes into the open chat as if you had typed it,
  and the draft comes back in the conversation.
- **Cards in the conversation.** Ask *"show me Dana"* and the reply contains the live card, with
  its buttons, not a description of it. *"Show me the market map"* draws the map the same way.
- **A page of its own.** **Stoa** in the sidebar opens on **Today**, then the funnel with its
  conversion rates and the pipeline board with the same filters, the market map with its reading,
  the activity log, and **Setup**.
- **Your name on it.** Under **Setup**, type your business name and it is drawn in dots at the head of
  the page, lit the way the stoa is: in place of the stoa, or beside it with the stoa kept. It is saved
  with the desk, so everyone who opens it sees the same name. Leave it empty and the stoa stays.
  Beside it, **Auto / Color / One ink** says how the stoa and the name are drawn: in the heat colors,
  or in the color of the page's own words. On Auto, which is how a desk starts, they are in color on a
  dark theme and in one ink on a light one. It changes those two and nothing else on the page.
- **Today.** What needs you, most pressing first: what is waiting for your approval, next steps
  overdue and due today, records gone quiet, and new ones with nothing planned. Each kind is a group
  that opens. Closed, it is one line that says how many and who; open, each row has one move, and
  the page remembers which groups you left open.
  **Done** logs the step and asks what comes next, **Draft it** has Hermes write what the step
  needs, **Snooze** puts it aside for a few days or until a date you pick, and until then it is on
  no list. What is coming up this week sits beside it.

![Today: everything that needs you, in groups that open, most pressing first](desktop-today.jpg)

- **Approvals.** Whatever Hermes suggests on its own is the first group on Today, one card each:
  what would change, why, and what it rests on (the email, the call, in their words). **Approve**
  writes it; **Decline** leaves the desk as it is, and the same suggestion is not made again. A
  list you were sent is reviewed there too, as a whole.
- **Undo.** Every change records what it replaced. After anything you do, **Undo** appears above
  the box at the foot of the page; a record's log and the Activity tab offer it for the last
  change on each record, whoever made it, you, Hermes or a button in Slack.

![An approved proposal, with the offer to take it back](desktop-undo.jpg)

- **A page for each record.** Press a record anywhere (Today, the board, Cash, Activity) and it
  opens on a page of its own, laid out the way a CRM is read. Across the top: its name, where it
  sits (at its company, in its search or account), its value and owner, the stages as one bar
  (press a stage to move it there), and the next step with the moves for it. Below, three
  columns: **Details** on the left (every fact, the empty ones shown so you can see what is not
  known yet; **Edit** changes any of them, the playbook's own included), **Notes** and
  **Activity** in the middle (a line at the top logs a call, an email, a meeting or a note), and
  what it is connected to on the right: the search or account it is in and the others there,
  others at the same company, the other people on it, and its money once there is any. **Back**
  returns to where you came from.
- **A page for each search, account or client.** Its details and notes, its records a stage at a
  time, the people on its side, and what happened lately. **+ New ... here** adds a record to it.
  Where an account or a client *is* the company (the B2B and agency playbooks), the company's
  email domain and everyone at it are on the same page, and there is no separate list of
  companies: one place to look. Where a group is a piece of work (a search, a campaign), a
  record's company has a page of its own as well.
- **Clients, and billing by the month.** Where a search or a campaign is done *for* a client
  (recruiting, lead searches), **Clients** on the pipeline lists them, and every "for Fernway" opens
  the client's page: its searches, the candidates in them, who is on their side, and how it is
  billed. Contract hours or a retainer are set up there (**Billing → Set terms**); each month that
  ends is then waiting on **Today**, worked out from the month's weekdays, to record as an invoice
  with the real hours, or to skip. Contingency fees stay on the placed candidate as before.
- **People.** A record has its own person (name, email, phone, LinkedIn) and can carry the others
  around it: the buyer's boss, finance, a hiring manager. Their email and calls are then filed on
  that record too.


- **Cash.** The other half of lead to cash, on its own tab. Five figures across the top: won this
  month, collected this month, invoiced and not yet paid, overdue, and won but never invoiced.
  Beneath them, what the open pipeline is likely to bring in, weighted by stage and laid out by
  expected close date, with anything past its date called out; where each won piece of work
  stands; and every open invoice with **Paid** and **Part** beside it.

![Cash: what came in, what is owed, and what is likely to come in](desktop-cash.jpg)

- **Invoices on the record.** A record's **Money** section holds its invoices: the amount, the
  number, when it is due, what has come in. Stoa does not send invoices or take payments; it
  keeps the account, so it can tell you what is late and what you forgot to bill.

![A won record with its invoice, part paid and overdue](desktop-cash-record.jpg)

- **Setup.** One tab that says where things stand: a short get-started list that ticks itself off,
  what is connected (a key shows as set or not set, never its value), the rules in force (which
  chats the desk answers in, who may press a Slack button, which folders it reads), the
  automations you can switch on, your playbook and the others, your data with a CSV export, and a
  cheat sheet of things to say, press and type.

![Setup: what is connected, the rules in force, and how to work the desk](desktop-setup.jpg)
- **Talk to the page.** A dock at the foot of the page takes the same sentences the chat does:
  *"Add Nadia Rahim from Larkspur to the Northwind search"*, *"what needs me today?"*. The
  conversation opens above the dock and the board redraws behind it. The page keeps its own
  conversation for this, called **Stoa desk**, listed in the sidebar like any other, one click
  away when you want the full chat.
  The dock takes sentences, not commands: a line that starts with `/` (such as `/blueprint` or
  `/gtm today`) is run by the app's own chat box, so the page opens a new chat with it typed
  and leaves it for you to send.
- **Say it or hand it over.** The microphone dictates into the dock where the browser has speech
  recognition. Drop a CSV anywhere on the page, or attach one, and the desk reads it itself and
  shows the review, with no model in between, whatever the headers are called. If it cannot tell
  which column is the name, Hermes reads the file instead. A small text file goes along with your message.
- **Or do it by hand.** Drag a card to another column to move it. **+ New** adds a record from a
  short form. **Edit** on any record changes its next step, stage, value, owner and the rest, and
  adds a line to its log. Press a stage in the funnel to pick out its column on the board. No model involved.

![Adding a candidate by hand on the page](desktop-new.jpg)

- **Lists you send in.** Drop a file into the chat. The review appears in the conversation and in
  the pane: a tick box per row, what each one would add or change, and one **Apply** button.

![A list waiting for review](desktop-intake.jpg)

Switch it on once under **Capabilities → Plugins → Stoa**. Every colour comes from the app's own
theme, accent included, so it looks like part of Hermes in whatever theme you run.

![The market map page, here for the fraud prevention example](desktop-market.jpg)

## What it looks like in chat

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

Two people clicking the same button at once move the stage once, not twice: the check and the
change are one database transaction, so it holds across Slack, the web tab and the agent.
