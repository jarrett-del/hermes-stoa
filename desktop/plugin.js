/**
 * Stoa in the Hermes Desktop app and the Webapp.
 *
 * One file, no build step. It adds a Stoa page, a live pane that sits beside the conversation,
 * and three components the agent can draw inside a reply: a record card, a market map, and the
 * review of a list someone sent in.
 *
 * Everything here talks to the plugin's own backend (/api/plugins/stoa, the same routes the web
 * dashboard tab uses) through ctx.rest. A button saves at once, with no model involved; when the
 * click calls for writing, one line is sent into the open chat (host.composer.submit, the same as
 * typing it) and the agent writes the draft there.
 */
import { host, queryClient, useQuery } from '@hermes/plugin-sdk'
import { createElement as h, useState } from 'react'

let rest = null // ctx.rest, set in register()
const STATE = ['stoa', 'state']
const refresh = () => queryClient.invalidateQueries({ queryKey: ['stoa'] })

// -- small helpers -------------------------------------------------------------------------------
const SYMBOL = { USD: '$', EUR: '€', GBP: '£' }

function money(value, currency) {
  if (!value) return ''
  const sign = SYMBOL[currency] ?? ''
  const text = value >= 1e6 ? (value / 1e6).toFixed(1) + 'M' : value >= 1e4 ? Math.round(value / 1e3) + 'k' : Math.round(value).toLocaleString()
  return sign ? sign + text : text + ' ' + (currency || '')
}

function today() {
  const d = new Date()
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0')
}

function ago(stamp) {
  const then = Date.parse(stamp || '')
  if (!then) return ''
  const hours = (Date.now() - then) / 36e5
  if (hours < 1) return 'just now'
  if (hours < 24) return Math.floor(hours) + 'h ago'
  if (hours < 48) return 'yesterday'
  return Math.floor(hours / 24) + 'd ago'
}

