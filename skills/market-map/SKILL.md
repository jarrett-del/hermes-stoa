---
name: market-map
description: How to map a market and turn it into leads with the Stoa desk - define who the team sells to and what a fit looks like, research companies against those requirements with evidence, read the map back, name the first calls, and move the best ones into the pipeline. Load when the user asks who to sell to, wants leads, or wants a market mapped or shared.
version: 0.4.0
---

# Mapping a market

A market map answers "who should we be selling to?" before the pipeline answers "where does
each deal stand?". It is a named set of companies, sorted into segments. Every company is
checked against the same short list of requirements, with the evidence, and carries what it
uses today and who buys. The desk reads the map back for you: how rare each requirement is, who
holds the market, where segments meet, and who to call first.

The loop: **define → research → add → read → first calls → track → outreach.**

## 1. Define the market

If the user has not said, ask in one message:

- What do you sell, and what problem does it solve?
- Who buys it: what kind of company, how big, where?
- Which customers have been your best, and what did they have in common?

Then `gtm_market(action="define")` with:

- `name`: specific enough to research. "Regional freight brokers, US Midwest", not "logistics".
- `brief`: two or three sentences on what makes a company a fit.
- `segments`: three to six groups that would each get a different pitch. Segment by something
  observable (business model, size band, who the buyer is).
- `requirements`: three to six things a fit company shows, each one checkable from public
  information. "50 to 500 people", "Runs its own warehouse", "Has a compliance team". These are
  what you will check every company against, so write them as facts, not opinions.

## 2. Research

Work one segment at a time. If `gtm_market(action="search")` is available (Exa is connected),
use it with a plain description of the segment. Otherwise use your web search tool. Good places
to look: industry directories and association member lists, "top N" lists, conference exhibitor
lists, funding news, job boards (a company hiring for the problem is a buying signal).

Aim for 5 to 10 companies per segment on a first pass. Twenty checked ones beat a hundred guesses.

## 3. Add companies, with evidence

`gtm_market(action="add", companies=[...])`. For every company record what you found:

- `requirements`: an object keyed by each requirement's text. Give the evidence as a short
  string when the company shows it, and an empty string when you looked and it does not. Leave
  a requirement out only if you could not check it.
- `fit` from 1 to 5. Leave it out and it is read off the requirements. Set it yourself when you
  know something the requirements miss. A map of all fives is useless.
- `why`: one sentence of evidence. Never invent it.
- `signal`: a reason to reach out now (funding, hiring, a launch, a new executive).
- `uses`: what it uses today for the job you do. A vendor's name, "Spreadsheets", "In-house
  tool". This is how the map shows who you are up against.
- `also`: a second segment it could just as well sit in. These are the bridges between segments.
- `people`: likely buyers you came across, each with name and title.
- `source`, `size` ("220 people") and `location` ("City, State"). Leave blank what you could not find.

Adding a company that is already on the map updates it, so add again when you learn more.

## 4. Read the map back

`gtm_market(action="show")` returns the companies and what they add up to:

- `requirements`: the share of checked companies showing each one. A low number means that
  requirement is scarce: either relax it or make it the opening question on a call.
- `holds`: what companies use today. If most use spreadsheets, the pitch is against the
  spreadsheet, not a competitor.
- `bridges`: companies between two segments, the second pool when a segment runs thin.
- `regions`, `sizes`, and per-segment share and average fit.

Then save your interpretation with `gtm_market(action="reading")`:

- `reading`: one or two short paragraphs on what the map shows. Only what the numbers support.
- `implications`: up to four things to do about it.
- `first_calls`: three to seven companies to approach first, each with a `why` that someone
  could say out loud on the call. Spread them across segments.

## 5. Show it

- In Slack: `gtm_market(action="post")` puts the map in the channel with an **Add to pipeline**
  button on each company.
- Elsewhere: present the `map` text from `show` as is.
- To send it to someone: `gtm_market(action="export")` writes one self-contained web page and
  returns its path. Pass `share=true` for a version without buyers' names or pipeline status.

Finish with the three first calls and why, in two or three lines.

## 6. Into the pipeline

When the user picks companies (or taps the button), they become records at the first stage of
the playbook, with the first known buyer as the contact. Do it with
`gtm_market(action="track", targets=[...])`; never re-create them with `gtm_deal(action="add")`.

A message starting `[Stoa desk]` that says someone added a company from the map means the record
already exists. Do the task in that message:

- If the map named a buyer, confirm they are the right person and draft the first note.
- If not, find one. With People Data Labs connected, `gtm_market(action="people", target=...,
  titles=[...], limit=3)`; each person returned costs a credit, and whoever it finds is saved
  to the map. Otherwise use web search, then record them with `gtm_deal(action="update")`.
- Reply with who and why, and a first-touch note under 90 words that leads with the evidence.

## Keeping a map alive

- `gtm_market(action="dismiss", targets=[...], reason=...)` for companies that are not a fit.
  They stay recorded so they do not come back.
- The *Market watch* automation re-checks a map every week and adds at most five new companies.
- After adding a batch, update the reading and first calls if the picture changed.
- One map per market. A second product or a second region gets its own map.

## What not to do

- Do not record evidence you did not find. An empty requirement is useful; an invented one is not.
- Do not track a whole segment unprompted. The team chooses what enters the pipeline.
- Do not run paid people lookups across a map. One company at a time, when someone asks or taps.
- Do not send the full export outside the team. Use `share=true` for that.
