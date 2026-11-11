/**
 * Stoa in the Hermes Desktop app and the Webapp.
 *
 * One file, no build step. It adds a Stoa page, a live pane that sits beside the conversation,
 * and three components the agent can draw inside a reply: a record card, a market map, and the
 * review of a list someone sent in.
 *
 * Everything here talks to the plugin's own backend (/api/plugins/stoa, the same routes the web
 * dashboard tab uses) through ctx.rest. A button or a form saves at once, with no model involved;
 * when a click calls for writing, one line is sent into the open chat (host.composer.submit, the
 * same as typing it) and the agent writes the draft there. The page, which has no chat beside it,
 * keeps a conversation of its own for its talk box (see "talking to the desk" below).
 */
import { atom, host, queryClient, useQuery, useValue } from '@hermes/plugin-sdk'
import { createElement as h, useEffect, useRef, useState } from 'react'

let rest = null // ctx.rest, set in register()
let storage = null // ctx.storage, set in register()

// What the pane and the page are narrowed to. One store, so both show the same thing.
const NO_FILTER = { text: '', owner: '', group: '', stage: '', due: false, quiet: false }
const $filter = atom({ ...NO_FILTER })
function setFilter(patch) {
  const next = { ...$filter.get(), ...patch }
  $filter.set(next)
  if (storage) storage.set('filter', next)
}
const filtering = f => Boolean(f.text || f.owner || f.group || f.stage || f.due || f.quiet)
function matches(deal, f) {
  if (f.owner && deal.owner !== f.owner) return false
  if (f.group && deal.group !== f.group) return false
  if (f.stage && deal.stage !== f.stage) return false
  if ((f.due || f.quiet) && !((f.due && deal.due) || (f.quiet && deal.stale && !deal.due))) return false
  if (!f.text) return true
  return [deal.name, deal.company, deal.group, deal.next_step, deal.owner, deal.stage_label].join(' ').toLowerCase().includes(f.text.toLowerCase())
}
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

// -- talking to the desk from its own page -------------------------------------------------------
// The page has no conversation beside it, so it keeps one of its own: an ordinary session named
// "Stoa desk", made and resumed through the gateway the way the app makes any session, and listed
// in the sidebar like any other. What is typed (or dictated) into the dock at the foot of the page
// is submitted to it, and the reply is read off the gateway's event stream (see register) and
// shown above the dock.
const $talk = atom({ busy: false, open: false, turns: [] })
const $attached = atom(null) // a small text file waiting to go with the next message: { name, text }
const talkSession = { runtime: '', stored: '', primed: false, turn: 0 }
const setTalk = patch => $talk.set({ ...$talk.get(), ...patch })
const PRIME = 'This conversation is the talk box on the Stoa desk page. Act on the Stoa desk with the gtm tools, and answer in a few '
  + 'short lines: the page already shows the pipeline.\n\n'

/** Change the turn being answered, which is always the last one. */
function setTurn(patch) {
  const talk = $talk.get()
  if (!talk.turns.length) return
  const turns = talk.turns.slice()
  const last = turns[turns.length - 1]
  turns[turns.length - 1] = { ...last, ...(typeof patch === 'function' ? patch(last) : patch) }
  $talk.set({ ...talk, turns })
}

async function talkOpen() {
  if (talkSession.runtime) return talkSession.runtime
  const stored = talkSession.stored || ((storage && storage.get('talk', {})) || {}).stored || ''
  if (stored) {
    try {
      const resumed = await host.request('session.resume', { session_id: stored, omit_messages: true })
      if (resumed && resumed.session_id) {
        Object.assign(talkSession, { runtime: resumed.session_id, stored, primed: true })
        return talkSession.runtime
      }
    } catch { /* that conversation is gone: start another */ }
  }
  const made = await host.request('session.create', { title: 'Stoa desk' })
  const runtime = (made && made.session_id) || ''
  if (!runtime) throw new Error('Hermes did not open a conversation')
  Object.assign(talkSession, { runtime, stored: made.stored_session_id || runtime, primed: false })
  if (storage) storage.set('talk', { stored: talkSession.stored })
  return runtime
}

async function say(text, file) {
  const line = String(text || '').trim()
  if ((!line && !file) || $talk.get().busy) return
  const turn = ++talkSession.turn
  const shown = line || 'Here is ' + file.name + '.'
  const sent = file ? shown + '\n\nAttached file "' + file.name + '":\n```\n' + file.text + '\n```' : shown
  $talk.set({ busy: true, open: true,
    turns: $talk.get().turns.slice(-7).concat([{ you: shown, file: file ? file.name : '', reply: '', note: 'Sending…', error: '' }]) })
  const submit = async () => {
    const id = await talkOpen()
    await host.request('prompt.submit', { session_id: id, text: talkSession.primed ? sent : PRIME + sent })
    talkSession.primed = true
  }
  try {
    try {
      await submit()
    } catch (first) {
      if (!talkSession.runtime) throw first
      talkSession.runtime = '' // the gateway let the idle session go: resume it (or start another) and send once more
      await submit()
    }
    if (talkSession.turn === turn && $talk.get().busy) setTurn(last => (last.reply ? {} : { note: 'Hermes is working on it…' }))
    setTimeout(() => {
      if (talkSession.turn !== turn || !$talk.get().busy) return
      setTalk({ busy: false })
      setTurn({ note: 'Still working. Open the conversation to follow it; the desk updates when it is done.' })
    }, 180000)
  } catch (error) {
    setTalk({ busy: false })
    setTurn({ note: '', error: 'Hermes could not take that: ' + String((error && error.message) || error) })
  }
}

function openTalk() {
  if (talkSession.stored) Promise.resolve(host.openSession(talkSession.stored)).catch(() => {})
  else host.notify({ kind: 'info', message: 'Say something to the desk first; the conversation starts then.' })
}

// A file handed to the desk. A CSV is read by the desk itself and comes back as a review, with no
// model in between. A small text file rides along with the next message. Anything else (a PDF, an
// image, a spreadsheet) needs the full chat, which can read it.
const LIST_FILE = /\.(csv|tsv)$/i
const TEXT_FILE = /\.(txt|md|markdown|json|vcf|log|ya?ml)$/i

async function takeFile(file) {
  if (!file) return
  try {
    if (LIST_FILE.test(file.name)) {
      if (file.size > 2e6) return host.notify({ kind: 'warning', message: 'That list is over 2 MB. Send it in parts.' })
      const result = await rest('/intake', { method: 'POST', body: { name: file.name, text: await file.text() } })
      refresh()
      host.notify({ kind: result.waiting ? 'success' : 'info', message: result.headline })
      const page = document.querySelector('.stoa-page')
      if (result.waiting && page) page.scrollTo({ top: 0, behavior: 'smooth' })
      return
    }
    if ((TEXT_FILE.test(file.name) || String(file.type || '').startsWith('text/')) && file.size <= 60000) {
      $attached.set({ name: file.name, text: await file.text() })
      return
    }
    host.notify({ kind: 'info', message: 'A PDF, an image or a spreadsheet goes through the full chat: press "Open the conversation" and drop it there.' })
  } catch (error) {
    host.notifyError(error, 'Stoa could not read that file')
  }
}

