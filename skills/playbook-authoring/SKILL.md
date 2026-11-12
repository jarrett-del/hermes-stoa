---
name: playbook-authoring
description: How to write a Stoa playbook that fits the desk to a specific business - interview the user, draft the YAML, confirm, save. Load when someone describes their sales motion or asks to change stages.
version: 0.1.0
---

# Writing a playbook

A playbook is one short YAML file that fits the desk to a business: what a record is called, the
stages it moves through, what the buttons say, and what you draft when a stage changes. Three
ship with the plugin (`b2b-sales`, `recruiting`, `agency`). When none fits, write one.

## Steps

1. **Check what exists.** `gtm_playbook(action="list")`. If a built-in is close, offer it first.
2. **Interview, briefly.** Ask at most four questions, in one message:
   - What do you sell, and to whom?
   - What do you call one opportunity (a deal, a candidate, a project, a listing, a policy)?
     And what groups them (an account, a search, a client, a property)?
   - Walk me through the steps from first contact to money in the bank.
   - Why do you usually lose?
3. **Get the template.** `gtm_playbook(action="template")` returns the default YAML and the rules.
4. **Write the playbook.** Keep the structure, change the content:
   - `name`: lowercase with dashes, e.g. `dental-supply`.
   - `terms`: their words. This is what makes the desk feel like theirs.
   - `stages`: 4 to 7 is right for most small teams. Each stage is a state the buyer is in, not
     a task. `advance` is the button label, written as the action that moves it forward
     ("Send proposal", "Book site visit"). `stale_days` is how long a record can sit there
     before it counts as gone quiet: short for hot stages, longer for slow ones.
   - `won` and `lost`: their words ("Signed", "Placed", "Policy bound").
   - `lost.reasons`: the real reasons from the interview, five or six of them.
   - `fields`: three to six facts worth seeing on a card. Not everything; the activity log holds
     the rest.
   - `drafts.on_enter.<stage id>`: what you should write the moment a record enters that stage.
     Be specific about length, tone and what to include. Placeholders: `{name}` `{company}`
     `{group}` `{stage}` `{reason}` `{owner}` `{value}`.
   - `enrich`: `person` when they sell to individuals they need to research, `company` when the
     account matters more, `none` to hide the Enrich button.
   - `terms.group` and `map_group`: what records are grouped under, and where a company added from
     a market map goes. If each company they sell to is its own group (an account), say
     `map_group: company`. If a group is something they run and fill (a search, a campaign, a
     lead list for one of their clients), say `map_group: map`: one map feeds one group. Always
     say one of the two. Someone who works leads **for clients** needs `map_group: map` and a
     group that is not called "client": the client is set on each group.
   - `group_fields`: two to six facts kept on the group itself (a search's fee terms, what a
     client sells, who the campaign is aimed at).
   - `delivery`: up to six stages a won record moves through afterwards, when there is work after
     the win (Kickoff, Delivering, Month to month).
   - `win_probability` on each stage (0 to 100): how likely a record there is to be won. It
     feeds the forecast until their own history takes over.
   - In drafts, `{client}` is the client of the record's group.
5. **Show it before saving.** Give the user the stage list and button labels in plain words,
   not the YAML, and ask if it matches how they work.
6. **Save.** `gtm_playbook(action="save", yaml=..., activate=true)`. If it returns an error, fix
   the YAML and save again.
7. **Mind existing records.** If the save result has a `warning` about records in stages the new
   playbook lacks, list them and offer to move each to the nearest new stage.

## A good stage list

Bad: `New, Emailed, Called, Called again, Meeting, Proposal, Follow-up, Closed`. These are
activities, and a record can be in several at once.

Good: `Lead, Qualified, Discovery, Proposal, Negotiation`. Each is a state with a clear exit.

## Example: a home-services contractor

```yaml
name: remodel
title: Remodel desk
summary: Kitchen and bath remodels. One project per record, grouped by neighbourhood.
terms: {deal: project, deals: projects, group: neighbourhood, groups: neighbourhoods, company: Address, value: Job size}
stale_days: 6
enrich: none
stages:
  - {id: inquiry, label: Inquiry, stale_days: 1, advance: Book site visit}
  - {id: visit, label: Site visit, stale_days: 4, advance: Send estimate}
  - {id: estimate, label: Estimate sent, stale_days: 5, advance: Send contract}
  - {id: contract, label: Contract out, stale_days: 3, advance: Mark signed}
won: {id: signed, label: Signed}
lost:
  id: lost
  label: Lost
  reasons: [Price, Went with another contractor, Postponed, Out of our area]
fields:
  - {key: scope, label: Scope}
  - {key: timeline, label: Wants to start}
drafts:
  follow_up: Draft a friendly two-line text to {name} checking in on the project.
  on_enter:
    visit: Draft a text to {name} confirming the site visit and what to have ready.
    estimate: Draft the email that goes with the estimate for {name}. Restate the scope and the price, and offer a call.
  lost: Draft a short, gracious note to {name}. Reason recorded - {reason}.
```