function dueLabel(deal) {
  if (!deal.next_due) return ''
  if (deal.next_due < today()) return 'overdue'
  if (deal.next_due === today()) return 'today'
  const d = new Date(deal.next_due + 'T12:00:00')
  return isNaN(d) ? deal.next_due : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

const dueTone = deal => (deal.due ? (deal.next_due < today() ? 'bad' : 'warn') : '')
const cap = text => (text ? text.charAt(0).toUpperCase() + text.slice(1) : '')
const plural = (n, one, many) => n + ' ' + (n === 1 ? one : many)

function useDesk() {
  return useQuery({ queryKey: STATE, queryFn: () => rest('/state'), refetchInterval: 5000, staleTime: 1500 })
}

/** Send one line into the chat, as if typed: the conversation the card is in, else the one in focus. */
function toChat(text, sessionId) {
  const composer = host.composer
  let sent = false
  try {
    const focused = host.state.focusedSessionId ? host.state.focusedSessionId.get() : null
    for (const address of [sessionId, focused, null, 'new']) {
      if (sent || address === undefined || !composer) continue
      sent = composer.submit(address, text) === true
    }
  } catch {
    sent = false
  }
  if (!sent) host.notify({ kind: 'info', message: 'Saved. The draft is waiting on the record: open a chat and ask for it.' })
  return sent
}

async function act(dealId, body, sessionId) {
  try {
    const result = await rest('/deal/' + encodeURIComponent(dealId) + '/act', { method: 'POST', body })
    refresh()
    if (result && result.notice) host.notify({ kind: 'info', message: result.notice })
    if (result && result.ask) toChat(result.ask, sessionId)
    return result
  } catch (error) {
    host.notifyError(error, 'Stoa could not save that')
    return null
  }
}

async function track(target, sessionId) {
  try {
    const result = await rest('/target/' + encodeURIComponent(target.id) + '/track', { method: 'POST', body: {} })
    refresh()
    if (result && result.ask) toChat(result.ask, sessionId)
    else if (result && result.notice) host.notify({ kind: 'info', message: result.notice })
    else host.notify({ kind: 'success', message: target.company + ' is in the pipeline.' })
  } catch (error) {
    host.notifyError(error, 'Stoa could not add that company')
  }
}

// -- look: the app's own variables only, so every theme carries it -------------------------------
const CSS = `
.stoa { font-size: 13px; line-height: 1.45; color: var(--ui-text-primary); }
.stoa *, .stoa *::before { box-sizing: border-box; }
.stoa-pane { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.stoa-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 14px 12px 18px; }
.stoa-page { height: 100%; overflow: auto; padding: 30px 34px 56px; }
.stoa-wrap { max-width: 1320px; margin: 0 auto; }
.stoa-eyebrow { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 10px; letter-spacing: .14em; text-transform: uppercase; color: var(--ui-text-tertiary); }
.stoa-figure { font-size: 30px; font-weight: 600; letter-spacing: -.02em; line-height: 1.1; font-variant-numeric: tabular-nums; margin-top: 4px; }
.stoa-figure small { font-size: 12px; font-weight: 400; letter-spacing: 0; color: var(--ui-text-tertiary); margin-left: 7px; }
.stoa-page .stoa-figure { font-size: 44px; }
.stoa-rule { height: 2px; border-radius: 2px; margin: 12px 0 4px; opacity: .85; background: linear-gradient(90deg, var(--ui-blue), var(--ui-purple), var(--ui-red), var(--ui-yellow)); }
.stoa-sub { color: var(--ui-text-tertiary); font-size: 12px; }
.stoa-clip { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stoa-sec { margin-top: 16px; }
.stoa-sechead { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; padding: 0 8px 4px; }
.stoa-row { display: flex; gap: 8px; align-items: flex-start; width: 100%; padding: 6px 8px; border: 0; border-radius: 6px; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-row:hover, .stoa-row[data-open="1"] { background: var(--ui-row-hover-background); }
.stoa-dot { flex: none; width: 6px; height: 6px; margin-top: 6px; border-radius: 50%; background: var(--ui-text-quaternary); }
.stoa-dot[data-tone="bad"] { background: var(--ui-red); }
.stoa-dot[data-tone="warn"] { background: var(--ui-orange); }
.stoa-dot[data-tone="good"] { background: var(--ui-green); }
.stoa-grow { flex: 1; min-width: 0; }
.stoa-name { font-weight: 550; }
.stoa-num { flex: none; font-size: 12px; font-variant-numeric: tabular-nums; color: var(--ui-text-secondary); white-space: nowrap; }
.stoa-num[data-tone="bad"] { color: var(--ui-red); }
.stoa-num[data-tone="warn"] { color: var(--ui-orange); }
.stoa-box { padding: 12px 14px; border: 1px solid var(--ui-stroke-secondary); border-radius: 10px; background: var(--ui-bg-card); }
.stoa-detail { margin: 2px 0 10px; }
.stoa-inchat { max-width: 460px; margin: 6px 0; }
.stoa-meter { display: flex; gap: 3px; margin: 8px 0 4px; }
.stoa-meter i { flex: 1; height: 3px; border-radius: 2px; background: var(--ui-stroke-secondary); }
.stoa-meter i[data-on="1"] { background: var(--ui-text-primary); }
.stoa-facts { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 14px; margin: 14px 0 2px; padding-top: 12px; border-top: 1px solid var(--ui-stroke-tertiary); }
.stoa-facts span { display: block; overflow-wrap: anywhere; }
.stoa-next { margin-top: 10px; }
.stoa-log { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--ui-stroke-tertiary); }
.stoa-log p { margin: 0 0 5px; font-size: 12px; color: var(--ui-text-secondary); }
.stoa-btns { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
.stoa-btn, .stoa-select { height: 28px; padding: 0 10px; border: 1px solid var(--ui-stroke-primary); border-radius: 6px; background: transparent; color: var(--ui-text-primary); font: inherit; font-size: 12px; cursor: pointer; }
.stoa-btn:hover, .stoa-select:hover { background: var(--ui-control-hover-background); }
.stoa-btn[data-kind="primary"] { border-color: var(--ui-text-primary); background: var(--ui-text-primary); color: var(--ui-bg-editor); font-weight: 550; }
.stoa-btn[data-kind="quiet"] { border-color: transparent; color: var(--ui-text-secondary); }
.stoa-btn:disabled, .stoa-select:disabled { opacity: .5; cursor: default; }
.stoa-chip { display: inline-block; padding: 1px 6px; border: 1px solid var(--ui-stroke-secondary); border-radius: 4px; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 10px; letter-spacing: .08em; text-transform: uppercase; color: var(--ui-text-tertiary); white-space: nowrap; }
.stoa-chip[data-tone="bad"] { color: var(--ui-red); border-color: var(--ui-red); }
.stoa-chip[data-tone="warn"] { color: var(--ui-orange); border-color: var(--ui-orange); }
.stoa-chip[data-tone="good"] { color: var(--ui-green); border-color: var(--ui-green); }
.stoa-chips { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }
.stoa-foot { display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 12px; border-top: 1px solid var(--ui-stroke-tertiary); }
.stoa-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); margin: 22px 0 6px; border-top: 1px solid var(--ui-stroke-secondary); border-bottom: 1px solid var(--ui-stroke-secondary); }
.stoa-stats > div { padding: 12px 14px 12px 0; }
.stoa-stats b { display: block; font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; margin-top: 2px; }
.stoa-tabs { display: flex; gap: 4px; margin: 20px 0 16px; }
.stoa-tab { height: 30px; padding: 0 12px; border: 0; border-radius: 6px; background: transparent; color: var(--ui-text-secondary); font: inherit; cursor: pointer; }
.stoa-tab[data-on="1"] { background: var(--ui-row-active-background, var(--ui-row-hover-background)); color: var(--ui-text-primary); font-weight: 550; }
.stoa-split { display: grid; grid-template-columns: minmax(0, 1fr); gap: 20px; align-items: start; }
.stoa-split[data-side="1"] { grid-template-columns: minmax(0, 1fr) 360px; }
.stoa-flow { margin: -2px 2px 10px; }
.stoa-flow u { display: block; height: 3px; margin: 0 0 5px; border-radius: 2px; background: var(--ui-stroke-secondary); text-decoration: none; overflow: hidden; }
.stoa-flow u i { display: block; height: 100%; background: var(--ui-text-primary); }
.stoa-board { display: grid; grid-auto-flow: column; grid-auto-columns: minmax(176px, 1fr); gap: 12px; overflow-x: auto; padding-bottom: 8px; }
.stoa-colhead { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; padding: 0 2px 8px; }
.stoa-card { display: block; width: 100%; margin-bottom: 8px; padding: 10px 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: 8px; background: var(--ui-bg-card); color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-card:hover, .stoa-card[data-open="1"] { border-color: var(--ui-stroke-primary); }
.stoa-card h5 { display: flex; justify-content: space-between; gap: 8px; margin: 0; font-size: 13px; font-weight: 550; }
.stoa-empty { padding: 10px 2px; color: var(--ui-text-quaternary); }
.stoa-cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; }
.stoa-company { padding: 10px 0; border-top: 1px solid var(--ui-stroke-tertiary); }
.stoa-company:first-of-type { border-top: 0; }
.stoa-fit { letter-spacing: 1px; color: var(--ui-text-primary); white-space: nowrap; }
.stoa-fit s { text-decoration: none; color: var(--ui-text-quaternary); }
.stoa-rate { display: grid; grid-template-columns: minmax(0, 1fr) 90px 44px; gap: 10px; align-items: center; padding: 4px 0; }
.stoa-rate u { display: block; height: 4px; border-radius: 2px; background: var(--ui-stroke-secondary); overflow: hidden; }
.stoa-rate u i { display: block; height: 100%; background: var(--ui-text-primary); }
.stoa-reading { max-width: 70ch; margin: 8px 0 0; color: var(--ui-text-secondary); white-space: pre-line; }
.stoa-h2 { margin: 26px 0 8px; font-size: 15px; font-weight: 600; }
.stoa-list { max-height: 340px; overflow-y: auto; margin-top: 10px; }
.stoa-pick { display: flex; gap: 9px; align-items: flex-start; padding: 7px 0; border-top: 1px solid var(--ui-stroke-tertiary); cursor: pointer; }
.stoa-pick:first-child { border-top: 0; }
.stoa-pick input { flex: none; margin-top: 3px; accent-color: var(--ui-accent); }
.stoa-pick[data-off="1"] .stoa-grow { opacity: .5; }
.stoa-rest { margin-top: 10px; font-size: 12px; color: var(--ui-text-tertiary); }
.stoa-rest summary { cursor: pointer; }
.stoa-rest p { margin: 4px 0 0 14px; }
`

const Style = () => h('style', null, CSS)
const Chip = ({ tone, children }) => h('span', { className: 'stoa-chip', 'data-tone': tone || '' }, children)
const Btn = ({ kind, children, ...props }) => h('button', { type: 'button', className: 'stoa-btn', 'data-kind': kind || '', ...props }, children)

function Fit({ value }) {
  const n = Math.max(0, Math.min(5, Number(value) || 0))
  return h('span', { className: 'stoa-fit', title: 'Fit ' + n + ' of 5' }, '●'.repeat(n), h('s', null, '●'.repeat(5 - n)))
}

function Unavailable({ error, loading }) {
  return h('div', { className: 'stoa stoa-scroll' }, h(Style), h('div', { className: 'stoa-eyebrow' }, 'Stoa'),
    h('p', { className: 'stoa-sub', style: { marginTop: 8 } }, loading ? 'Opening the desk…'
      : 'The desk is not answering. Enable the stoa plugin for this profile (hermes plugins enable stoa), restart the gateway, and it appears here.'),
    !loading && error ? h('p', { className: 'stoa-sub' }, String(error.message || error)) : null)
}

// -- one record: what it is, what is next, and the buttons ---------------------------------------
function Record({ id, state, sessionId, close }) {
  const pb = state.playbook
  const query = useQuery({ queryKey: ['stoa', 'deal', id], queryFn: () => rest('/deal/' + encodeURIComponent(id)), staleTime: 1500 })
  const [busy, setBusy] = useState(false)
  const deal = query.data
  if (query.error) return h('div', { className: 'stoa-box stoa-sub' }, 'That record is no longer on the desk.')
  if (!deal) return h('div', { className: 'stoa-box stoa-sub' }, 'Loading…')
  const open = deal.status === 'open'
  const run = body => { setBusy(true); act(id, body, sessionId).then(() => setBusy(false)) }
  const facts = [[cap(pb.terms.group), deal.group], [pb.terms.value, money(deal.value, pb.currency)], [cap(pb.terms.owner), deal.owner]]
    .concat(pb.fields.map(field => [field.label, (deal.fields || {})[field.key]]))
    .concat([['Email', deal.email]]).filter(pair => pair[1])
  return h('div', { className: 'stoa-box' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'flex-start' } },
      h('div', { className: 'stoa-grow' },
        h('div', { className: 'stoa-name', style: { fontSize: 14 } }, deal.name),
        deal.company && deal.company !== deal.name ? h('div', { className: 'stoa-sub' }, deal.company) : null),
      close ? h(Btn, { kind: 'quiet', onClick: close, 'aria-label': 'Close' }, '×') : null),
    h('div', { className: 'stoa-meter' }, pb.stages.map((stage, index) =>
      h('i', { key: stage.id, 'data-on': deal.status === 'won' || (open && index <= deal.position) ? '1' : '0' }))),
    h('div', { className: 'stoa-sub', style: { display: 'flex', justifyContent: 'space-between' } },
      h('span', null, deal.stage_label + (deal.lost_reason ? ' · ' + deal.lost_reason : '')),
      h('span', null, open && deal.position >= 0 ? deal.position + 1 + ' of ' + pb.stages.length : ago(deal.stage_at))),
    deal.due || deal.stale || deal.pending_task ? h('div', { className: 'stoa-chips' },
      deal.due ? h(Chip, { tone: dueTone(deal) }, deal.next_due < today() ? 'overdue' : 'due today') : null,
      deal.stale && !deal.due ? h(Chip, null, 'quiet') : null,
      deal.pending_task ? h(Chip, { tone: 'good' }, 'draft waiting') : null) : null,
    deal.next_step ? h('div', { className: 'stoa-next' }, h('small', { className: 'stoa-eyebrow' }, 'Next'),
      h('div', null, deal.next_step, deal.next_due ? h('span', { className: 'stoa-sub' }, '  ' + dueLabel(deal)) : null)) : null,
    h('div', { className: 'stoa-btns' },
      open && deal.advance ? h(Btn, { kind: 'primary', disabled: busy, onClick: () => run({ verb: 'advance', extra: deal.stage }) }, deal.advance + '  →') : null,
      open ? h(Btn, { disabled: busy, onClick: () => run({ verb: 'draft' }) }, 'Draft follow-up') : null,
      open ? h(Btn, { disabled: busy, title: 'Hide from the quiet list for a week', onClick: () => run({ verb: 'snooze7' }) }, 'Snooze') : null,
      open ? h('select', { className: 'stoa-select', value: '', disabled: busy, 'aria-label': pb.lost.label,
        onChange: event => { if (event.target.value !== '') run({ verb: 'lost', extra: event.target.value }) } },
        [h('option', { key: 'none', value: '' }, pb.lost.label + '…')].concat(pb.lost.reasons.map((reason, index) =>
          h('option', { key: index, value: String(index) }, reason)))) : null,
      !open ? h(Btn, { kind: 'primary', disabled: busy, onClick: () => run({ verb: 'reopen' }) }, 'Reopen') : null),
    facts.length ? h('div', { className: 'stoa-facts' }, facts.map(pair =>
      h('div', { key: pair[0] }, h('small', { className: 'stoa-eyebrow' }, pair[0]), h('span', null, String(pair[1]))))) : null,
    (deal.activity || []).length ? h('div', { className: 'stoa-log' }, deal.activity.slice(0, 3).map((entry, index) =>
      h('p', { key: index }, entry.text, h('span', { className: 'stoa-sub' }, '  ' + ago(entry.at) + (entry.by ? ' · ' + entry.by : ''))))) : null)
}