// -- look: the app's own variables only, so every theme carries it -------------------------------
const CSS = `
/* Colour: all of it comes from the app's theme. One accent, the app's own (--ui-accent), used the way the
   app uses it: filled for the main action, tinted with a hairline for "you are here". */
.stoa { font-size: 13px; line-height: 1.45; color: var(--ui-text-primary);
  --stoa-accent: var(--ui-accent);
  --stoa-on-accent: var(--dt-midground-foreground, #fff);
  --stoa-accent-text: color-mix(in oklab, var(--ui-accent) 58%, var(--ui-text-primary));
  --stoa-accent-soft: color-mix(in srgb, var(--ui-accent) 16%, transparent);
  --stoa-accent-line: color-mix(in srgb, var(--ui-accent) 58%, transparent); }
.stoa *, .stoa *::before { box-sizing: border-box; }
.stoa-pane { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.stoa-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 14px 12px 18px; }
.stoa-page { height: 100%; overflow: auto; padding: 28px 34px 0; }
.stoa-wrap { max-width: 1320px; margin: 0 auto; }
.stoa-eyebrow { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 10px; letter-spacing: .14em; text-transform: uppercase; color: var(--ui-text-tertiary); }
.stoa-figure { font-size: 30px; font-weight: 600; letter-spacing: -.02em; line-height: 1.1; font-variant-numeric: tabular-nums; margin-top: 4px; }
.stoa-figure small { font-size: 12px; font-weight: 400; letter-spacing: 0; color: var(--ui-text-tertiary); margin-left: 7px; }
.stoa-page .stoa-figure { font-size: 46px; }
.stoa-rule { height: 2px; border-radius: 2px; margin: 12px 0 4px; background: linear-gradient(90deg, var(--ui-text-primary), var(--ui-text-tertiary) 46%, var(--stoa-accent)); }
.stoa-sub { color: var(--ui-text-tertiary); font-size: 12px; }
.stoa-clip { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stoa-sec { margin-top: 16px; }
.stoa-sechead { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; padding: 0 8px 4px; }
.stoa-row { display: flex; gap: 8px; align-items: flex-start; width: 100%; padding: 6px 8px; border: 0; border-radius: 6px; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-row:hover { background: var(--ui-row-hover-background); }
.stoa-row[data-open="1"] { background: var(--stoa-accent-soft); box-shadow: inset 0 0 0 1px var(--stoa-accent-line); }
.stoa-dot { flex: none; width: 6px; height: 6px; margin-top: 6px; border-radius: 50%; background: var(--ui-text-quaternary); }
.stoa-dot[data-tone="bad"] { background: var(--ui-red); }
.stoa-dot[data-tone="warn"] { background: var(--ui-orange); }
.stoa-dot[data-tone="good"] { background: var(--stoa-accent); }
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
.stoa-meter i[data-on="1"] { background: var(--stoa-accent); }
.stoa-facts { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 14px; margin: 14px 0 2px; padding-top: 12px; border-top: 1px solid var(--ui-stroke-tertiary); }
.stoa-facts span { display: block; overflow-wrap: anywhere; }
.stoa-next { margin-top: 10px; }
.stoa-log { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--ui-stroke-tertiary); }
.stoa-log p { margin: 0 0 5px; font-size: 12px; color: var(--ui-text-secondary); }
.stoa-btns { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
.stoa-btn, .stoa-select { height: 28px; padding: 0 10px; border: 1px solid var(--ui-stroke-primary); border-radius: 6px; background: transparent; color: var(--ui-text-primary); font: inherit; font-size: 12px; cursor: pointer; }
.stoa-btn, .stoa-select { transition: background .15s ease, opacity .15s ease; }
.stoa-btn:hover, .stoa-select:hover { background: var(--ui-control-hover-background); }
.stoa-btn[data-kind="primary"]:hover { background: var(--stoa-accent); opacity: .88; }
.stoa-btn[data-kind="primary"] { border-color: var(--stoa-accent); background: var(--stoa-accent); color: var(--stoa-on-accent); font-weight: 550; }
.stoa-btn[data-kind="quiet"] { border-color: transparent; color: var(--ui-text-secondary); }
.stoa-btn:disabled, .stoa-select:disabled { opacity: .5; cursor: default; }
.stoa-chip { display: inline-block; padding: 1px 6px; border: 1px solid var(--ui-stroke-secondary); border-radius: 4px; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 10px; letter-spacing: .08em; text-transform: uppercase; color: var(--ui-text-tertiary); white-space: nowrap; }
.stoa-chip[data-tone="bad"] { color: var(--ui-red); border-color: var(--ui-red); }
.stoa-chip[data-tone="warn"] { color: var(--ui-orange); border-color: var(--ui-orange); }
.stoa-chip[data-tone="good"] { color: var(--stoa-accent-text); border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); }
.stoa-chips { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }
.stoa-foot { display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 12px; border-top: 1px solid var(--ui-stroke-tertiary); }
.stoa-tabs { display: inline-flex; gap: 2px; margin: 22px 0 18px; padding: 3px; border: 1px solid var(--ui-stroke-secondary); border-radius: 10px; background: var(--ui-bg-card); }
.stoa-tab { display: inline-flex; align-items: center; gap: 8px; height: 32px; padding: 0 15px; border: 0; border-radius: 7px; background: transparent; color: var(--ui-text-secondary); font: inherit; font-size: 13px; cursor: pointer; outline: none; transition: background .15s ease, color .15s ease; }
.stoa-tab:hover { color: var(--ui-text-primary); background: var(--ui-control-hover-background); }
.stoa-tab[data-on="1"], .stoa-tab[data-on="1"]:hover { background: var(--stoa-accent-soft); box-shadow: inset 0 0 0 1px var(--stoa-accent-line); color: var(--ui-text-primary); font-weight: 600; }
.stoa-tab small { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 10px; opacity: .6; }
.stoa button:focus-visible, .stoa select:focus-visible, .stoa input:focus-visible { outline: 2px solid var(--stoa-accent-line); outline-offset: 2px; }
.stoa-split { display: grid; grid-template-columns: minmax(0, 1fr); gap: 20px; align-items: start; }
.stoa-split[data-side="1"] { grid-template-columns: minmax(0, 1fr) 360px; }
.stoa-split > .stoa-box { position: sticky; top: 0; max-height: calc(100vh - 235px); overflow-y: auto; }   /* it ends above the dock */
.stoa-board { display: grid; grid-auto-flow: column; grid-auto-columns: minmax(176px, 1fr); gap: 12px; overflow-x: auto; padding-bottom: 8px; }
.stoa-colhead { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; padding: 0 2px 8px; }
.stoa-card { display: block; width: 100%; margin-bottom: 8px; padding: 10px 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: 8px; background: var(--ui-bg-card); color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-card { transition: border-color .15s ease, transform .15s ease; }
.stoa-card:hover, .stoa-card[data-open="1"] { border-color: var(--ui-stroke-primary); }
.stoa-card:hover { transform: translateY(-1px); }
.stoa-card[data-open="1"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); }
.stoa-cardfoot { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; margin-top: 6px; font-size: 12px; }
.stoa-cardfoot em { flex: none; font-style: normal; font-size: 11px; color: var(--ui-text-tertiary); }
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
.stoa-filters { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.stoa-filters .stoa-input { flex: 1 1 120px; min-width: 0; height: 28px; padding: 0 9px; border: 1px solid var(--ui-stroke-secondary); border-radius: 6px; background: transparent; color: var(--ui-text-primary); font: inherit; font-size: 12px; outline: none; }\n.stoa-filters .stoa-input:focus { border-color: var(--ui-stroke-primary); }\n.stoa-filters .stoa-input::placeholder { color: var(--ui-text-quaternary); }
.stoa-filters .stoa-select { max-width: 150px; }
.stoa-toggle { height: 28px; padding: 0 9px; border: 1px solid var(--ui-stroke-secondary); border-radius: 6px; background: transparent; color: var(--ui-text-secondary); font: inherit; font-size: 12px; cursor: pointer; }
.stoa-toggle[data-on="1"] { border-color: var(--stoa-accent-line); color: var(--ui-text-primary); background: var(--stoa-accent-soft); }
.stoa-funnel { margin: 0 0 20px; padding: 16px 18px 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: 10px; background: var(--ui-bg-card); }
.stoa-funnel svg { display: block; width: 100%; height: auto; overflow: visible; }
.stoa-funnel text { font-family: ui-monospace, "SF Mono", Menlo, monospace; }
.stoa-fn-block { fill: url(#stoa-fn-fill); }
.stoa-fn-won { fill: var(--stoa-accent); filter: drop-shadow(0 0 7px color-mix(in srgb, var(--stoa-accent) 55%, transparent)); }
.stoa-fn-block, .stoa-fn-won { transform-box: fill-box; transform-origin: center; animation: stoa-rise .7s cubic-bezier(.2, .7, .2, 1) both; transition: filter .15s ease; }
.stoa-fn-link { fill: url(#stoa-fn-flow); }
.stoa-fn-pill { fill: var(--ui-bg-card); stroke: var(--ui-stroke-secondary); }
.stoa-fn-hit { fill: transparent; }
.stoa-fn-step[data-press="1"] { cursor: pointer; }
.stoa-fn-step[data-press="1"]:hover .stoa-fn-block { filter: brightness(1.3); }
.stoa-fn-step[data-on="1"] .stoa-fn-block { fill: var(--stoa-accent); opacity: .85; }
.stoa-fn-step[data-on="1"] .stoa-fn-label { fill: var(--stoa-accent-text); }
.stoa-fn-label { font-size: 9.5px; letter-spacing: .12em; fill: var(--ui-text-tertiary); }
.stoa-fn-num { font-size: 15px; font-weight: 600; fill: var(--ui-text-primary); font-family: inherit !important; }
.stoa-fn-pct { font-size: 11px; fill: var(--ui-text-primary); }
.stoa-fn-sub { font-size: 9px; letter-spacing: .06em; fill: var(--ui-text-tertiary); }
.stoa-funnel-foot { display: flex; flex-wrap: wrap; gap: 4px 18px; margin-top: 6px; font-size: 12px; color: var(--ui-text-tertiary); }
.stoa-funnel-foot b { color: var(--ui-text-primary); font-weight: 600; }
.stoa-list { max-height: 340px; overflow-y: auto; margin-top: 10px; }
.stoa-pick { display: flex; gap: 9px; align-items: flex-start; padding: 7px 0; border-top: 1px solid var(--ui-stroke-tertiary); cursor: pointer; }
.stoa-pick:first-child { border-top: 0; }
.stoa-pick input { flex: none; margin-top: 3px; accent-color: var(--stoa-accent); }
.stoa-pick[data-off="1"] .stoa-grow { opacity: .5; }
.stoa-rest { margin-top: 10px; font-size: 12px; color: var(--ui-text-tertiary); }
.stoa-rest summary { cursor: pointer; }
.stoa-rest p { margin: 4px 0 0 14px; }
.stoa-reply { margin-top: 10px; overflow-wrap: anywhere; }
.stoa-reply p { margin: 0 0 8px; }
.stoa-reply p:last-child { margin-bottom: 0; }
/* The dock floats at the foot of the page; the board fades out beneath it instead of running into it. */
.stoa-dockzone { position: sticky; bottom: 0; z-index: 6; margin: 26px -34px 0; padding: 54px 34px 16px; background: linear-gradient(to top, var(--background, #0d1015) 46%, transparent); pointer-events: none; }
.stoa-dock { width: min(780px, 100%); margin: 0 auto; pointer-events: auto; }
.stoa-glass { border: 1px solid var(--ui-stroke-primary); border-radius: 16px; background: color-mix(in srgb, var(--ui-bg-elevated, var(--background, #14161b)) 84%, transparent); -webkit-backdrop-filter: blur(18px) saturate(1.2); backdrop-filter: blur(18px) saturate(1.2); box-shadow: 0 18px 50px -14px rgba(0, 0, 0, .6), 0 2px 8px rgba(0, 0, 0, .22); }
.stoa-sheet { margin-bottom: 8px; padding: 10px 14px 14px; max-height: min(46vh, 430px); overflow-y: auto; animation: stoa-lift .2s ease both; }
.stoa-sheethead { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 8px; }
.stoa-turn + .stoa-turn { margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--ui-stroke-tertiary); }
.stoa-youline { display: flex; justify-content: flex-end; }
.stoa-you { max-width: 86%; padding: 6px 12px; border-radius: 13px 13px 4px 13px; background: var(--stoa-accent-soft); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--stoa-accent) 24%, transparent); overflow-wrap: anywhere; white-space: pre-wrap; }
.stoa-you small { display: block; margin-top: 2px; font-size: 11px; color: var(--ui-text-tertiary); }
.stoa-working { display: inline-flex; align-items: center; gap: 8px; }
.stoa-working::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--stoa-accent); animation: stoa-blink 1s ease-in-out infinite; }
.stoa-composer { padding: 7px; transition: border-color .15s ease, box-shadow .15s ease; }
.stoa-composer:focus-within { border-color: var(--stoa-accent-line); box-shadow: 0 18px 50px -14px rgba(0, 0, 0, .6), 0 0 0 1px var(--stoa-accent-line); }
.stoa-composerrow { display: flex; align-items: flex-end; gap: 3px; }
.stoa-composer textarea { flex: 1; min-width: 0; min-height: 34px; max-height: 132px; padding: 7px 6px; border: 0; background: transparent; color: var(--ui-text-primary); font: inherit; font-size: 14px; line-height: 1.45; resize: none; }
.stoa .stoa-composer textarea, .stoa .stoa-composer textarea:focus-visible { outline: none; }
.stoa-composer textarea::placeholder { color: var(--ui-text-quaternary); }
.stoa-icon { flex: none; display: inline-flex; align-items: center; justify-content: center; width: 34px; height: 34px; padding: 0; border: 0; border-radius: 10px; background: transparent; color: var(--ui-text-secondary); cursor: pointer; transition: background .15s ease, color .15s ease, opacity .15s ease; }
.stoa-icon:hover { background: var(--ui-control-hover-background); color: var(--ui-text-primary); }
.stoa-icon:disabled { opacity: .4; cursor: default; }
.stoa-icon[data-kind="send"] { margin-left: 3px; border-radius: 50%; background: var(--stoa-accent); color: var(--stoa-on-accent); }
.stoa-icon[data-kind="send"]:hover { background: var(--stoa-accent); opacity: .88; }
.stoa-icon[data-kind="send"]:disabled { opacity: .32; }
.stoa-icon[data-on="1"] { color: var(--ui-red); background: color-mix(in srgb, var(--ui-red) 14%, transparent); animation: stoa-pulse 1.4s ease-in-out infinite; }
.stoa-filechip { display: inline-flex; align-items: center; gap: 6px; margin: 1px 0 6px 4px; padding: 2px 3px 2px 9px; border: 1px solid var(--ui-stroke-secondary); border-radius: 8px; font-size: 12px; color: var(--ui-text-secondary); }
.stoa-hints { display: flex; flex-wrap: wrap; justify-content: center; gap: 6px; margin-bottom: 8px; }
.stoa-hint { height: 28px; padding: 0 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: 999px; background: color-mix(in srgb, var(--ui-bg-elevated, var(--background, #14161b)) 80%, transparent); -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); color: var(--ui-text-secondary); font: inherit; font-size: 12px; cursor: pointer; transition: color .15s ease, border-color .15s ease; }
.stoa-hint:hover { color: var(--ui-text-primary); border-color: var(--ui-stroke-primary); }
.stoa-hint:disabled { opacity: .5; cursor: default; }
.stoa-dropnote { position: sticky; top: 0; z-index: 7; width: fit-content; margin: 0 auto 12px; padding: 8px 18px; border: 1px solid var(--stoa-accent-line); border-radius: 999px; background: var(--stoa-accent-soft); -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); font-size: 13px; }
.stoa-page[data-drop="1"] { box-shadow: inset 0 0 0 2px var(--stoa-accent-line); }
.stoa-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: flex-end; gap: 14px 44px; }
.stoa-headstats { display: flex; flex-wrap: wrap; gap: 10px 36px; padding-bottom: 5px; }
.stoa-headstats b { display: block; margin-top: 3px; font-size: 20px; font-weight: 600; line-height: 1.1; font-variant-numeric: tabular-nums; }
.stoa-tabsrow { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px; }
.stoa-col { min-width: 0; border-radius: 10px; transition: opacity .2s ease, background .15s ease; }
.stoa-col[data-dim="1"] { opacity: .38; }
.stoa-col[data-over="1"] { background: var(--stoa-accent-soft); box-shadow: inset 0 0 0 1px var(--stoa-accent-line); }   /* inset: the board clips anything drawn outside a column */
.stoa-weight { height: 2px; margin: 0 2px 9px; border-radius: 2px; background: var(--ui-stroke-tertiary); }
.stoa-weight i { display: block; height: 100%; border-radius: 2px; background: var(--ui-text-tertiary); transition: width .4s ease; }
.stoa-weight[data-won="1"] i { background: var(--stoa-accent); }
.stoa-card[draggable="true"] { cursor: grab; }
.stoa-card[draggable="true"]:active { cursor: grabbing; }
.stoa-split > .stoa-box { animation: stoa-slide .22s ease both; }
@keyframes stoa-rise { from { transform: scaleY(.12); opacity: 0; } }
@keyframes stoa-lift { from { transform: translateY(8px); opacity: 0; } }
@keyframes stoa-slide { from { transform: translateX(10px); opacity: 0; } }
@keyframes stoa-blink { 50% { opacity: .25; } }
@keyframes stoa-pulse { 50% { box-shadow: 0 0 0 5px color-mix(in srgb, var(--ui-red) 16%, transparent); } }
@media (prefers-reduced-motion: reduce) { .stoa *, .stoa *::before { animation: none !important; transition: none !important; } }
.stoa-form { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 10px; margin-top: 12px; }
.stoa-form label { display: block; min-width: 0; }
.stoa-form label[data-wide="1"] { grid-column: 1 / -1; }
.stoa-field { display: block; width: 100%; height: 28px; margin-top: 3px; padding: 0 8px; border: 1px solid var(--ui-stroke-secondary); border-radius: 6px; background: transparent; color: var(--ui-text-primary); font: inherit; font-size: 12px; outline: none; }
.stoa-field:focus { border-color: var(--ui-stroke-primary); }
.stoa-field::placeholder { color: var(--ui-text-quaternary); }
.stoa-edit { margin-top: 12px; padding-top: 2px; border-top: 1px solid var(--ui-stroke-tertiary); }
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

// -- a form: one labelled input per fact ----------------------------------------------------------
function Field({ label, wide, children, ...props }) {
  return h('label', { 'data-wide': wide ? '1' : '0' }, h('small', { className: 'stoa-eyebrow' }, label),
    children || h('input', { className: 'stoa-field', ...props }))
}

const uniq = values => [...new Set(values.filter(Boolean))].sort()

/** Change a record by hand: its next step, its stage, its facts, and a line for the log. */
function Edit({ deal, state, done }) {
  const pb = state.playbook, terms = pb.terms
  const start = { next_step: deal.next_step || '', next_due: deal.next_due || '', value: deal.value ? String(deal.value) : '', owner: deal.owner || '',
    company: deal.company || '', group: deal.group || '', email: deal.email || '' }
  const [v, setV] = useState(start)
  const [note, setNote] = useState('')
  const [kind, setKind] = useState('note')
  const [busy, setBusy] = useState(false)
  const set = key => event => setV({ ...v, [key]: event.target.value })
  const send = body => { setBusy(true); return act(deal.id, body).then(result => { setBusy(false); return result }) }
  const save = () => {
    const body = { verb: 'update' }
    for (const key of Object.keys(v)) {
      const now = String(v[key]).trim()
      if (now !== start[key]) body[key] = key === 'value' ? Number(now) || 0 : now
    }
    if (Object.keys(body).length === 1) return done()
    send(body).then(result => { if (result) done() })
  }
  const log = () => { if (note.trim()) send({ verb: 'log', kind, text: note.trim() }).then(result => { if (result) setNote('') }) }
  const open = deal.status === 'open'
  return h('div', { className: 'stoa-edit' },
    h('div', { className: 'stoa-form' },
      h(Field, { label: 'Next step', wide: true, value: v.next_step, onChange: set('next_step') }),
      h(Field, { label: 'Due', type: 'date', value: v.next_due, onChange: set('next_due') }),
      open ? h(Field, { label: 'Stage' }, h('select', { className: 'stoa-field', value: deal.stage, disabled: busy,
        onChange: event => send({ verb: 'move', stage: event.target.value }) },
        pb.stages.map(stage => h('option', { key: stage.id, value: stage.id }, stage.label)))) : h('span'),
      h(Field, { label: cap(terms.value), type: 'number', min: 0, value: v.value, onChange: set('value') }),
      h(Field, { label: cap(terms.owner), value: v.owner, onChange: set('owner') }),
      h(Field, { label: cap(terms.company), value: v.company, onChange: set('company') }),
      h(Field, { label: cap(terms.group), value: v.group, onChange: set('group') }),
      h(Field, { label: 'Email', wide: true, type: 'email', value: v.email, onChange: set('email') })),
    h('div', { className: 'stoa-btns' },
      h(Btn, { kind: 'primary', disabled: busy, onClick: save }, 'Save'),
      h(Btn, { kind: 'quiet', disabled: busy, onClick: done }, 'Cancel')),
    h('div', { className: 'stoa-form' },
      h(Field, { label: 'Log what happened', wide: true, value: note, placeholder: 'Called her; she wants to see the comp range', onChange: event => setNote(event.target.value),
        onKeyDown: event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) log() } })),
    h('div', { className: 'stoa-btns', style: { marginTop: 8 } },
      h('select', { className: 'stoa-select', value: kind, 'aria-label': 'What it was', onChange: event => setKind(event.target.value) },
        ['note', 'call', 'email', 'meeting'].map(name => h('option', { key: name, value: name }, cap(name)))),
      h(Btn, { disabled: busy || !note.trim(), onClick: log }, 'Add to log')))
}

/** Put someone or something new on the desk by hand. */
function NewRecord({ state, close, opened }) {
  const pb = state.playbook, terms = pb.terms
  const [v, setV] = useState({ name: '', company: '', group: '', stage: pb.stages[0] ? pb.stages[0].id : '', value: '', owner: '', email: '', next_step: '', next_due: '' })
  const [busy, setBusy] = useState(false)
  const set = key => event => setV({ ...v, [key]: event.target.value })
  const save = () => {
    const body = {}
    for (const key of Object.keys(v)) {
      const now = String(v[key]).trim()
      if (now) body[key] = key === 'value' ? Number(now) || 0 : now
    }
    if (!body.name || busy) return
    setBusy(true)
    rest('/deal', { method: 'POST', body })
      .then(result => {
        refresh()
        host.notify({ kind: result && result.exists ? 'info' : 'success', message: result && result.exists ? body.name + ' is already on the desk.' : body.name + ' is on the desk.' })
        opened(result && result.deal ? result.deal.id : '')
      })
      .catch(error => host.notifyError(error, 'Stoa could not add that'))
      .then(() => setBusy(false))
  }
  return h('div', { className: 'stoa-box' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center' } },
      h('div', { className: 'stoa-name', style: { fontSize: 14 } }, 'New ' + terms.deal),
      h(Btn, { kind: 'quiet', onClick: close, 'aria-label': 'Close' }, '×')),
    h('div', { className: 'stoa-form' },
      h(Field, { label: 'Name', wide: true, autoFocus: true, value: v.name, onChange: set('name') }),
      h(Field, { label: cap(terms.company), value: v.company, onChange: set('company') }),
      h(Field, { label: cap(terms.group), list: 'stoa-new-groups', value: v.group, onChange: set('group') }),
      h(Field, { label: 'Stage' }, h('select', { className: 'stoa-field', value: v.stage, onChange: set('stage') },
        pb.stages.map(stage => h('option', { key: stage.id, value: stage.id }, stage.label)))),
      h(Field, { label: cap(terms.value), type: 'number', min: 0, value: v.value, onChange: set('value') }),
      h(Field, { label: cap(terms.owner), list: 'stoa-new-owners', value: v.owner, onChange: set('owner') }),
      h(Field, { label: 'Email', type: 'email', value: v.email, onChange: set('email') }),
      h(Field, { label: 'Next step', wide: true, value: v.next_step, onChange: set('next_step') }),
      h(Field, { label: 'Due', type: 'date', value: v.next_due, onChange: set('next_due') })),
    h('datalist', { id: 'stoa-new-groups' }, uniq(state.deals.map(deal => deal.group)).map(name => h('option', { key: name, value: name }))),
    h('datalist', { id: 'stoa-new-owners' }, uniq(state.deals.map(deal => deal.owner)).map(name => h('option', { key: name, value: name }))),
    h('div', { className: 'stoa-btns' },
      h(Btn, { kind: 'primary', disabled: busy || !v.name.trim(), onClick: save }, 'Add to the desk'),
      h(Btn, { kind: 'quiet', disabled: busy, onClick: close }, 'Cancel')))
}

// -- one record: what it is, what is next, and the buttons ---------------------------------------
function Record({ id, state, sessionId, close }) {
  const pb = state.playbook
  const query = useQuery({ queryKey: ['stoa', 'deal', id], queryFn: () => rest('/deal/' + encodeURIComponent(id)), staleTime: 1500 })
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState(false)
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
      !open ? h(Btn, { kind: 'primary', disabled: busy, onClick: () => run({ verb: 'reopen' }) }, 'Reopen') : null,
      h(Btn, { kind: 'quiet', onClick: () => setEditing(!editing) }, editing ? 'Close editing' : 'Edit')),
    editing ? h(Edit, { key: deal.stage, deal, state, done: () => setEditing(false) }) : null,
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

// -- narrowing what is shown ----------------------------------------------------------------------
function Filters({ state }) {
  const f = useValue($filter)
  const open = state.deals.filter(deal => deal.status === 'open')
  const owners = [...new Set(open.map(deal => deal.owner).filter(Boolean))].sort()
  const groups = [...new Set(open.map(deal => deal.group).filter(Boolean))].sort()
  const terms = state.playbook.terms
  return h('div', { className: 'stoa-filters' },
    h('input', { className: 'stoa-input', value: f.text, placeholder: 'Filter…', 'aria-label': 'Filter by text', onChange: event => setFilter({ text: event.target.value }) }),
    owners.length > 1 ? h('select', { className: 'stoa-select', value: f.owner, 'aria-label': 'Owner', onChange: event => setFilter({ owner: event.target.value }) },
      [h('option', { key: '', value: '' }, 'Everyone')].concat(owners.map(owner => h('option', { key: owner, value: owner }, owner)))) : null,
    groups.length > 1 ? h('select', { className: 'stoa-select', value: f.group, 'aria-label': cap(terms.group), onChange: event => setFilter({ group: event.target.value }) },
      [h('option', { key: '', value: '' }, 'All ' + terms.groups)].concat(groups.map(group => h('option', { key: group, value: group }, group)))) : null,
    h('button', { type: 'button', className: 'stoa-toggle', 'data-on': f.due ? '1' : '0', onClick: () => setFilter({ due: !f.due }) }, 'Due'),
    h('button', { type: 'button', className: 'stoa-toggle', 'data-on': f.quiet ? '1' : '0', onClick: () => setFilter({ quiet: !f.quiet }) }, 'Quiet'),
    filtering(f) ? h('button', { type: 'button', className: 'stoa-toggle', onClick: () => setFilter({ ...NO_FILTER }) }, 'Clear') : null)
}

// -- the funnel: how many reach each stage, and what share moves on ------------------------------
// Press a stage to narrow the board to it; press it again to let go.
function Funnel({ state }) {
  const flow = state.flow, pb = state.playbook
  const filter = useValue($filter)
  if (!flow || !flow.total) return null
  const steps = flow.stages.map(stage => ({ id: stage.id, label: stage.label, reached: stage.reached, here: stage.here, lost: stage.lost, value: stage.value }))
    .concat([{ id: '', label: flow.won_label, reached: flow.won, won: true, value: flow.won_value }])
  const W = 1000, H = 196, mid = 102, tall = 100, col = W / steps.length, block = col * 0.5
  const height = step => Math.max(3, (tall * step.reached) / flow.total)
  const closed = flow.won + flow.lost
  const links = [], groups = []
  steps.forEach((step, index) => {
    const x = index * col, tallness = height(step), next = steps[index + 1]
    if (next) {
      const after = height(next), right = (index + 1) * col, centre = x + block + (col - block) / 2
      links.push(h('polygon', { key: 'l' + index, className: 'stoa-fn-link',
        points: [x + block, mid - tallness / 2, right, mid - after / 2, right, mid + after / 2, x + block, mid + tallness / 2].join(' ') }))
      links.push(h('rect', { key: 'q' + index, className: 'stoa-fn-pill', x: centre - 19, y: mid - 10, width: 38, height: 20, rx: 10 }))
      links.push(h('text', { key: 'p' + index, className: 'stoa-fn-pct', x: centre, y: mid + 4, textAnchor: 'middle' },
        step.reached ? Math.round((100 * next.reached) / step.reached) + '%' : '–'))
    }
    const on = Boolean(step.id) && filter.stage === step.id
    groups.push(h('g', { key: 'g' + index, className: 'stoa-fn-step', 'data-on': on ? '1' : '0', 'data-press': step.id ? '1' : '0',
      onClick: step.id ? () => setFilter({ stage: on ? '' : step.id }) : undefined },
      h('title', null, step.id ? (on ? 'Show every stage' : 'Show only ' + step.label) : step.label),
      h('rect', { className: 'stoa-fn-hit', x: x - 4, y: 0, width: col - 2, height: H }),
      h('rect', { className: step.won ? 'stoa-fn-won' : 'stoa-fn-block', x, y: mid - tallness / 2, width: block, height: tallness, rx: 3,
        style: { animationDelay: index * 55 + 'ms' } }),
      h('text', { className: 'stoa-fn-label', x, y: 11 }, step.label.toUpperCase().slice(0, Math.max(6, Math.floor(col / 7.6)))),
      h('text', { className: 'stoa-fn-num', x, y: 31 }, String(step.reached)),
      h('text', { className: 'stoa-fn-sub', x, y: mid + tall / 2 + 22 },
        step.won ? (step.value ? money(step.value, pb.currency) : '') : step.here + ' here' + (step.value ? ' · ' + money(step.value, pb.currency) : '')),
      step.lost ? h('text', { className: 'stoa-fn-sub', x, y: mid + tall / 2 + 35 }, step.lost + ' lost here') : null))
  })
  return h('div', { className: 'stoa-funnel' },
    h('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'How many records reach each stage, and what share moves on' },
      h('defs', null,
        h('linearGradient', { id: 'stoa-fn-fill', x1: 0, y1: 0, x2: 0, y2: 1 },
          h('stop', { offset: '0%', style: { stopColor: 'var(--ui-text-primary)', stopOpacity: 0.62 } }),
          h('stop', { offset: '100%', style: { stopColor: 'var(--ui-text-primary)', stopOpacity: 0.36 } })),
        h('linearGradient', { id: 'stoa-fn-flow', x1: 0, y1: 0, x2: 1, y2: 0 },
          h('stop', { offset: '0%', style: { stopColor: 'var(--ui-text-primary)', stopOpacity: 0.2 } }),
          h('stop', { offset: '100%', style: { stopColor: 'var(--ui-text-primary)', stopOpacity: 0.08 } }))),
      links, groups),
    h('div', { className: 'stoa-funnel-foot' },
      h('span', null, h('b', null, closed ? Math.round((100 * flow.won) / closed) + '%' : '–'), ' win rate' + (closed ? ' (' + flow.won + ' of ' + closed + ' closed)' : '')),
      h('span', null, h('b', null, Math.round((100 * flow.won) / flow.total) + '%'), ' of everything that entered has been ' + flow.won_label.toLowerCase()),
      h('span', null, 'Percentages are the share that reached the next stage; ' + pb.terms.deals + ' still in a stage have not had their chance yet.')))
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
  const filter = useValue($filter)
  const [openId, setOpenId] = useState('')
  if (!query.data) return h(Unavailable, { error: query.error, loading: query.isLoading })
  const state = query.data, pb = state.playbook, totals = state.totals
  const everyone = state.deals.filter(deal => deal.status === 'open')
  const open = everyone.filter(deal => matches(deal, filter))
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
        h('div', { className: 'stoa-rule' }),
        state.deals.length ? h('div', { style: { marginTop: 10 } }, h(Filters, { state })) : null,
        filtering(filter) ? h('div', { className: 'stoa-sub', style: { marginTop: 6 } }, 'Showing ' + open.length + ' of ' + everyone.length) : null),
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
      calls.length && !filtering(filter) ? h('div', { className: 'stoa-sec' },
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

// -- the dock at the foot of the page: say it, dictate it, or hand it a file ------------------------
const ICON = {
  clip: 'M21.4 11.1l-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5',
  mic: 'M12 2.5a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0v-6a3 3 0 0 0-3-3zM5.5 11a6.5 6.5 0 0 0 13 0M12 17.5v4',
  up: 'M12 19.5v-15M5.5 11L12 4.5 18.5 11',
  down: 'M6 9.5l6 6 6-6',
  x: 'M6.5 6.5l11 11M17.5 6.5l-11 11'
}
const Icon = ({ name, size }) => h('svg', { width: size || 17, height: size || 17, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
  strokeWidth: 1.8, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true }, h('path', { d: ICON[name] }))

/** A reply, set plainly: paragraphs, bullets and bold. Lines that only draw a card in a chat are left out. */
function Words({ text }) {
  const clean = String(text || '').replace(/^[ \t]*::stoa-[a-z]+\{[^}\n]*\}[ \t]*$/gm, '').trim()
  const bold = line => line.split(/(\*\*[^*]+\*\*)/g).map((part, index) => (/^\*\*[^*]+\*\*$/.test(part) ? h('b', { key: index }, part.slice(2, -2)) : part))
  return h('div', { className: 'stoa-reply' }, clean.split(/\n{2,}/).map((block, index) => h('p', { key: index },
    block.split('\n').map((line, at) => h('span', { key: at, style: { display: 'block' } }, bold(line.replace(/^\s*[-*]\s+/, '•  ').replace(/^#+\s+/, '')))))))
}

// Dictation is the browser's own speech recognition, where it has one (Chrome, Edge and Safari do;
// the Desktop app does not, and there the microphone in the full chat is the one to use).
const Recognition = typeof window !== 'undefined' ? window.SpeechRecognition || window.webkitSpeechRecognition : null

function Dock({ state }) {
  const talk = useValue($talk)
  const file = useValue($attached)
  const [text, setText] = useState('')
  const [listening, setListening] = useState(false)
  const area = useRef(null), picker = useRef(null), sheet = useRef(null), ear = useRef(null), before = useRef('')
  useEffect(() => {
    const el = area.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 132) + 'px'
  }, [text])
  useEffect(() => {
    if (sheet.current) sheet.current.scrollTop = sheet.current.scrollHeight
  }, [talk.turns, talk.open])
  useEffect(() => () => { try { if (ear.current) ear.current.abort() } catch { /* already stopped */ } }, [])
  const hush = () => { try { if (ear.current) ear.current.stop() } catch { /* already stopped */ } }
  const send = line => {
    const words = typeof line === 'string' ? line : text
    if ((!words.trim() && !file) || talk.busy) return
    hush()
    say(words, file)
    setText('')
    $attached.set(null)
  }
  const dictate = () => {
    if (listening) return hush()
    try {
      const hearing = new Recognition()
      hearing.lang = navigator.language || 'en-US'
      hearing.interimResults = true
      hearing.continuous = true
      before.current = text.trim() ? text.replace(/\s*$/, ' ') : ''
      hearing.onresult = event => {
        let said = ''
        for (let index = 0; index < event.results.length; index++) said += event.results[index][0].transcript
        setText(before.current + said)
      }
      hearing.onerror = event => {
        setListening(false)
        if (event.error === 'no-speech' || event.error === 'aborted') return
        host.notify({ kind: 'warning', message: event.error === 'not-allowed' || event.error === 'service-not-allowed'
          ? 'The browser did not allow the microphone.' : 'Dictation is not available here. The microphone in the full chat works everywhere.' })
      }
      hearing.onend = () => setListening(false)
      hearing.start()
      ear.current = hearing
      setListening(true)
    } catch {
      setListening(false)
    }
  }
  const hints = ['What needs me today?', 'Who has gone quiet?', 'What moved this week?']
  return h('div', { className: 'stoa-dockzone' }, h('div', { className: 'stoa-dock' },
    talk.turns.length && talk.open ? h('div', { className: 'stoa-glass stoa-sheet', ref: sheet },
      h('div', { className: 'stoa-sheethead' },
        h('span', { className: 'stoa-eyebrow' }, 'Stoa desk'),
        h('span', { style: { display: 'flex', gap: 2 } },
          h(Btn, { kind: 'quiet', onClick: openTalk }, 'Open the conversation'),
          h('button', { type: 'button', className: 'stoa-icon', 'aria-label': 'Hide the conversation', title: 'Hide', onClick: () => setTalk({ open: false }) }, h(Icon, { name: 'down' })))),
      talk.turns.map((turn, index) => h('div', { key: index, className: 'stoa-turn' },
        h('div', { className: 'stoa-youline' }, h('span', { className: 'stoa-you' }, turn.you, turn.file ? h('small', null, turn.file) : null)),
        turn.reply ? h(Words, { text: turn.reply }) : null,
        turn.note ? h('div', { className: 'stoa-sub stoa-working', style: { marginTop: 8 } }, turn.note) : null,
        turn.error ? h('div', { style: { marginTop: 8, color: 'var(--ui-red)' } }, turn.error) : null)))
      : talk.turns.length ? h('div', { className: 'stoa-hints' },
        h('button', { type: 'button', className: 'stoa-hint', onClick: () => setTalk({ open: true }) }, 'Show the conversation'))
        : !text && !file ? h('div', { className: 'stoa-hints' }, hints.map(hint =>
          h('button', { type: 'button', key: hint, className: 'stoa-hint', disabled: talk.busy, onClick: () => send(hint) }, hint))) : null,
    h('div', { className: 'stoa-glass stoa-composer' },
      file ? h('div', { className: 'stoa-filechip' }, h(Icon, { name: 'clip', size: 13 }), file.name,
        h('button', { type: 'button', className: 'stoa-icon', style: { width: 22, height: 22 }, 'aria-label': 'Remove the file', onClick: () => $attached.set(null) }, h(Icon, { name: 'x', size: 13 }))) : null,
      h('div', { className: 'stoa-composerrow' },
        h('button', { type: 'button', className: 'stoa-icon', title: 'Hand it a file: a CSV becomes a review, a text file goes with your message', 'aria-label': 'Attach a file',
          onClick: () => picker.current && picker.current.click() }, h(Icon, { name: 'clip' })),
        h('input', { ref: picker, type: 'file', hidden: true, accept: '.csv,.tsv,.txt,.md,.json,.vcf,text/*',
          onChange: event => { takeFile(event.target.files[0]); event.target.value = '' } }),
        h('textarea', { ref: area, rows: 1, value: text, disabled: talk.busy, 'aria-label': 'Talk to the desk',
          placeholder: listening ? 'Listening…' : 'Tell Stoa what happened, ask it anything, or drop a list on the page…',
          onChange: event => setText(event.target.value),
          onKeyDown: event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send() } } }),
        Recognition ? h('button', { type: 'button', className: 'stoa-icon', 'data-on': listening ? '1' : '0', disabled: talk.busy,
          title: listening ? 'Stop dictating' : 'Dictate (your browser\'s speech recognition)', 'aria-label': listening ? 'Stop dictating' : 'Dictate', onClick: dictate },
          h(Icon, { name: 'mic' })) : null,
        h('button', { type: 'button', className: 'stoa-icon', 'data-kind': 'send', disabled: talk.busy || (!text.trim() && !file), title: 'Send', 'aria-label': 'Send',
          onClick: () => send() }, h(Icon, { name: 'up' }))))))
}

// -- the full page -------------------------------------------------------------------------------
// The board is worked by hand as well as by talking: drag a card to another column to move it.
const DRAG = 'text/stoa-deal'
const dragging = event => Boolean(event.dataTransfer) && [...event.dataTransfer.types].includes(DRAG)

function Board({ state, openId, setOpenId }) {
  const pb = state.playbook
  const filter = useValue($filter)
  const [over, setOver] = useState('')
  const open = state.deals.filter(deal => deal.status === 'open' && matches(deal, filter))
  const won = state.deals.filter(deal => deal.status === 'won' && matches(deal, { ...filter, stage: '', due: false, quiet: false }))
    .sort((a, b) => (a.stage_at < b.stage_at ? 1 : -1))
  const known = new Set(pb.stages.map(stage => stage.id))
  const strays = open.filter(deal => !known.has(deal.stage))
  const columns = pb.stages.map(stage => ({ ...stage, here: open.filter(deal => deal.stage === stage.id) }))
    .concat(strays.length ? [{ id: '~other', label: 'Other stages', other: true, here: strays }] : [])
    .concat([{ id: '~won', label: pb.won.label, won: true, here: won.slice(0, 8) }])
    .map(column => ({ ...column, value: column.here.reduce((sum, deal) => sum + (deal.value || 0), 0) }))
  const most = Math.max(1, ...columns.filter(column => !column.won).map(column => column.value))
  const drop = (event, column) => {
    const id = event.dataTransfer.getData(DRAG)
    setOver('')
    if (!id || column.other) return
    event.preventDefault()
    const deal = state.deals.find(item => item.id === id)
    const target = column.won ? pb.won.id : column.id
    if (deal && deal.stage !== target) act(id, { verb: 'move', stage: target })
  }
  return h('div', { className: 'stoa-board' }, columns.map(column =>
    h('div', { key: column.id, className: 'stoa-col', 'data-over': over === column.id ? '1' : '0', 'data-dim': filter.stage && !column.won && column.id !== filter.stage ? '1' : '0',
      onDragOver: event => { if (dragging(event) && !column.other) { event.preventDefault(); if (over !== column.id) setOver(column.id) } },
      onDragLeave: event => { if (!event.currentTarget.contains(event.relatedTarget)) setOver('') },
      onDrop: event => drop(event, column) },
      h('div', { className: 'stoa-colhead' }, h('span', { className: 'stoa-eyebrow stoa-clip' }, column.label),
        h('span', { className: 'stoa-num' }, column.here.length + (column.value ? ' · ' + money(column.value, pb.currency) : ''))),
      h('div', { className: 'stoa-weight', 'data-won': column.won ? '1' : '0' }, h('i', { style: { width: (column.won ? 100 : Math.round((100 * column.value) / most)) + '%' } })),
      column.here.length ? column.here.map(deal => h('button', { type: 'button', key: deal.id, className: 'stoa-card', 'data-open': openId === deal.id ? '1' : '0',
        draggable: !column.won, title: column.won ? undefined : 'Drag to another column to move it',
        onDragStart: event => { event.dataTransfer.setData(DRAG, deal.id); event.dataTransfer.effectAllowed = 'move' },
        onDragEnd: () => setOver(''),
        onClick: () => setOpenId(openId === deal.id ? '' : deal.id) },
        h('h5', null, h('span', { className: 'stoa-clip' }, deal.name), deal.value ? h('span', { className: 'stoa-num' }, money(deal.value, pb.currency)) : null),
        h('div', { className: 'stoa-cardfoot', style: { marginTop: 0 } },
          h('span', { className: 'stoa-sub stoa-clip' }, [deal.company, deal.group && deal.group !== deal.company ? deal.group : ''].filter(Boolean).join(' · ') || ' '),
          deal.owner ? h('em', null, deal.owner) : null),
        deal.next_step && !column.won ? h('div', { className: 'stoa-clip', style: { marginTop: 6, fontSize: 12 } }, deal.next_step) : null,
        deal.due || deal.stale || deal.pending || column.other ? h('div', { className: 'stoa-chips' },
          column.other ? h(Chip, null, deal.stage_label) : null,
          deal.due ? h(Chip, { tone: dueTone(deal) }, deal.next_due < today() ? 'overdue' : 'due today') : null,
          deal.stale && !deal.due ? h(Chip, null, 'quiet') : null,
          deal.pending ? h(Chip, { tone: 'good' }, 'draft waiting') : null) : null))
        : h('div', { className: 'stoa-empty' }, over === column.id ? 'Drop it here' : '—'))))
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
  const [adding, setAdding] = useState(false)
  const [marketId, setMarketId] = useState('')
  const [dropping, setDropping] = useState(false)
  const split = useRef(null)
  // A record or the form opens beside the board: bring it clear of the dock if it would sit under it.
  useEffect(() => {
    if (!adding && !openId) return undefined
    const timer = setTimeout(() => {
      const box = split.current && split.current.querySelector(':scope > .stoa-box')
      const page = split.current && split.current.closest('.stoa-page')
      if (!box || !page) return
      const frame = page.getBoundingClientRect(), rect = box.getBoundingClientRect()
      const hidden = rect.bottom - (frame.bottom - 150)
      if (hidden > 0) page.scrollBy({ top: Math.min(hidden, Math.max(0, rect.top - frame.top - 12)), behavior: 'smooth' })
    }, 260)
    return () => clearTimeout(timer)
  }, [adding, openId])
  if (!query.data) return h('div', { className: 'stoa stoa-page' }, h(Unavailable, { error: query.error, loading: query.isLoading }))
  const state = query.data, pb = state.playbook, totals = state.totals
  const market = state.markets.find(item => item.id === marketId) || state.markets[0]
  const stats = [['Open', totals.open], ['Due', totals.due], ['Quiet', totals.stale], ['Mapped', totals.mapped], ['From the map', totals.tracked],
    ['Won', totals.won + (totals.won_value ? ' · ' + money(totals.won_value, pb.currency) : '')]]
  // A file dragged in from the computer lands on the desk, wherever on the page it is let go.
  const carrying = event => Boolean(event.dataTransfer) && [...event.dataTransfer.types].includes('Files')
  return h('div', { className: 'stoa stoa-page', 'data-drop': dropping ? '1' : '0',
    onDragOver: event => { if (carrying(event)) { event.preventDefault(); if (!dropping) setDropping(true) } },
    onDragLeave: event => { if (!event.currentTarget.contains(event.relatedTarget)) setDropping(false) },
    onDrop: event => { if (!carrying(event)) return; event.preventDefault(); setDropping(false); takeFile(event.dataTransfer.files[0]) } },
  h(Style),
  dropping ? h('div', { className: 'stoa-dropnote' }, 'Drop it on the desk: a CSV becomes a review to approve') : null,
  h('div', { className: 'stoa-wrap' },
    h('div', { className: 'stoa-head' },
      h('div', null,
        h('div', { className: 'stoa-eyebrow' }, 'Stoa · ' + pb.title),
        h('div', { className: 'stoa-figure' }, money(totals.open_value, pb.currency) || String(totals.open), h('small', null, totals.open_value ? 'in play' : pb.terms.deals + ' open'))),
      h('div', { className: 'stoa-headstats' }, stats.map(pair => h('div', { key: pair[0] }, h('span', { className: 'stoa-eyebrow' }, pair[0]), h('b', null, String(pair[1])))))),
    h('div', { className: 'stoa-rule' }),
    (state.intakes || []).length ? h('div', { style: { marginTop: 20, maxWidth: 720 } },
      h('div', { className: 'stoa-eyebrow', style: { marginBottom: 6 } }, 'Waiting for your review'),
      state.intakes.map(item => h('div', { key: item.id, style: { marginBottom: 10 } }, h(Review, { id: item.id })))) : null,
    h('div', { className: 'stoa-tabsrow' },
      h('div', { className: 'stoa-tabs', role: 'tablist' }, [['pipeline', 'Pipeline', totals.open], ['market', 'Market map', totals.mapped], ['activity', 'Activity', 0]].map(item =>
        h('button', { type: 'button', key: item[0], role: 'tab', 'aria-selected': tab === item[0], className: 'stoa-tab', 'data-on': tab === item[0] ? '1' : '0',
          onClick: () => setTab(item[0]) }, item[1], item[2] ? h('small', null, String(item[2])) : null))),
      tab === 'pipeline' ? h(Btn, { onClick: () => { setOpenId(''); setAdding(true) } }, '+  New ' + pb.terms.deal) : null),
    tab === 'pipeline' ? h('div', null,
      state.deals.length ? h(Funnel, { state }) : null,
      state.deals.length ? h('div', { style: { margin: '0 0 14px', maxWidth: 760 } }, h(Filters, { state })) : null,
      h('div', { className: 'stoa-split', ref: split, 'data-side': adding || openId ? '1' : '0' },
        state.deals.length ? h(Board, { state, openId, setOpenId: id => { setAdding(false); setOpenId(id) } })
          : h('p', { className: 'stoa-sub' }, 'Nothing on the desk yet. Add a ' + pb.terms.deal + ' with the button above, tell Stoa about one below, drop a list on the page, or ask it to map a market.'),
        adding ? h(NewRecord, { state, close: () => setAdding(false), opened: id => { setAdding(false); setOpenId(id) } })
          : openId ? h(Record, { key: openId, id: openId, state, close: () => setOpenId('') }) : null)) : null,
    tab === 'market' ? (market
      ? h('div', null,
        state.markets.length > 1 ? h('div', { className: 'stoa-tabs', style: { marginTop: 0 } }, state.markets.map(item =>
          h('button', { type: 'button', key: item.id, className: 'stoa-tab', 'data-on': item.id === market.id ? '1' : '0', onClick: () => setMarketId(item.id) }, item.name))) : null,
        h(Market, { market }))
      : h('p', { className: 'stoa-sub' }, 'No market map yet. Ask Stoa below: “Map the market for us.”')) : null,
    tab === 'activity' ? h('div', { style: { maxWidth: 760 } }, state.activity.map(entry => h('div', { key: entry.id, className: 'stoa-row', style: { cursor: 'default' } },
      h('i', { className: 'stoa-dot', 'data-tone': entry.kind === 'stage' ? 'good' : '' }),
      h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name' }, entry.name), h('span', null, '  ' + entry.text),
        h('span', { className: 'stoa-sub', style: { display: 'block' } }, ago(entry.at) + ' · ' + entry.kind + (entry.by ? ' · ' + entry.by : ''))))) ) : null),
  h(Dock, { state }))
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
    storage = ctx.storage
    $filter.set({ ...NO_FILTER, ...(ctx.storage.get('filter', {}) || {}) })
    // While the app is open the desk knows it can draw cards and maps in the conversation here.
    const hello = () => { ctx.rest('/hello', { method: 'POST', body: {} }).catch(() => {}) }
    hello()
    ctx.setInterval(hello, 30000)
    // The agent, a Slack click or another window changed the desk: redraw now, not at the next poll.
    ctx.onEvent('plugin.stoa.changed', refresh)
    // The dock on the page: hear its own conversation, and nobody else's.
    const mine = event => Boolean(talkSession.runtime) && $talk.get().busy
      && String((event && (event.session_id || event.sessionId)) || '') === talkSession.runtime
    ctx.onEvent('message.delta', event => {
      const text = event && event.payload && event.payload.text
      if (mine(event) && typeof text === 'string') setTurn(last => ({ reply: last.reply + text, note: '' }))
    })
    ctx.onEvent('tool.start', event => {
      if (mine(event)) setTurn(last => ({ note: 'Working on the desk…', reply: last.reply && !last.reply.endsWith('\n') ? last.reply + '\n\n' : last.reply }))
    })
    ctx.onEvent('message.complete', event => {
      if (!mine(event)) return
      const payload = (event && event.payload) || {}
      const failed = typeof payload.error === 'string' ? payload.error : ''
      setTurn(last => {
        const final = [payload.text, payload.rendered].find(value => typeof value === 'string' && value.trim()) || last.reply
        return { note: '', reply: final || (failed ? '' : 'Done.'), error: failed }
      })
      setTalk({ busy: false })
      refresh()
    })
    ctx.onEvent('error', event => {
      if (!mine(event)) return
      const payload = (event && event.payload) || {}
      setTurn({ note: '', error: String(payload.message || payload.error || 'Hermes could not answer that.') })
      setTalk({ busy: false })
    })
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
