# Limits

- **If you run scheduled jobs against the desk, use 0.13.2 or later.** Earlier versions did not
  recognise a scheduled run as one (Hermes gives it no platform and marks the run another way), so
  the `cron` opt-in in `channels` and the autonomy setting did not apply to it: a job could change
  the desk without asking. Found by running a real scheduled job; fixed in 0.13.2.
- **The web dashboard tab is frozen.** It shows the pipeline, the market map, activity and setup as
  it did in 0.13. Approvals, cash, and searches or accounts are on the Stoa page in the Desktop app
  and the Webapp, and that is where new work goes.
- A search or account that is on hold is marked so and sorted last, but its records still show up
  as due or quiet. Snooze them if you want them out of the way.
- A person is kept once as a contact, but a record's own person is not: the same candidate in two
  searches is two records.
- A scheduled job cannot put someone on a search's or account's side by itself below the highest
  autonomy level, and there is no proposal for it to leave instead; it says so in its report.
- A change you declined is not proposed again for 90 days, even when something new comes in. Make it
  yourself, or clear what was declined under Setup.
- A proposal waits on the page or in a chat ("approve the next steps"). There is no approve button
  for one in Slack yet.
- Buttons are Slack-only. Other platforms get the text card, the text map and `/gtm`.
- In the Desktop app and Webapp the half ships switched off, like every plugin's: turn it on under
  **Capabilities → Plugins**. A click there needs an open chat to send its line into; with none
  open, the task waits on the record.
- If your agent already keeps people somewhere else, it may look there first. Naming the desk ("on the Stoa
  desk") or using `!gtm <name>` settles it.
- A map is as good as the research behind it. Fit scores are the agent's judgement against your
  brief; the source link is there so you can check.
- One Slack workspace per profile.
- The Google Sheet is a mirror, not the database: it updates when Hermes mirrors it, and edits made
  in it come back when you approve them. Deleting a row in the Sheet does not remove the record.
- After updating the plugin, restart the gateway so the Slack buttons bind to the new code.
- A team shares one desk. There are owners on records, but no per-person permissions.
- A card posted by a scheduled job, or before 0.9, has no one on record as having asked for it:
  list the people who may press such cards in `slack_users`, or ask for the card again.
- The activity sweep is only as good as the tools your Hermes has connected and the model reading
  the mail. It summarises; check the record's log against the email when it matters.
- Setup cannot tell which automations you have switched on, so it lists them all and says how.
  Today, approvals, undo, people and companies are in the Desktop app and Webapp page; the web
  dashboard tab has the pipeline, the map, the log and Setup.
- The autonomy setting governs scheduled jobs. In a chat the agent can still change something you
  did not ask for; the skill tells it to propose instead, and whatever it does can be taken back.
- Undo covers changes made since 0.12, when the ledger began. Changes to a market map, notes on
  a company, and a proposal you declined are not undone by it.
- A record's own person (its name and email) is not yet a shared contact: the same buyer on two
  deals is linked only where they were added as an extra person. The company view shows both.
- The ledger grows for as long as the desk is used. It is small, and nothing prunes it.
- A proposal is as good as the evidence the agent quotes. Read it before you approve.
- Stoa keeps the account of invoices; it does not create, send or collect them, and it has no link
  to an accounting system. One currency per desk (the playbook's). An invoice belongs to one
  record. "Collected this month" counts payments recorded since 0.13.
- The forecast is arithmetic, not a prediction: stage odds times amounts, by the dates you set.
- On a tour, what you do is thrown away when you leave, Slack cards posted from it stop working,
  lookups are off, and scheduled jobs do nothing until it ends.
- The dock on the page runs a turn of your agent's model, the same as typing in a chat. It takes
  CSV and small text files; a PDF, an image or a spreadsheet goes through the full chat, which is
  one click away. Dictation is not available in the Desktop app; the full chat's microphone is.