// -- a list sent in: tick what to keep, then Apply ------------------------------------------------
function changeText(change) {
  if (change.field === 'Note') return 'note: ' + change.to
  return change.from ? change.field + ': ' + change.from + ' → ' + change.to : change.field + ': ' + change.to
}

function Review({ id }) {
  const query = useQuery({ queryKey: ['stoa', 'intake', id], queryFn: () => rest('/intake/' + encodeURIComponent(id)), staleTime: 1500 })
  const [off, setOff] = useState({})
  const [busy, setBusy] = useState(false)
  const intake = query.data
  if (query.error) return h('div', { className: 'stoa-box stoa-sub' }, 'That list is no longer here.')
  if (!intake) return h('div', { className: 'stoa-box stoa-sub' }, 'Loading…')
  const open = intake.status === 'open'
  const usable = intake.rows.filter(row => row.kind === 'new' || row.kind === 'update')
  const rest_ = intake.rows.filter(row => row.kind !== 'new' && row.kind !== 'update')
  const chosen = usable.filter(row => !off[row.n])
  const counts = intake.counts, result = intake.result || {}
  const send = verb => {
    setBusy(true)
    rest('/intake/' + encodeURIComponent(id) + '/' + verb, { method: 'POST', body: verb === 'apply' ? { rows: chosen.map(row => row.n) } : {} })
      .then(done => {
        refresh()
        if (verb === 'apply' && done.applied) host.notify({ kind: 'success', message: done.added + ' added, ' + done.updated + ' updated' })
        else if (done.note) host.notify({ kind: 'info', message: done.note })
        if (done.problems && done.problems.length) host.notify({ kind: 'warning', message: done.problems[0] })
      })
      .catch(error => host.notifyError(error, 'Stoa could not apply that list'))
      .then(() => setBusy(false))
  }
  return h('div', { className: 'stoa-box' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'baseline' } },
      h('span', { className: 'stoa-eyebrow' }, 'Intake · ' + intake.source),
      open ? null : h(Chip, { tone: intake.status === 'applied' ? 'good' : '' }, intake.status)),
    h('div', { className: 'stoa-name', style: { marginTop: 4 } },
      [counts.new ? counts.new + ' new' : '', counts.update ? counts.update + ' to update' : '', counts.same ? counts.same + ' already there' : '',
        counts.unclear ? counts.unclear + ' unclear' : '', counts.problem ? counts.problem + ' unusable' : ''].filter(Boolean).join(' · ') || 'Nothing to add'),
    usable.length ? h('div', { className: 'stoa-list' }, usable.map(row => h('label', { key: row.n, className: 'stoa-pick', 'data-off': open && off[row.n] ? '1' : '0' },
      open ? h('input', { type: 'checkbox', checked: !off[row.n], disabled: busy, onChange: () => setOff({ ...off, [row.n]: !off[row.n] }) })
        : h('i', { className: 'stoa-dot', 'data-tone': row.applied ? 'good' : '' }),
      h('span', { className: 'stoa-grow' },
        h('span', { className: 'stoa-name' }, row.name),
        h('span', { className: 'stoa-sub' }, '  ' + [row.company !== row.name ? row.company : '', row.group].filter(Boolean).join(' · ')),
        h('span', { className: 'stoa-sub', style: { display: 'block' } },
          row.kind === 'new' ? 'New' + (row.stage ? ' at ' + row.stage : '') : row.changes.map(changeText).join(' · ')),
        (row.warnings || []).map((warning, index) => h('span', { key: index, className: 'stoa-sub', style: { display: 'block' } }, warning))),
      h(Chip, { tone: row.kind === 'new' ? 'good' : '' }, row.kind === 'new' ? 'new' : 'update')))) : null,
    rest_.length ? h('details', { className: 'stoa-rest' },
      h('summary', null, plural(rest_.length, 'row', 'rows') + ' left out'),
      rest_.map(row => h('p', { key: row.n }, (row.name || 'Row ' + row.n) + ': ' + row.why))) : null,
    open ? h('div', { className: 'stoa-btns' },
      h(Btn, { kind: 'primary', disabled: busy || !chosen.length, onClick: () => send('apply') }, 'Apply ' + chosen.length),
      h(Btn, { kind: 'quiet', disabled: busy, onClick: () => send('discard') }, 'Discard'),
      h('span', { className: 'stoa-sub', style: { alignSelf: 'center' } }, 'Nothing is written until you apply.'))
      : intake.status === 'applied' ? h('div', { className: 'stoa-sub', style: { marginTop: 10 } },
        (result.added || 0) + ' added · ' + (result.updated || 0) + ' updated' + (result.left ? ' · ' + result.left + ' left out' : '')) : null)
}

