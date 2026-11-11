---
name: market-map
description: How to map a market and turn it into leads with the Stoa desk - define who the team sells to, segment the market, research companies with evidence, and move the best ones into the pipeline. Load when the user asks who to sell to, wants leads, or wants a market mapped.
version: 0.2.0
---

# Mapping a market

A market map answers "who should we be selling to?" before the pipeline answers "where does
each deal stand?". It is a named list of companies, sorted into segments, each with a fit score
and the evidence behind it. The good ones become pipeline records with one click.

The loop: **define → research → add → show → track → find the buyer → outreach.**

## 1. Define the market

If the user has not said, ask in one message:

- What do you sell, and what problem does it solve?
- Who buys it: what kind of company, how big, where?
- Which customers have been your best, and what did they have in common?

Then `gtm_market(action="define")` with:

- `name`: specific enough to research. "Regional freight brokers, US Midwest", not "logistics".
- `brief`: two or three sentences that say what makes a company a fit. You will score against it.
- `segments`: three to six groups that would each get a different pitch. Segment by something
  observable (business model, size band, who the buyer is), not by something you would have to
  guess.

## 2. Research

Work one segment at a time. If `gtm_market(action="search")` is available (Exa is connected),
use it with a plain description of the segment. Otherwise use your web search tool. Good places
to look: industry directories and association member lists, "top N" lists, conference exhibitor
lists, funding news, job boards (a company hiring for the problem is a buying signal).

Aim for 5 to 10 companies per segment on a first pass. Twenty good ones beat a hundred guesses.

## 3. Add companies, with evidence

`gtm_market(action="add", companies=[...])`. For every company:

- `fit` from 1 to 5 against the brief. Be honest: a map of all fives is useless. 5 means it
  matches the brief and has a reason to buy now. 3 means it matches on paper. 1 or 2 is worth
  recording only so nobody researches it again.
- `why`: one sentence of evidence you actually found. Never invent it.
- `signal`: a reason to reach out now, if there is one (funding, hiring, a launch, a new exec).
- `source`: the URL the evidence came from.
- `size` and `location` as found. Leave them empty when you could not find them.

Adding a company that is already on the map updates it, so it is safe to add again with better
evidence.

## 4. Show the map

`gtm_market(action="post")` in Slack puts the map in the channel with an **Add to pipeline**
button on each company. Elsewhere, `gtm_market(action="show")` and present the `map` text as is.

Then say which three you would start with and why, in two or three lines.

## 5. Into the pipeline

When the user picks companies (or taps the button), they become records at the first stage of
the playbook. Do it with `gtm_market(action="track", targets=[...])`; never re-create them with
`gtm_deal(action="add")`.

A message starting `[Stoa desk]` that says someone added a company from the map means the record
already exists. Your job is to find the buyer and draft the first note:

1. Decide which role buys this (from the market brief).
2. Find the person. With People Data Labs connected, `gtm_market(action="people", target=...,
   titles=[...], limit=3)`; each person returned costs a credit, so ask for few and never loop
   over a whole map. Otherwise use web search.
3. Put the person on the record with `gtm_deal(action="update", name=..., fields={"title": ...})`.
4. Reply with who you picked and why, and a first-touch note under 90 words that leads with the
   evidence from the map.

## Keeping a map alive

- `gtm_market(action="dismiss", targets=[...], reason=...)` for companies that are not a fit.
  They stay recorded so they do not come back.
- The *Market watch* automation re-checks a map every week and adds at most five new companies.
- One map per market. A second product or a second region gets its own map.

## What not to do

- Do not add companies you have no evidence for.
- Do not track a whole segment unprompted. The team chooses what enters the pipeline.
- Do not run paid people lookups across a map. One company at a time, when someone asks or taps.