// -- the pane beside the conversation ------------------------------------------------------------
function Row({ deal, sub, right, tone, open, onClick }) {
  return h('button', { type: 'button', className: 'stoa-row', 'data-open': open ? '1' : '0', onClick },
    h('i', { className: 'stoa-dot', 'data-tone': tone || '' }),
    h('span', { className: 'stoa-grow' },
      h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, deal.name),
      sub ? h('span', { className: 'stoa-sub stoa-clip', style: { display: 'block' } }, sub) : null),
    right ? h('span', { className: 'stoa-num', 'data-tone': tone || '' }, right) : null)
}

function Pane() {
  const query = useDesk()
  const [openId, setOpenId] = useState('')
  if (!query.data) return h(Unavailable, { error: query.error, loading: query.isLoading })
  const state = query.data, pb = state.playbook, totals = state.totals
  const open = state.deals.filter(deal => deal.status === 'open')
  const due = open.filter(deal => deal.due).sort((a, b) => (a.next_due < b.next_due ? -1 : 1))
  const quiet = open.filter(deal => deal.stale && !deal.due)
  const toggle = id => setOpenId(openId === id ? '' : id)
  const listed = (deal, key, sub, right, tone) => [
    h(Row, { key, deal, sub, right, tone, open: openId === key, onClick: () => toggle(key) }),
    openId === key ? h('div', { key: key + ':detail', className: 'stoa-detail' }, h(Record, { id: deal.id, state })) : null]
  const calls = (state.markets[0] ? state.markets[0].first_calls : []).filter(call => call.status === 'new').slice(0, 3)
  return h('div', { className: 'stoa stoa-pane' }, h(Style),
    h('div', { className: 'stoa-scroll' },
      h('div', { style: { padding: '0 8px' } },
        h('div', { className: 'stoa-eyebrow' }, 'Stoa · ' + pb.title),
        h('div', { className: 'stoa-figure' }, money(totals.open_value, pb.currency) || String(totals.open), h('small', null, totals.open_value ? 'in play' : pb.terms.deals + ' open')),
        h('div', { className: 'stoa-sub' }, [plural(totals.open, 'open', 'open'), totals.due ? totals.due + ' due' : '', totals.stale ? totals.stale + ' quiet' : '']
          .filter(Boolean).join(' · ')),
        h('div', { className: 'stoa-rule' })),
      (state.intakes || []).length ? h('div', { className: 'stoa-sec' },
        h('div', { className: 'stoa-sechead' }, h('span', { className: 'stoa-eyebrow' }, 'Waiting for your review'), h('span', { className: 'stoa-num' }, state.intakes.length)),
        state.intakes.map(item => h('div', { key: item.id, style: { marginBottom: 8 } }, h(Review, { id: item.id })))) : null,
      !state.deals.length ? h('p', { className: 'stoa-sub', style: { padding: '8px' } },
        'Nothing on the desk yet. Tell Hermes about a ' + pb.terms.deal + ', send it a list, or ask it to map a market.') : null,
      due.length || quiet.length ? h('div', { className: 'stoa-sec' },
        h('div', { className: 'stoa-sechead' }, h('span', { className: 'stoa-eyebrow' }, 'Needs you'), h('span', { className: 'stoa-num' }, due.length + quiet.length)),
        due.map(deal => listed(deal, 'due:' + deal.id, deal.next_step || deal.stage_label, dueLabel(deal), dueTone(deal))),
        quiet.map(deal => listed(deal, 'quiet:' + deal.id, deal.stage_label, 'quiet ' + ago(deal.last_touch || deal.updated_at).replace(' ago', '')))) : null,
      pb.stages.map(stage => {
        const here = open.filter(deal => deal.stage === stage.id)
        if (!here.length) return null
        const value = here.reduce((sum, deal) => sum + (deal.value || 0), 0)
        return h('div', { className: 'stoa-sec', key: stage.id },
          h('div', { className: 'stoa-sechead' }, h('span', { className: 'stoa-eyebrow' }, stage.label),
            h('span', { className: 'stoa-num' }, here.length + (value ? ' · ' + money(value, pb.currency) : ''))),
          here.map(deal => listed(deal, stage.id + ':' + deal.id, [deal.company, deal.next_step].filter(Boolean).join(' · '), money(deal.value, pb.currency))))
      }),
      calls.length ? h('div', { className: 'stoa-sec' },
        h('div', { className: 'stoa-sechead' }, h('span', { className: 'stoa-eyebrow' }, 'First calls'), h('span', { className: 'stoa-num' }, state.markets[0].name)),
        calls.map(call => h('div', { key: call.id, className: 'stoa-row', style: { cursor: 'default' } },
          h('span', { className: 'stoa-grow' },
            h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, call.company),
            h('span', { className: 'stoa-sub', style: { display: 'block' } }, call.why)),
          h(Btn, { onClick: () => track(call) }, 'Add')))) : null),
    h('div', { className: 'stoa-foot' },
      h(Btn, { onClick: () => host.navigate('/stoa') }, 'Open the desk'),
      h(Btn, { kind: 'quiet', onClick: () => toChat("What's on my plate today on the Stoa desk?") }, "What's on my plate?")))
}

// -- the full page -------------------------------------------------------------------------------
function Board({ state, openId, setOpenId }) {
  const pb = state.playbook
  const open = state.deals.filter(deal => deal.status === 'open')
  const won = state.deals.filter(deal => deal.status === 'won').sort((a, b) => (a.stage_at < b.stage_at ? 1 : -1))
  const known = new Set(pb.stages.map(stage => stage.id))
  const strays = open.filter(deal => !known.has(deal.stage))
  const flow = {}
  state.flow.stages.forEach(stage => { flow[stage.id] = stage })
  const total = Math.max(1, state.flow.total)
  const columns = pb.stages.map(stage => ({ ...stage, here: open.filter(deal => deal.stage === stage.id), flow: flow[stage.id] }))
    .concat(strays.length ? [{ id: '~other', label: 'Other stages', other: true, here: strays }] : [])
    .concat([{ id: '~won', label: pb.won.label, won: true, here: won.slice(0, 8), flow: { reached: state.flow.won, lost: 0 } }])
  return h('div', { className: 'stoa-board' }, columns.map(column => {
    const value = column.here.reduce((sum, deal) => sum + (deal.value || 0), 0)
    const reached = column.flow ? column.flow.reached : column.here.length
    return h('div', { key: column.id },
      h('div', { className: 'stoa-colhead' }, h('span', { className: 'stoa-eyebrow stoa-clip' }, column.label),
        h('span', { className: 'stoa-num' }, column.here.length + (value ? ' · ' + money(value, pb.currency) : ''))),
      h('div', { className: 'stoa-flow' },
        h('u', null, h('i', { style: { width: Math.round((100 * reached) / total) + '%' } })),
        h('div', { className: 'stoa-sub' }, column.won ? reached + ' won' : column.other ? 'from another playbook'
          : reached + ' reached' + (column.flow && column.flow.lost ? ' · ' + column.flow.lost + ' lost' : ''))),
      column.here.length ? column.here.map(deal => h('button', { type: 'button', key: deal.id, className: 'stoa-card', 'data-open': openId === deal.id ? '1' : '0',
        onClick: () => setOpenId(openId === deal.id ? '' : deal.id) },
        h('h5', null, h('span', { className: 'stoa-clip' }, deal.name), deal.value ? h('span', { className: 'stoa-num' }, money(deal.value, pb.currency)) : null),
        h('div', { className: 'stoa-sub stoa-clip' }, [deal.company, deal.group && deal.group !== deal.company ? deal.group : ''].filter(Boolean).join(' · ') || ' '),
        deal.next_step && !column.won ? h('div', { className: 'stoa-clip', style: { marginTop: 6, fontSize: 12 } }, deal.next_step) : null,
        deal.due || deal.stale || deal.pending || column.other ? h('div', { className: 'stoa-chips' },
          column.other ? h(Chip, null, deal.stage_label) : null,
          deal.due ? h(Chip, { tone: dueTone(deal) }, deal.next_due < today() ? 'overdue' : 'due today') : null,
          deal.stale && !deal.due ? h(Chip, null, 'quiet') : null,
          deal.pending ? h(Chip, { tone: 'good' }, 'draft waiting') : null) : null))
        : h('div', { className: 'stoa-empty' }, '—'))
  }))
}

function Company({ company, sessionId }) {
  const [busy, setBusy] = useState(false)
  const buyer = (company.people || [])[0]
  return h('div', { className: 'stoa-company' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'baseline' } },
      h('span', { className: 'stoa-name stoa-clip' }, company.company), h(Fit, { value: company.fit })),
    h('div', { className: 'stoa-sub' }, [company.size, company.location, company.uses ? 'uses ' + company.uses : ''].filter(Boolean).join(' · ')),
    company.why ? h('div', { style: { marginTop: 4 } }, company.why) : null,
    buyer ? h('div', { className: 'stoa-sub', style: { marginTop: 4 } }, buyer.name + (buyer.title ? ', ' + buyer.title : '')) : null,
    h('div', { className: 'stoa-chips', style: { alignItems: 'center' } },
      company.status === 'tracked' ? h(Chip, { tone: 'good' }, 'in the pipeline')
        : company.status === 'dismissed' ? h(Chip, null, 'dismissed')
          : h(Btn, { disabled: busy, onClick: () => { setBusy(true); track(company, sessionId).then(() => setBusy(false)) } }, 'Add to pipeline'),
      company.signal ? h(Chip, null, company.signal) : null))
}

function Market({ market, sessionId, compact }) {
  const numbers = market.numbers
  return h('div', null,
    h('div', { className: 'stoa-name', style: { fontSize: compact ? 14 : 16 } }, market.name),
    h('div', { className: 'stoa-sub' }, [plural(numbers.companies, 'company', 'companies'), numbers.in_pipeline + ' in the pipeline',
      numbers.with_signal ? numbers.with_signal + ' with a signal' : '', numbers.buyers_named ? plural(numbers.buyers_named, 'buyer named', 'buyers named') : '']
      .filter(Boolean).join(' · ')),
    market.reading && !compact ? h('p', { className: 'stoa-reading' }, market.reading) : null,
    (market.first_calls || []).length ? h('div', null, h('div', { className: 'stoa-h2' }, 'First calls'),
      market.first_calls.slice(0, compact ? 3 : 7).map(call => h('div', { key: call.id, className: 'stoa-row', style: { cursor: 'default', padding: '6px 0' } },
        h(Fit, { value: call.fit }),
        h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name' }, call.company),
          h('span', { className: 'stoa-sub' }, '  ' + [call.segment, call.buyer].filter(Boolean).join(' · ')),
          h('span', { style: { display: 'block' } }, call.why)),
        call.status === 'tracked' ? h(Chip, { tone: 'good' }, 'in the pipeline') : h(Btn, { onClick: () => track(call, sessionId) }, 'Add')))) : null,
    h('div', { className: 'stoa-h2' }, 'The map'),
    h('div', { className: 'stoa-cols' }, market.segments.map(segment => h('div', { key: segment.name, className: 'stoa-box' },
      h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'baseline' } },
        h('span', { className: 'stoa-eyebrow' }, segment.name), h('span', { className: 'stoa-num' }, segment.count + ' · fit ' + segment.avg_fit)),
      segment.description && !compact ? h('div', { className: 'stoa-sub', style: { marginTop: 2 } }, segment.description) : null,
      segment.companies.slice(0, compact ? 4 : 30).map(company => h(Company, { key: company.id, company, sessionId }))))),
    compact ? null : h('div', { className: 'stoa-cols', style: { marginTop: 6 } },
      (market.rates || []).length ? h('div', null, h('div', { className: 'stoa-h2' }, 'How rare each requirement is'),
        market.rates.map(rate => h('div', { key: rate.label, className: 'stoa-rate' },
          h('span', { className: 'stoa-clip' }, rate.label), h('u', null, h('i', { style: { width: rate.rate + '%' } })),
          h('span', { className: 'stoa-num' }, rate.rate + '%')))) : null,
      (market.holds || []).length ? h('div', null, h('div', { className: 'stoa-h2' }, 'Who holds this market today'),
        market.holds.map(hold => h('div', { key: hold.name, className: 'stoa-rate' },
          h('span', { className: 'stoa-clip' }, hold.name), h('u', null, h('i', { style: { width: hold.share + '%' } })),
          h('span', { className: 'stoa-num' }, hold.share + '%')))) : null))
}

function Page() {
  const query = useDesk()
  const [tab, setTab] = useState('pipeline')
  const [openId, setOpenId] = useState('')
  const [marketId, setMarketId] = useState('')
  if (!query.data) return h('div', { className: 'stoa stoa-page' }, h(Unavailable, { error: query.error, loading: query.isLoading }))
  const state = query.data, pb = state.playbook, totals = state.totals
  const market = state.markets.find(item => item.id === marketId) || state.markets[0]
  const stats = [['Open', totals.open], ['Due', totals.due], ['Quiet', totals.stale], ['Mapped', totals.mapped], ['From the map', totals.tracked],
    ['Won', totals.won + (totals.won_value ? ' · ' + money(totals.won_value, pb.currency) : '')]]
  return h('div', { className: 'stoa stoa-page' }, h(Style), h('div', { className: 'stoa-wrap' },
    h('div', { className: 'stoa-eyebrow' }, 'Stoa · ' + pb.title),
    h('div', { className: 'stoa-figure' }, money(totals.open_value, pb.currency) || String(totals.open), h('small', null, totals.open_value ? 'in play' : pb.terms.deals + ' open')),
    h('div', { className: 'stoa-rule' }),
    h('div', { className: 'stoa-stats' }, stats.map(pair => h('div', { key: pair[0] }, h('span', { className: 'stoa-eyebrow' }, pair[0]), h('b', null, String(pair[1]))))),
    (state.intakes || []).length ? h('div', { style: { marginTop: 18, maxWidth: 720 } },
      h('div', { className: 'stoa-eyebrow', style: { marginBottom: 6 } }, 'Waiting for your review'),
      state.intakes.map(item => h('div', { key: item.id, style: { marginBottom: 10 } }, h(Review, { id: item.id })))) : null,
    h('div', { className: 'stoa-tabs' }, [['pipeline', 'Pipeline'], ['market', 'Market map'], ['activity', 'Activity']].map(pair =>
      h('button', { type: 'button', key: pair[0], className: 'stoa-tab', 'data-on': tab === pair[0] ? '1' : '0', onClick: () => setTab(pair[0]) }, pair[1]))),
    tab === 'pipeline' ? (state.deals.length
      ? h('div', { className: 'stoa-split', 'data-side': openId ? '1' : '0' },
        h(Board, { state, openId, setOpenId }),
        openId ? h(Record, { key: openId, id: openId, state, close: () => setOpenId('') }) : null)
      : h('p', { className: 'stoa-sub' }, 'Nothing on the desk yet. Tell Hermes about a ' + pb.terms.deal + ', or ask it to map a market.')) : null,
    tab === 'market' ? (market
      ? h('div', null,
        state.markets.length > 1 ? h('div', { className: 'stoa-tabs', style: { marginTop: 0 } }, state.markets.map(item =>
          h('button', { type: 'button', key: item.id, className: 'stoa-tab', 'data-on': item.id === market.id ? '1' : '0', onClick: () => setMarketId(item.id) }, item.name))) : null,
        h(Market, { market }))
      : h('p', { className: 'stoa-sub' }, 'No market map yet. Ask Hermes: “Map the market for us.”')) : null,
    tab === 'activity' ? h('div', { style: { maxWidth: 760 } }, state.activity.map(entry => h('div', { key: entry.id, className: 'stoa-row', style: { cursor: 'default' } },
      h('i', { className: 'stoa-dot', 'data-tone': entry.kind === 'stage' ? 'good' : '' }),
      h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name' }, entry.name), h('span', null, '  ' + entry.text),
        h('span', { className: 'stoa-sub', style: { display: 'block' } }, ago(entry.at) + ' · ' + entry.kind + (entry.by ? ' · ' + entry.by : ''))))) ) : null))
}

// -- drawn inside a reply: ::stoa-card{id="…"} and ::stoa-map{market="…"} ------------------------
// Attributes are model output: checked, and nothing is drawn when they do not name something real.
function CardInChat({ attrs, sessionId }) {
  const query = useDesk()
  const id = String((attrs && attrs.id) || '').trim()
  if (!id || id.length > 80 || /[\s/\\]/.test(id) || !query.data) return null
  return h('div', { className: 'stoa stoa-inchat' }, h(Style), h(Record, { id, state: query.data, sessionId: sessionId ?? null }))
}

function MapInChat({ attrs, sessionId }) {
  const query = useDesk()
  const want = String((attrs && attrs.market) || '').trim().toLowerCase()
  if (!query.data || want.length > 120) return null
  const markets = query.data.markets
  const market = markets.find(item => item.id === want || item.name.toLowerCase() === want) || (want ? null : markets[0])
  if (!market) return null
  return h('div', { className: 'stoa stoa-box', style: { margin: '6px 0', maxWidth: 760 } }, h(Style),
    h(Market, { market, sessionId: sessionId ?? null, compact: true }))
}

function ReviewInChat({ attrs }) {
  const id = String((attrs && attrs.id) || '').trim()
  if (!/^in-[0-9a-f]{8}$/.test(id)) return null
  return h('div', { className: 'stoa', style: { margin: '6px 0', maxWidth: 620 } }, h(Style), h(Review, { id }))
}

export default {
  id: 'stoa',
  name: 'Stoa',
  register(ctx) {
    rest = ctx.rest
    // While the app is open the desk knows it can draw cards and maps in the conversation here.
    const hello = () => { ctx.rest('/hello', { method: 'POST', body: {} }).catch(() => {}) }
    hello()
    ctx.setInterval(hello, 30000)
    // The agent, a Slack click or another window changed the desk: redraw now, not at the next poll.
    ctx.onEvent('plugin.stoa.changed', refresh)
    ctx.registerMany([
      { id: 'pane', area: 'panes', title: 'Stoa', data: { placement: 'right', width: '340px' }, render: () => h(Pane) },
      { id: 'page', area: 'routes', data: { path: '/stoa' }, render: () => h(Page) },
      { id: 'nav', area: 'sidebar.nav', data: { path: '/stoa', label: 'Stoa', codicon: 'graph' } },
      { id: 'card', area: 'transcript.directives', data: { name: 'stoa-card', render: props => h(CardInChat, props) } },
      { id: 'map', area: 'transcript.directives', data: { name: 'stoa-map', render: props => h(MapInChat, props) } },
      { id: 'intake', area: 'transcript.directives', data: { name: 'stoa-intake', render: props => h(ReviewInChat, props) } },
      { id: 'open', area: 'palette', data: { id: 'stoa.open', label: 'Stoa: open the desk', keywords: ['pipeline', 'gtm', 'market map'], run: () => host.navigate('/stoa') } }
    ])
  }
}
