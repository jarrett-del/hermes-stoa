/**
 * Stoa in the Hermes Desktop app and the Webapp.
 *
 * One file, no build step. It adds a Stoa page (Today, the pipeline, the market map, the activity
 * log and Setup), a live pane that sits beside the conversation, and three components the agent
 * can draw inside a reply: a record card, a market map, and the review of a list someone sent in.
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

// What the pane and the page are narrowed to. One store, so both show the same thing. `stage` is
// not a filter: it is the stage picked out on the board, where every column keeps its cards.
const NO_FILTER = { text: '', owner: '', group: '', stage: '', due: false, quiet: false }
const $filter = atom({ ...NO_FILTER })
function setFilter(patch) {
  const next = { ...$filter.get(), ...patch }
  $filter.set(next)
  // Only whose records and which group are remembered between visits. Anything that hides records
  // for a moment (a search, due, quiet, a picked-out stage) starts clear, so nothing looks missing.
  if (storage) storage.set('filter', { owner: next.owner, group: next.group })
}
const filtering = f => Boolean(f.text || f.owner || f.group || f.due || f.quiet)
function matches(deal, f) {
  if (f.owner && deal.owner !== f.owner) return false
  if (f.group && deal.group !== f.group) return false
  if ((f.due || f.quiet) && !((f.due && deal.due) || (f.quiet && deal.stale && !deal.due))) return false
  if (!f.text) return true
  return [deal.name, deal.company, deal.group, deal.next_step, deal.owner, deal.stage_label].join(' ').toLowerCase().includes(f.text.toLowerCase())
}
const STATE = ['stoa', 'state']
const refresh = () => queryClient.invalidateQueries({ queryKey: ['stoa'] })
const $tab = atom('today') // the page's tab: today | pipeline | cash | market | activity | setup
const $draft = atom('') // words put in the dock for the person to finish and send
let onPage = false // the Stoa page is what is on screen: a click that calls for writing goes to its dock
const $onPage = atom(false) // the same, for the pane to watch: beside the open desk it shows other things
const $want = atom('') // a record picked in the pane while the desk is open: the page opens it
const $view = atom('board') // the pipeline tab: board | companies | groups
const $pipe = atom('') // which pipeline the page is showing ('' = the first): an id from state.pipelines
const $last = atom(null) // what was just done, while it can be taken back: { deal, name, text, op }
let lastTimer = 0

/** Offer to take back what was just done. The offer goes away by itself. */
function offerUndo(last) {
  if (!last || !onPage) return
  $last.set(last)
  clearTimeout(lastTimer)
  lastTimer = setTimeout(() => { if ($last.get() === last) $last.set(null) }, 14000)
}

/** A record taken off the desk (its add was undone) comes back, log and all. */
async function putBack(dealId) {
  try {
    const result = await rest('/deal/' + encodeURIComponent(dealId) + '/restore', { method: 'POST', body: {} })
    refresh()
    host.notify({ kind: 'success', message: result.name + ' is back on the desk.' })
  } catch (error) {
    host.notifyError(error, 'Stoa could not put that back')
  }
}

async function undo(dealId, op) {
  try {
    const result = await rest('/deal/' + encodeURIComponent(dealId) + '/undo', { method: 'POST', body: { op: op || '' } })
    $last.set(null)
    refresh()
    host.notify({ kind: 'info', message: 'Taken back: ' + result.undid + (result.removed ? '. ' + result.name + ' is off the desk.' : '') })
    return result
  } catch (error) {
    $last.set(null)
    refresh()
    host.notifyError(error, 'Not taken back')     // nothing left to undo, or it would overwrite a later change (the message says which)
    return null
  }
}

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
  if (onPage) {
    if ($talk.get().busy) {
      host.notify({ kind: 'info', message: 'Hermes is still on the last thing. Ask again when it has answered.' })
      return false
    }
    say(text)
    return true
  }
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
    if (result && result.last) offerUndo(result.last)
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
  + 'short lines: the page already shows the pipeline. What is said here is for the desk: do not also write it to another system '
  + '(a spreadsheet, another CRM, email) unless asked to.\n\n'

/** The name of the pipeline the page is showing, when the desk has more than one ('' otherwise). */
function onScreen() {
  const state = queryClient.getQueryData(STATE)
  const pipes = (state && state.pipelines) || []
  if (pipes.length < 2) return ''
  const found = pipes.find(item => item.id === state.pipeline)
  return found ? found.name : ''
}

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
  const typed = file ? shown + '\n\nAttached file "' + file.name + '":\n```\n' + file.text + '\n```' : shown
  const here = onScreen()
  const sent = here ? '(On the Stoa page I am looking at the pipeline “' + here + '”.)\n\n' + typed : typed
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
      const text = await file.text()
      const result = await rest('/intake', { method: 'POST', body: { name: file.name, text } })
      refresh()
      if (result.unread) { // the desk could not tell which column is the name: Hermes reads it, and asks if it cannot tell either
        if (text.length > 60000) return host.notify({ kind: 'info', message: 'Stoa could not tell which column of ' + file.name + ' holds the name. It is too long for the box here: press "Open the conversation" and drop it there.' })
        host.notify({ kind: 'info', message: result.headline })
        return say('Stoa could not tell which column of this list holds the name. Read it, work out which column is which, and put it on the desk as a list for my review. If you cannot tell, ask me.', { name: file.name, text })
      }
      host.notify({ kind: result.waiting ? 'success' : 'info', message: result.headline })
      const page = document.querySelector('.stoa-page')
      if (result.waiting) $tab.set('today') // where everything waiting for a yes or a no is
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

// -- look ------------------------------------------------------------------------------------------
const CSS = `
/* The look. Surfaces, words and lines are the app's theme (--ui-*), so every theme carries it.
   Labels, figures and anything you press are set in the app's mono face; sentences stay in its text face.
   Corners are square and lines are hairlines. A quantity is drawn as dots, never as a smooth bar.
   Heat (--stoa-h1 to h7) is the one set of colours Stoa brings: the ramp its artwork is drawn in, cold to hot. */
.stoa { font-size: 13px; line-height: 1.45; color: var(--ui-text-primary);
  --stoa-mono: var(--font-mono, "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace);
  --stoa-accent: var(--ui-accent);
  --stoa-on-accent: var(--dt-midground-foreground, #fff);
  --stoa-accent-text: color-mix(in oklab, var(--ui-accent) 58%, var(--ui-text-primary));
  --stoa-accent-soft: color-mix(in srgb, var(--ui-accent) 16%, transparent);
  --stoa-accent-line: color-mix(in srgb, var(--ui-accent) 58%, transparent);
  --stoa-h1: #2150cc; --stoa-h2: #2c86d9; --stoa-h3: #30b4c4; --stoa-h4: #48c78c; --stoa-h5: #f2b24c; --stoa-h6: #f4967e; --stoa-h7: #f3a9c0;
  --stoa-heat: linear-gradient(90deg, var(--stoa-h1), var(--stoa-h2), var(--stoa-h3), var(--stoa-h4), var(--stoa-h5), var(--stoa-h6), var(--stoa-h7));
  --stoa-bar: linear-gradient(90deg, var(--stoa-h1), var(--stoa-h2) 46%, var(--ui-text-primary));      /* the quiet one: blue to the colour of the words */
  --stoa-r: 2px;
  --stoa-page: var(--background, #0d1015);
  /* Two sets of stripes that cross: what is left is a grid of 2px dots, 4px apart. Used as a mask. */
  --stoa-grid: repeating-linear-gradient(90deg, #000 0 2px, transparent 2px 4px), repeating-linear-gradient(0deg, #000 0 2px, transparent 2px 4px); }
.stoa *, .stoa *::before, .stoa *::after { box-sizing: border-box; }
.stoa-pane { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.stoa-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 14px 12px 18px; }
.stoa-page { height: 100%; overflow: auto; padding: 24px 34px 0; }
.stoa-wrap { max-width: 1320px; margin: 0 auto; container-type: inline-size; }
.stoa-eyebrow { font-family: var(--stoa-mono); font-size: 10px; letter-spacing: .14em; text-transform: uppercase; color: var(--ui-text-tertiary); }
.stoa-figure { display: flex; align-items: flex-end; gap: 10px; margin-top: 6px; font-family: var(--stoa-mono); font-size: 30px; font-weight: 600; letter-spacing: -.02em; line-height: 1.1; font-variant-numeric: tabular-nums; }
.stoa-figure small { font-size: 11.5px; font-weight: 400; letter-spacing: .02em; color: var(--ui-text-tertiary); padding-bottom: 3px; }
.stoa-page .stoa-figure { font-size: 44px; }
.stoa-page .stoa-figure small { padding-bottom: 1px; }
/* The figure that matters, set in dots. */
.stoa-dotfig { display: block; height: 28px; width: auto; overflow: visible; }
.stoa-page .stoa-dotfig { height: 42px; }
.stoa-dotfig rect { fill: var(--ui-text-primary); }
/* The line under the header: one row of dots, blue running to white. */
.stoa-rule { height: 2px; margin: 14px 0 4px; background: var(--stoa-bar); -webkit-mask-image: repeating-linear-gradient(90deg, #000 0 2px, transparent 2px 4px); mask-image: repeating-linear-gradient(90deg, #000 0 2px, transparent 2px 4px); }
.stoa-sub { color: var(--ui-text-tertiary); font-size: 12px; }
.stoa-clip { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stoa-sec { margin-top: 16px; }
.stoa-sechead { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; padding: 0 8px 4px; }
/* A section opens with three shades of block, light to dark: the same dots, at the size of a letter. */
.stoa-sechead > .stoa-eyebrow:first-child::before, .stoa-rpsechead h3::before { content: "░▒▓"; margin-right: 8px; letter-spacing: 0; font-family: var(--stoa-mono); font-weight: 400;
  background: var(--stoa-bar); -webkit-background-clip: text; background-clip: text; color: transparent; }
.stoa-row { display: flex; gap: 8px; align-items: flex-start; width: 100%; padding: 6px 8px; border: 0; border-radius: var(--stoa-r); background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-row:hover { background: var(--ui-row-hover-background); }
.stoa-row[data-open="1"] { background: var(--stoa-accent-soft); box-shadow: inset 2px 0 0 var(--stoa-accent); }
.stoa-dot { flex: none; width: 6px; height: 6px; margin-top: 6px; background: var(--ui-text-quaternary); }
.stoa-dot[data-tone="bad"] { background: var(--ui-red); }
.stoa-dot[data-tone="warn"] { background: var(--ui-orange); }
.stoa-dot[data-tone="good"] { background: var(--stoa-accent); }
.stoa-grow { flex: 1; min-width: 0; }
.stoa-name { font-weight: 550; }
.stoa-num { flex: none; font-family: var(--stoa-mono); font-size: 11.5px; font-variant-numeric: tabular-nums; color: var(--ui-text-secondary); white-space: nowrap; }
.stoa-num[data-tone="bad"] { color: var(--ui-red); }
.stoa-num[data-tone="warn"] { color: var(--ui-orange); }
.stoa-box { padding: 12px 14px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: var(--ui-bg-card); }
.stoa-detail { margin: 2px 0 10px; }
.stoa-inchat { max-width: 460px; margin: 6px 0; }
/* Every bar is a row or two of dots: lit as far as it is filled. */
.stoa-meter, .stoa-weight, .stoa-rate u, .stoa-fc u, .stoa-billbar, .stoa-path > *::after { -webkit-mask-image: var(--stoa-grid); mask-image: var(--stoa-grid); -webkit-mask-composite: source-in; mask-composite: intersect; }
.stoa-meter { display: flex; margin: 8px 0 4px; }
.stoa-meter i { flex: 1; height: 4px; background: var(--ui-stroke-primary); }
/* Lit dots take their colour from the heat ramp: --pos is where along it this piece sits, --size how many pieces share it. */
.stoa-meter i[data-on="1"], .stoa-weight i, .stoa-path > [data-state="done"]::after, .stoa-path > [data-state="now"]::after, .stoa-path > [data-state="won"]::after { background: var(--stoa-heat); background-size: var(--size, 100%) 100%; background-position: var(--pos, 0) 0; }
.stoa-facts { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 14px; margin: 14px 0 2px; padding-top: 12px; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-facts span { display: block; overflow-wrap: anywhere; }
.stoa-next { margin-top: 10px; }
.stoa-log { margin-top: 10px; padding-top: 8px; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-log p { margin: 0 0 5px; font-size: 12px; color: var(--ui-text-secondary); }
.stoa-btns { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
.stoa-btn, .stoa-select { height: 28px; padding: 0 11px; border: 1px solid var(--ui-stroke-primary); border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-primary); font-family: var(--stoa-mono); font-size: 11.5px; cursor: pointer; }
.stoa-btn, .stoa-select { transition: background .15s ease, opacity .15s ease; }
.stoa-btn:hover, .stoa-select:hover { background: var(--ui-control-hover-background); }
.stoa-btn[data-kind="primary"]:hover { background: var(--stoa-accent); opacity: .88; }
.stoa-btn[data-kind="primary"] { border-color: var(--stoa-accent); background: var(--stoa-accent); color: var(--stoa-on-accent); font-weight: 600; }
.stoa-btn[data-kind="quiet"] { border-color: transparent; color: var(--ui-text-secondary); }
.stoa-btn:disabled, .stoa-select:disabled { opacity: .5; cursor: default; }
/* A tag is a word in square brackets, with no box round it. */
.stoa-chip { display: inline-block; font-family: var(--stoa-mono); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; color: var(--ui-text-tertiary); white-space: nowrap; }
.stoa-chip::before { content: "["; margin-right: 2px; opacity: .55; }
.stoa-chip::after { content: "]"; margin-left: 1px; opacity: .55; }
.stoa-chip[data-tone="bad"] { color: var(--ui-red); }
.stoa-chip[data-tone="warn"] { color: var(--ui-orange); }
.stoa-chip[data-tone="good"] { color: var(--stoa-accent-text); }
.stoa-chips { display: flex; flex-wrap: wrap; gap: 5px 9px; margin-top: 7px; }
.stoa-foot { display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 12px; border-top: 1px dotted var(--ui-stroke-primary); }
/* Tabs: a strip of cells. Where you are is shown the way a terminal shows it, light on dark turned round. */
.stoa-tabs { display: inline-flex; margin: 18px 0 18px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); }
.stoa-tab { display: inline-flex; align-items: center; gap: 8px; height: 30px; padding: 0 14px; border: 0; border-radius: 0; background: transparent; color: var(--ui-text-secondary); font-family: var(--stoa-mono); font-size: 12px; cursor: pointer; outline: none; transition: background .15s ease, color .15s ease; }
.stoa-tab + .stoa-tab { border-left: 1px solid var(--ui-stroke-tertiary); }
.stoa-tab:hover { color: var(--ui-text-primary); background: var(--ui-control-hover-background); }
.stoa-tab[data-on="1"], .stoa-tab[data-on="1"]:hover { background: var(--ui-text-primary); color: var(--stoa-page); font-weight: 600; }
.stoa-tab small { font-size: 10px; opacity: .6; }
.stoa-tab[data-on="1"] small { opacity: .75; }
.stoa button:focus-visible, .stoa select:focus-visible, .stoa input:focus-visible { outline: 2px solid var(--stoa-accent-line); outline-offset: 2px; }
.stoa-split { display: grid; grid-template-columns: minmax(0, 1fr); gap: 20px 36px; align-items: start; }
.stoa-split[data-side="1"] { grid-template-columns: minmax(0, 1fr) 340px; }
.stoa-split > .stoa-box { position: sticky; top: 0; max-height: calc(100vh - 235px); overflow-y: auto; }   /* it ends above the dock */
.stoa-board { display: grid; grid-auto-flow: column; grid-auto-columns: minmax(176px, 1fr); gap: 12px; overflow-x: auto; padding-bottom: 8px; }
.stoa-colhead { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; padding: 0 2px 8px; }
.stoa-card { display: block; width: 100%; margin-bottom: 8px; padding: 10px 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: var(--ui-bg-card); color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-card { transition: border-color .15s ease, transform .15s ease; }
.stoa-card:hover, .stoa-card[data-open="1"] { border-color: var(--ui-stroke-primary); }
.stoa-card:hover { transform: translateY(-1px); }
.stoa-card[data-open="1"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); }
.stoa-cardfoot { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; margin-top: 6px; font-size: 12px; }
.stoa-cardfoot em { flex: none; font-style: normal; font-family: var(--stoa-mono); font-size: 10.5px; color: var(--ui-text-tertiary); }
.stoa-card h5 { display: flex; justify-content: space-between; gap: 8px; margin: 0; font-size: 13px; font-weight: 550; }
.stoa-empty { padding: 10px 2px; color: var(--ui-text-quaternary); }
.stoa-cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; }
.stoa-company { padding: 10px 0; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-company:first-of-type { border-top: 0; }
/* Fit is heat: a better fit is a hotter mark. */
.stoa-fit { font-family: var(--stoa-mono); font-size: 9px; letter-spacing: 2px; color: var(--stoa-h4); white-space: nowrap; }
.stoa-fit[data-fit="1"] { color: var(--stoa-h2); } .stoa-fit[data-fit="2"] { color: var(--stoa-h3); } .stoa-fit[data-fit="4"] { color: var(--stoa-h5); } .stoa-fit[data-fit="5"] { color: var(--stoa-h6); }
.stoa-fit s { text-decoration: none; color: var(--ui-text-quaternary); opacity: .6; }
.stoa-rate { display: grid; grid-template-columns: minmax(0, 1fr) 92px 44px; gap: 10px; align-items: center; padding: 4px 0; }
.stoa-rate u { display: block; height: 4px; background: var(--ui-stroke-primary); overflow: hidden; }
.stoa-rate u i { display: block; height: 100%; background: var(--ui-text-primary); }
.stoa-reading { max-width: 70ch; margin: 8px 0 0; color: var(--ui-text-secondary); white-space: pre-line; }
.stoa-h2 { margin: 26px 0 8px; font-size: 15px; font-weight: 600; }
.stoa-filters { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.stoa-filters .stoa-input { flex: 1 1 120px; min-width: 0; height: 28px; padding: 0 9px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-primary); font: inherit; font-size: 12px; outline: none; }
.stoa-filters .stoa-input:focus { border-color: var(--ui-stroke-primary); }
.stoa-filters .stoa-input::placeholder { color: var(--ui-text-quaternary); }
.stoa-filters .stoa-select { max-width: 150px; }
.stoa-toggle { height: 28px; padding: 0 9px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-secondary); font-family: var(--stoa-mono); font-size: 11.5px; cursor: pointer; }
.stoa-toggle[data-on="1"] { border-color: var(--stoa-accent-line); color: var(--ui-text-primary); background: var(--stoa-accent-soft); }
/* The funnel: each stage a block of dots, cold where records come in and hot where they are won. */
.stoa-funnel { margin: 0 0 20px; padding: 16px 18px 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: var(--ui-bg-card); }
.stoa-funnel svg { display: block; width: 100%; height: auto; overflow: visible; }
.stoa-funnel text { font-family: var(--stoa-mono); }
.stoa-fn-block { fill: var(--stoa-h3); }
.stoa-fn-step[data-heat="1"] .stoa-fn-block { fill: var(--stoa-h1); } .stoa-fn-step[data-heat="2"] .stoa-fn-block { fill: var(--stoa-h2); }
.stoa-fn-step[data-heat="4"] .stoa-fn-block { fill: var(--stoa-h4); } .stoa-fn-step[data-heat="5"] .stoa-fn-block { fill: var(--stoa-h5); }
.stoa-fn-step[data-heat="6"] .stoa-fn-block { fill: var(--stoa-h6); }
/* Where a record is won is not one more colour on the ramp: it is white, and it glows. */
.stoa-fn-won { fill: var(--ui-text-primary); filter: drop-shadow(0 0 5px color-mix(in srgb, var(--ui-text-primary) 70%, transparent)) drop-shadow(0 0 16px color-mix(in srgb, var(--stoa-h7) 60%, transparent)); }
.stoa-fn-block, .stoa-fn-won { transform-box: fill-box; transform-origin: center; animation: stoa-rise .7s cubic-bezier(.2, .7, .2, 1) both; transition: filter .15s ease; }
.stoa-fn-won { animation: stoa-rise .7s cubic-bezier(.2, .7, .2, 1) both, stoa-glow 3.6s ease-in-out 1s infinite; }
.stoa-fn-step:last-child .stoa-fn-label, .stoa-fn-step:last-child .stoa-fn-num { fill: var(--ui-text-primary); }
.stoa-fn-link { fill: var(--ui-text-primary); opacity: .17; }
.stoa-fn-pill { fill: var(--stoa-page); stroke: var(--ui-stroke-secondary); }
.stoa-fn-hit { fill: transparent; }
.stoa-fn-step[data-press="1"] { cursor: pointer; }
.stoa-fn-step[data-press="1"]:hover .stoa-fn-block { filter: brightness(1.25); }
.stoa-fn-step[data-on="1"] .stoa-fn-label { fill: var(--ui-text-primary); font-weight: 700; }
.stoa-fn-step[data-dim="1"] .stoa-fn-block { opacity: .35; }
.stoa-fn-label { font-size: 9.5px; letter-spacing: .12em; fill: var(--ui-text-tertiary); }
.stoa-fn-num { font-size: 15px; font-weight: 600; fill: var(--ui-text-primary); }
.stoa-fn-pct { font-size: 10.5px; fill: var(--ui-text-primary); }
.stoa-fn-sub { font-size: 9px; letter-spacing: .06em; fill: var(--ui-text-tertiary); }
.stoa-funnel-foot { display: flex; flex-wrap: wrap; gap: 4px 18px; margin-top: 6px; font-size: 12px; color: var(--ui-text-tertiary); }
.stoa-funnel-foot b { font-family: var(--stoa-mono); color: var(--ui-text-primary); font-weight: 600; }
.stoa-list { max-height: 340px; overflow-y: auto; margin-top: 10px; }
.stoa-pick { display: flex; gap: 9px; align-items: flex-start; padding: 7px 0; border-top: 1px dotted var(--ui-stroke-primary); cursor: pointer; }
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
.stoa-dockzone { position: sticky; bottom: 0; z-index: 6; margin: 26px -34px 0; padding: 54px 34px 16px; background: linear-gradient(to top, var(--stoa-page) 46%, transparent); pointer-events: none; }
.stoa-dock { width: min(780px, 100%); margin: 0 auto; pointer-events: auto; }
.stoa-glass { border: 1px solid var(--ui-stroke-primary); border-radius: var(--stoa-r); background: color-mix(in srgb, var(--ui-bg-elevated, var(--background, #14161b)) 88%, transparent); -webkit-backdrop-filter: blur(18px) saturate(1.2); backdrop-filter: blur(18px) saturate(1.2); box-shadow: 0 18px 50px -14px rgba(0, 0, 0, .6), 0 2px 8px rgba(0, 0, 0, .22); }
.stoa-sheet { margin-bottom: 8px; padding: 10px 14px 14px; max-height: min(46vh, 430px); overflow-y: auto; animation: stoa-lift .2s ease both; }
.stoa-sheethead { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 8px; }
.stoa-turn + .stoa-turn { margin-top: 14px; padding-top: 14px; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-youline { display: flex; justify-content: flex-end; }
.stoa-you { max-width: 86%; padding: 6px 12px; border-radius: var(--stoa-r); background: var(--stoa-accent-soft); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--stoa-accent) 24%, transparent); overflow-wrap: anywhere; white-space: pre-wrap; }
.stoa-you small { display: block; margin-top: 2px; font-size: 11px; color: var(--ui-text-tertiary); }
.stoa-working { display: inline-flex; align-items: center; gap: 8px; }
.stoa-working::before { content: ""; width: 7px; height: 13px; background: var(--stoa-accent); animation: stoa-blink 1s steps(1) infinite; }
.stoa-composer { padding: 7px; transition: border-color .15s ease, box-shadow .15s ease; }
.stoa-composer:focus-within { border-color: var(--stoa-accent-line); box-shadow: 0 18px 50px -14px rgba(0, 0, 0, .6), 0 0 0 1px var(--stoa-accent-line); }
.stoa-composerrow { display: flex; align-items: flex-end; gap: 3px; }
/* The talk box is a prompt: a mark, then what you type, in the mono face. */
.stoa-prompt { flex: none; align-self: flex-start; padding: 6px 0 0 5px; font-family: var(--stoa-mono); font-size: 15px; font-weight: 700; line-height: 22px; color: var(--stoa-accent-text); user-select: none; }
.stoa-composer textarea { flex: 1; min-width: 0; min-height: 34px; max-height: 132px; padding: 8px 6px 6px; border: 0; background: transparent; color: var(--ui-text-primary); font-family: var(--stoa-mono); font-size: 13px; line-height: 1.5; resize: none; }
.stoa .stoa-composer textarea, .stoa .stoa-composer textarea:focus-visible { outline: none; }
.stoa-composer textarea::placeholder { color: var(--ui-text-quaternary); }
.stoa-icon { flex: none; display: inline-flex; align-items: center; justify-content: center; width: 34px; height: 34px; padding: 0; border: 0; border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-secondary); cursor: pointer; transition: background .15s ease, color .15s ease, opacity .15s ease; }
.stoa-icon:hover { background: var(--ui-control-hover-background); color: var(--ui-text-primary); }
.stoa-icon:disabled { opacity: .4; cursor: default; }
.stoa-icon[data-kind="send"] { margin-left: 3px; background: var(--stoa-accent); color: var(--stoa-on-accent); }
.stoa-icon[data-kind="send"]:hover { background: var(--stoa-accent); opacity: .88; }
.stoa-icon[data-kind="send"]:disabled { opacity: .32; }
.stoa-icon[data-on="1"] { color: var(--ui-red); background: color-mix(in srgb, var(--ui-red) 14%, transparent); animation: stoa-pulse 1.4s ease-in-out infinite; }
.stoa-filechip { display: inline-flex; align-items: center; gap: 6px; margin: 1px 0 6px 4px; padding: 2px 3px 2px 9px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); font-family: var(--stoa-mono); font-size: 11.5px; color: var(--ui-text-secondary); }
.stoa-hints { display: flex; flex-wrap: wrap; justify-content: center; gap: 6px; margin-bottom: 8px; }
.stoa-hint { height: 28px; padding: 0 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: color-mix(in srgb, var(--ui-bg-elevated, var(--background, #14161b)) 86%, transparent); -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); color: var(--ui-text-secondary); font-family: var(--stoa-mono); font-size: 11.5px; cursor: pointer; transition: color .15s ease, border-color .15s ease; }
.stoa-hint:hover { color: var(--ui-text-primary); border-color: var(--ui-stroke-primary); }
.stoa-hint:disabled { opacity: .5; cursor: default; }
.stoa-dropnote { position: sticky; top: 0; z-index: 7; width: fit-content; margin: 0 auto 12px; padding: 8px 18px; border: 1px dashed var(--stoa-accent-line); border-radius: var(--stoa-r); background: var(--stoa-accent-soft); -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); font-size: 13px; }
.stoa-page[data-drop="1"] { box-shadow: inset 0 0 0 2px var(--stoa-accent-line); }
/* The head of the page is two columns. The figure and its counts take the room they need; the picture (the stoa,
   or your name in its place) has what is left, so it is cropped or made smaller and never runs under the figures.
   Where too little is left, the picture goes: a name then sits above the figures instead. */
.stoa-head { position: relative; display: grid; grid-template-columns: minmax(0, max-content) minmax(0, 1fr); gap: 14px 44px; align-items: end; min-height: 124px; }
.stoa-head > * { position: relative; }
.stoa-headmain { grid-column: 1; grid-row: 1; min-width: 0; }
.stoa-headstats { grid-column: 1; grid-row: 2; }
.stoa-headart { grid-column: 2; grid-row: 1 / span 2; align-self: stretch; min-width: 0; pointer-events: none; -webkit-mask-image: linear-gradient(90deg, transparent, #000 90px); mask-image: linear-gradient(90deg, transparent, #000 90px); }
.stoa-headart > canvas { position: absolute; top: 0; left: 0; width: 100%; height: 100%; }
.stoa-art { display: block; width: 100%; height: 100%; }
.stoa-nameart { font-family: var(--stoa-mono); }
.stoa-headname { height: 56px; width: min(100%, 700px); margin: 0 0 12px -3px; }
.stoa-headname[data-narrow="1"] { display: none; }
.stoa-head[data-sign="art"] > .stoa-headart { -webkit-mask-image: none; mask-image: none; }
.stoa-head[data-sign="art"] > .stoa-headart > canvas { top: 4px; height: calc(100% - 8px); }
@container (max-width: 980px) {
  .stoa-head[data-sign="art"] > .stoa-headart { display: none; }
  .stoa-headname[data-narrow="1"] { display: block; }
}
@container (max-width: 860px) { .stoa-headart { display: none; } .stoa-head { min-height: 0; } }
.stoa-headstats { display: flex; flex-wrap: wrap; gap: 10px 32px; }
.stoa-headstats b { display: block; margin-top: 3px; font-family: var(--stoa-mono); font-size: 18px; font-weight: 600; line-height: 1.1; font-variant-numeric: tabular-nums; }
.stoa-tabsrow { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px; }
.stoa-col { min-width: 0; border-radius: var(--stoa-r); transition: opacity .2s ease, background .15s ease; }
.stoa-col[data-dim="1"] { opacity: .45; }
.stoa-col[data-dim="1"]:hover { opacity: .8; }
.stoa-col[data-spot="1"] .stoa-colhead .stoa-eyebrow { color: var(--ui-text-primary); }
.stoa-col[data-over="1"] { background: var(--stoa-accent-soft); box-shadow: inset 0 0 0 1px var(--stoa-accent-line); }   /* inset: the board clips anything drawn outside a column */
.stoa-weight { height: 4px; margin: 0 2px 9px; background: var(--ui-stroke-primary); }
.stoa-weight i { display: block; height: 100%; transition: width .4s ease; }
.stoa-weight[data-won="1"] i { background: var(--ui-text-primary); }
.stoa-col:has(.stoa-weight[data-won="1"]) .stoa-colhead .stoa-eyebrow { color: var(--ui-text-primary); }
.stoa-card[draggable="true"] { cursor: grab; }
.stoa-card[draggable="true"]:active { cursor: grabbing; }
.stoa-split > .stoa-box { animation: stoa-slide .22s ease both; }
@keyframes stoa-rise { from { transform: scaleY(.12); opacity: 0; } }
@keyframes stoa-lift { from { transform: translateY(8px); opacity: 0; } }
@keyframes stoa-slide { from { transform: translateX(10px); opacity: 0; } }
@keyframes stoa-blink { 50% { opacity: 0; } }
@keyframes stoa-glow { 50% { filter: drop-shadow(0 0 9px color-mix(in srgb, var(--ui-text-primary) 92%, transparent)) drop-shadow(0 0 28px color-mix(in srgb, var(--stoa-h7) 80%, transparent)); } }
@keyframes stoa-pulse { 50% { box-shadow: 0 0 0 5px color-mix(in srgb, var(--ui-red) 16%, transparent); } }
@media (prefers-reduced-motion: reduce) { .stoa *, .stoa *::before { animation: none !important; transition: none !important; } }
.stoa-form { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 10px; margin-top: 12px; }
.stoa-form label { display: block; min-width: 0; }
.stoa-form label[data-wide="1"] { grid-column: 1 / -1; }
.stoa-field { display: block; width: 100%; height: 28px; margin-top: 3px; padding: 0 8px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-primary); font: inherit; font-size: 12px; outline: none; }
.stoa-field:focus { border-color: var(--ui-stroke-primary); }
.stoa-field::placeholder { color: var(--ui-text-quaternary); }
.stoa-edit { margin-top: 12px; padding-top: 2px; border-top: 1px dotted var(--ui-stroke-primary); }
/* Sample data on show: said plainly, on every surface, with the way back beside it. */
.stoa-tour { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px 16px; margin: -6px 0 18px; padding: 8px 10px 8px 14px; border: 1px dashed var(--stoa-accent-line); border-radius: var(--stoa-r); background: var(--stoa-accent-soft); }
.stoa-tourline { margin-bottom: 10px; padding: 4px 8px; border: 1px dashed var(--stoa-accent-line); border-radius: var(--stoa-r); background: var(--stoa-accent-soft); font-size: 11px; }
.stoa-stat { padding: 0; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: default; }
.stoa-stat[data-press="1"] { cursor: pointer; }
.stoa-stat[data-press="1"]:hover b { color: var(--stoa-accent-text); }
/* Today: a greeting, then each kind of thing that wants you as a group that opens. Closed, a group is one calm line. */
.stoa-today { min-width: 0; }
.stoa-todayhead { margin: 6px 0 20px; }
.stoa-hello { margin: 0; font-size: 22px; font-weight: 600; letter-spacing: -.015em; line-height: 1.2; }
.stoa-todayhead .stoa-sub { margin: 6px 0 0; font-size: 13px; }
.stoa-folds { border-bottom: 1px dotted var(--ui-stroke-primary); }
.stoa-folds:empty { display: none; }
.stoa-fold { border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-foldhead { display: flex; align-items: center; gap: 10px; }
.stoa-foldmain { flex: 1; min-width: 0; display: grid; grid-template-columns: auto 28px 196px minmax(0, 1fr); gap: 0 12px; align-items: baseline; padding: 16px 8px 15px; border: 0; border-radius: var(--stoa-r); background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; transition: background .15s ease; }
.stoa-foldmain:hover { background: var(--ui-row-hover-background); }
.stoa-foldmark { font-family: var(--stoa-mono); font-size: 11px; color: var(--ui-text-tertiary); }
.stoa-foldmain:hover .stoa-foldmark, .stoa-fold[data-open="1"] .stoa-foldmark { color: var(--ui-text-primary); }
.stoa-foldcount { font-family: var(--stoa-mono); font-size: 15px; font-weight: 600; text-align: right; font-variant-numeric: tabular-nums; color: var(--ui-text-secondary); }
.stoa-foldcount[data-tone="bad"] { color: var(--ui-red); }
.stoa-foldcount[data-tone="warn"] { color: var(--ui-orange); }
.stoa-foldcount[data-tone="good"] { color: var(--stoa-accent-text); }
.stoa-foldtitle { font-size: 14px; font-weight: 600; white-space: nowrap; }
.stoa-foldsum { padding-left: 6px; font-size: 12.5px; color: var(--ui-text-tertiary); }
.stoa-foldaside { flex: none; padding-right: 6px; }
.stoa-foldbody { padding: 0 0 18px 44px; animation: stoa-lift .16s ease both; }
.stoa-foldbody > .stoa-trow:first-child, .stoa-foldbody > .stoa-irow:first-child { border-top: 0; }
.stoa-foldbody > .stoa-proposal:last-child { margin-bottom: 0; }
@container (max-width: 760px) { .stoa-foldmain { grid-template-columns: auto 28px minmax(0, 1fr); } .stoa-foldsum { display: none; } .stoa-foldbody { padding-left: 8px; } }
.stoa-trow { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 2px 12px; align-items: center; padding: 2px 6px 2px 0; border-top: 1px dotted var(--ui-stroke-primary); border-radius: 0; transition: background .15s ease; }
.stoa-trow[data-open="1"] { background: var(--stoa-accent-soft); box-shadow: inset 2px 0 0 var(--stoa-accent); }
.stoa-tmain { display: flex; gap: 9px; align-items: flex-start; min-width: 0; padding: 7px 8px; border: 0; border-radius: var(--stoa-r); background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-tmain:hover { background: var(--ui-row-hover-background); }
.stoa-tacts { display: flex; gap: 6px; align-items: center; justify-content: flex-end; }
.stoa-tacts:empty { display: none; }
.stoa-nextform { grid-column: 1 / -1; display: grid; grid-template-columns: minmax(0, 1fr) 150px auto auto; gap: 6px 8px; align-items: center; padding: 2px 8px 10px 23px; animation: stoa-lift .18s ease both; }
.stoa-nextform .stoa-field, .stoa-pickdate .stoa-field { margin-top: 0; }
.stoa-box .stoa-nextform { grid-template-columns: minmax(0, 1fr) 132px; padding: 12px 0 0; }
.stoa-box .stoa-nextform .stoa-btn { justify-self: start; }
.stoa-pickdate { display: inline-flex; gap: 4px; align-items: center; }
.stoa-pickdate .stoa-field { width: 138px; }
.stoa-pace { margin: -4px 2px 9px; font-family: var(--stoa-mono); font-size: 10px; color: var(--ui-text-quaternary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
/* Cash: the strip, the forecast, what has been billed, and invoices */
.stoa-cash { min-width: 0; }
.stoa-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(168px, 1fr)); gap: 12px; }
.stoa-tile { padding: 12px 14px 11px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: var(--ui-bg-card); }
.stoa-tile b { display: block; margin: 5px 0 1px; font-family: var(--stoa-mono); font-size: 22px; font-weight: 600; letter-spacing: -.015em; line-height: 1.1; font-variant-numeric: tabular-nums; }
.stoa-tile[data-tone="bad"] b { color: var(--ui-red); }
.stoa-tile[data-tone="warn"] b { color: var(--ui-orange); }
.stoa-tile[data-tone="good"] b { color: var(--stoa-accent-text); }
.stoa-fc { display: grid; grid-template-columns: 120px minmax(0, 1fr) 200px; gap: 12px; align-items: center; padding: 5px 0; }
.stoa-fc u { position: relative; display: block; height: 8px; background: var(--ui-stroke-primary); text-decoration: none; overflow: hidden; }
.stoa-fc u i, .stoa-fc u b { position: absolute; top: 0; bottom: 0; left: 0; transition: width .4s ease; }
.stoa-fc u i { background: color-mix(in srgb, var(--ui-text-primary) 46%, transparent); }
.stoa-fc u b { background: var(--stoa-accent); }
.stoa-fc[data-key="past"] > span:first-child { color: var(--ui-orange); }
.stoa-fc[data-key="none"] u b { background: color-mix(in srgb, var(--stoa-accent) 55%, transparent); }
.stoa-fc .stoa-num { text-align: right; }
.stoa-billbar { position: relative; height: 4px; margin: 8px 0 2px; background: var(--ui-stroke-primary); overflow: hidden; }
.stoa-billbar i { position: absolute; top: 0; bottom: 0; left: 0; transition: width .4s ease; }
.stoa-billbar i[data-kind="invoiced"] { background: color-mix(in srgb, var(--ui-text-primary) 46%, transparent); }
.stoa-billbar i[data-kind="paid"] { background: var(--stoa-accent); }
.stoa-irow { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; padding: 8px 0; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-irow > .stoa-grow { flex: 1 1 200px; }      /* in a narrow panel the moves drop to a second line; the words are never squeezed */
.stoa-irow > .stoa-tacts { margin-left: auto; }
.stoa-irow[data-state="void"] { opacity: .5; }
.stoa-log > .stoa-irow:first-of-type { border-top: 0; }
/* Approvals, undo, people and companies */
.stoa-proposal { margin-bottom: 10px; padding: 11px 14px 12px; border: 1px solid var(--ui-stroke-secondary); border-left: 2px solid var(--stoa-accent); border-radius: var(--stoa-r); background: var(--ui-bg-card); animation: stoa-lift .2s ease both; }
.stoa-prophead { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
.stoa-proplink { padding: 0; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-proplink.stoa-sub { color: var(--ui-text-tertiary); font-size: 12px; }
.stoa-proplink.stoa-name { font-weight: 600; }
.stoa-proplink:hover { color: var(--stoa-accent-text); }
.stoa-propchange { margin-top: 5px; font-size: 14px; font-weight: 550; overflow-wrap: anywhere; }
.stoa-evidence { margin: 9px 0 0; padding: 2px 0 2px 11px; border-left: 2px dotted var(--ui-text-tertiary); color: var(--ui-text-secondary); }
.stoa-evidence small { display: block; margin-bottom: 2px; }
.stoa-evidence span { display: block; overflow-wrap: anywhere; }
.stoa-undobar { display: flex; align-items: center; gap: 10px; width: fit-content; max-width: 100%; margin: 0 auto 8px; padding: 4px 5px 4px 13px; border: 1px solid var(--ui-stroke-primary); border-radius: var(--stoa-r); background: color-mix(in srgb, var(--ui-bg-elevated, var(--background, #14161b)) 92%, transparent); -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); box-shadow: 0 12px 34px -14px rgba(0, 0, 0, .65); font-size: 12.5px; animation: stoa-lift .2s ease both; }
.stoa-undobar b { font-weight: 600; }
.stoa-undobar .stoa-btn { height: 25px; border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); font-weight: 600; }
.stoa-person { display: flex; gap: 6px; align-items: center; padding: 5px 0; }
.stoa-notes { height: auto; min-height: 62px; margin-top: 5px; padding: 7px 8px; line-height: 1.45; resize: vertical; }
.stoa-waiting { display: block; width: fit-content; margin: -6px 0 16px; padding: 6px 13px; border: 1px solid var(--stoa-accent-line); border-radius: var(--stoa-r); background: var(--stoa-accent-soft); color: var(--ui-text-primary); font: inherit; font-size: 12.5px; cursor: pointer; }
.stoa-waiting b { font-family: var(--stoa-mono); font-weight: 600; }
.stoa-viewrow { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px; margin: 0 0 14px; }
.stoa-companies { min-width: 0; }
.stoa-crow { display: grid; grid-template-columns: minmax(0, 2.2fr) minmax(0, 1.1fr) minmax(0, 1.1fr) 64px 84px; gap: 12px; align-items: center; width: 100%; padding: 9px 10px; border: 0; border-top: 1px dotted var(--ui-stroke-primary); background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-crow[data-cols="7"] { grid-template-columns: minmax(0, 2fr) minmax(0, 1.2fr) 76px minmax(0, 1fr) minmax(0, 1fr) 52px 80px; }
.stoa-crow:hover { background: var(--ui-row-hover-background); }
.stoa-crow[data-dim="1"] > * { opacity: .55; }
.stoa-newgroup { display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-end; margin: 0 0 12px; max-width: 760px; }
.stoa-pipes { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 14px; }
.stoa-pipe { display: inline-flex; align-items: center; gap: 8px; height: 28px; padding: 0 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-secondary); font-family: var(--stoa-mono); font-size: 11.5px; cursor: pointer; }
.stoa-pipe:hover { border-color: var(--ui-stroke-primary); color: var(--ui-text-primary); }
.stoa-pipe[data-on="1"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); color: var(--ui-text-primary); font-weight: 600; }
.stoa-pipe small { font-size: 10.5px; opacity: .7; font-variant-numeric: tabular-nums; }
.stoa-piperow { display: flex; gap: 10px; align-items: center; padding: 8px 0; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-sechead + .stoa-piperow { border-top: 0; padding-top: 0; }
.stoa-newgroup > label { flex: 1 1 200px; }
.stoa-kinds { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 10px; }
.stoa-proposal[data-follow="1"] { margin-top: -6px; margin-left: 14px; }      /* another suggestion about the record above it */
.stoa-crow[data-open="1"] { background: var(--stoa-accent-soft); box-shadow: inset 2px 0 0 var(--stoa-accent); }
.stoa-crow .stoa-num { text-align: left; }
.stoa-chead, .stoa-chead:hover { border-top: 0; background: transparent; cursor: default; padding-bottom: 6px; }
.stoa-level { display: flex; gap: 10px; align-items: flex-start; width: 100%; margin-bottom: 6px; padding: 9px 11px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; transition: border-color .15s ease, background .15s ease; }
.stoa-level:hover { border-color: var(--ui-stroke-primary); }
.stoa-level[data-on="1"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); cursor: default; }
.stoa-btn[data-kind="go"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); font-weight: 600; }
.stoa-btn[data-kind="go"]:hover { background: color-mix(in srgb, var(--ui-accent) 30%, transparent); }
.stoa-tacts .stoa-btn:not([data-kind="go"]), .stoa-tacts .stoa-select { border-color: var(--ui-stroke-secondary); color: var(--ui-text-secondary); }
.stoa-tacts .stoa-btn:not([data-kind="go"]):hover, .stoa-tacts .stoa-select:hover { color: var(--ui-text-primary); }
.stoa-tacts .stoa-select { max-width: 112px; }
.stoa-ahead .stoa-row { align-items: center; }
.stoa-aheadrow { display: flex; align-items: center; gap: 2px; margin: 0 -6px; }
.stoa-aheadrow .stoa-row { flex: 1; min-width: 0; }
.stoa-weekline { display: flex; flex-wrap: wrap; gap: 4px 18px; margin-top: 5px; color: var(--ui-text-secondary); }
.stoa-weekline b { font-family: var(--stoa-mono); font-size: 15px; font-weight: 600; color: var(--ui-text-primary); font-variant-numeric: tabular-nums; }
/* The way in, and Setup */
.stoa-welcome { max-width: 1040px; }
.stoa-hero { position: relative; display: flex; flex-direction: column; justify-content: flex-end; min-height: 300px; margin: 2px 0 18px; padding: 0 0 6px; }
.stoa-hero > * { position: relative; }
.stoa-heroart { position: absolute; inset: 0; pointer-events: none; -webkit-mask-image: linear-gradient(90deg, transparent 34%, #000 58%); mask-image: linear-gradient(90deg, transparent 34%, #000 58%); }
@container (max-width: 760px) { .stoa-heroart { display: none; } .stoa-hero { min-height: 0; } }
.stoa-herotitle { max-width: 15ch; margin: 8px 0 8px; font-size: 34px; font-weight: 600; letter-spacing: -.02em; line-height: 1.08; }
.stoa-hero .stoa-reading { max-width: 40ch; }
.stoa-modal { position: fixed; inset: 0; z-index: 50; display: flex; align-items: center; justify-content: center; padding: 28px 20px; background: color-mix(in srgb, var(--background, #0d1015) 62%, transparent); -webkit-backdrop-filter: blur(3px); backdrop-filter: blur(3px); animation: stoa-lift .16s ease both; }
.stoa-modalpanel { width: min(980px, 100%); max-height: calc(100vh - 56px); overflow-y: auto; padding: 20px 22px 0; border: 1px solid var(--ui-stroke-primary); border-radius: var(--stoa-r); background: var(--ui-bg-elevated, var(--background, #14161b)); box-shadow: 0 30px 80px -20px rgba(0, 0, 0, .55); }
.stoa-modalhead { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 12px; }
.stoa-modaltitle { margin-top: 4px; font-size: 19px; font-weight: 600; letter-spacing: -.01em; }
.stoa-modalsec { margin-top: 18px; }
.stoa-modalfoot { position: sticky; bottom: 0; display: flex; gap: 8px; align-items: center; margin: 18px -22px 0; padding: 12px 22px; border-top: 1px dotted var(--ui-stroke-primary); background: var(--ui-bg-elevated, var(--background, #14161b)); }
.stoa-describe summary { cursor: pointer; color: var(--ui-text-secondary); }
.stoa-describe[open] summary { margin-bottom: 8px; }
.stoa-warn { margin: 8px 0; padding: 8px 10px; border: 1px dashed var(--ui-orange); border-radius: var(--stoa-r); color: var(--ui-text-primary); font-size: 12.5px; }
.stoa-shapes { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
.stoa-shape { display: flex; flex-direction: column; gap: 2px; padding: 14px 16px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: var(--ui-bg-card); color: inherit; font: inherit; text-align: left; cursor: pointer; transition: border-color .15s ease, transform .15s ease, background .15s ease; }
.stoa-shape:hover { border-color: var(--stoa-accent-line); transform: translateY(-1px); }
.stoa-shape[data-on="1"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); }
.stoa-shapehead { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.stoa-shapetitle { font-size: 16px; font-weight: 600; letter-spacing: -.005em; }
.stoa-uses { margin: 8px 0 6px; padding-left: 0; list-style: none; color: var(--ui-text-secondary); font-size: 12.5px; line-height: 1.6; }
.stoa-uses li::before { content: "·"; display: inline-block; width: 14px; font-family: var(--stoa-mono); font-weight: 700; color: var(--ui-text-tertiary); }
.stoa-stagelist { list-style: none; margin: 0; padding: 0; max-width: 560px; }
.stoa-stagelist li { display: flex; gap: 4px; align-items: center; margin-bottom: 6px; }
.stoa-stagelist .stoa-field { margin-top: 0; height: 32px; font-size: 13px; }
.stoa-stagelist .stoa-icon { width: 30px; height: 30px; }
.stoa-stagenum { flex: none; width: 22px; font-family: var(--stoa-mono); font-size: 11px; color: var(--ui-text-tertiary); text-align: right; margin-right: 4px; }
.stoa-doors { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; margin-top: 14px; }
.stoa-door { display: flex; flex-direction: column; gap: 3px; transition: border-color .15s ease, transform .15s ease; }
.stoa-door:hover { border-color: var(--ui-stroke-primary); transform: translateY(-1px); }
.stoa-door[data-on="1"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); }
.stoa-door .stoa-btns { margin-top: auto; padding-top: 10px; }
.stoa-chain { margin: 8px 0 6px; font-family: var(--stoa-mono); font-size: 10.5px; letter-spacing: .02em; line-height: 1.7; color: var(--ui-text-secondary); }
.stoa-setup { columns: 2 380px; column-gap: 14px; max-width: 1120px; }
.stoa-setbox { break-inside: avoid; padding-bottom: 14px; }      /* padding, not a margin: a margin at the foot of a column is dropped, and boxes touched */
.stoa-setbox[data-wide="1"] { column-span: all; }
.stoa-check, .stoa-setrow { display: flex; gap: 10px; align-items: flex-start; padding: 9px 0; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-sechead + .stoa-check, .stoa-sechead + .stoa-setrow { border-top: 0; padding-top: 0; }
.stoa-check:last-child, .stoa-setrow:last-child { padding-bottom: 0; }
/* A step done or not: a box with a mark in it, the way a checklist is typed. */
.stoa-tick { flex: none; display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 16px; margin-top: 2px; border: 1px solid var(--ui-stroke-primary); font-family: var(--stoa-mono); font-style: normal; font-size: 11px; font-weight: 700; line-height: 1; }
.stoa-check[data-done="1"] .stoa-tick { border-color: var(--stoa-accent); background: var(--stoa-accent); color: var(--stoa-on-accent); }
.stoa-check[data-done="1"] .stoa-name { font-weight: 400; color: var(--ui-text-secondary); }
.stoa-lamp { flex: none; width: 8px; height: 8px; margin-top: 5px; border: 1px solid var(--ui-text-quaternary); }
.stoa-lamp[data-on="1"] { border-color: var(--stoa-accent); background: var(--stoa-accent); box-shadow: 0 0 0 3px var(--stoa-accent-soft); }
.stoa-sayline { display: block; width: 100%; margin: 0 0 3px; padding: 5px 8px; border: 0; border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-secondary); font: inherit; text-align: left; cursor: pointer; }
.stoa-sayline::before { content: "›"; margin-right: 8px; font-family: var(--stoa-mono); font-weight: 700; color: var(--stoa-accent-text); }
.stoa-sayline:hover { background: var(--ui-row-hover-background); color: var(--ui-text-primary); }
.stoa-tip { margin: 0 0 7px; color: var(--ui-text-secondary); }
.stoa-tip code { padding: 1px 5px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); font-family: var(--stoa-mono); font-size: 11px; color: var(--ui-text-primary); white-space: nowrap; }
.stoa-tip b { font-weight: 550; color: var(--ui-text-primary); }
/* A record, a group or a company on a page of its own: a header, the stages, the next step, then details | activity | related */
.stoa-rp { container-type: inline-size; padding-bottom: 8px; animation: stoa-lift .18s ease both; }
.stoa-backrow { margin: -4px 0 12px; }
.stoa-back { display: inline-flex; align-items: center; gap: 4px; height: 26px; padding: 0 10px 0 6px; border: 0; border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-secondary); font-family: var(--stoa-mono); font-size: 11.5px; cursor: pointer; }
.stoa-back:hover { background: var(--ui-control-hover-background); color: var(--ui-text-primary); }
.stoa-back span { font-size: 15px; line-height: 1; }
.stoa-rphead { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: flex-end; gap: 12px 32px; }
.stoa-rptitle { margin: 3px 0 0; font-size: 26px; font-weight: 600; letter-spacing: -.015em; line-height: 1.15; overflow-wrap: anywhere; }
.stoa-rpwhere { margin-top: 5px; font-size: 13px; color: var(--ui-text-secondary); }
.stoa-rpwhere .stoa-proplink { color: var(--ui-text-primary); font-weight: 500; }
.stoa-rpsep { margin: 0 8px; color: var(--ui-text-quaternary); }
.stoa-rpwhere .stoa-proplink:hover { color: var(--stoa-accent-text); }
.stoa-rpfig { text-align: right; }
.stoa-rpfig b { display: block; font-family: var(--stoa-mono); font-size: 24px; font-weight: 600; letter-spacing: -.015em; line-height: 1.1; font-variant-numeric: tabular-nums; }
.stoa-rpstats { display: flex; flex-wrap: wrap; gap: 10px 30px; padding-bottom: 3px; }
.stoa-rpstats b { display: block; margin-top: 3px; font-family: var(--stoa-mono); font-size: 16px; font-weight: 600; font-variant-numeric: tabular-nums; }
/* The stages: a strip of cells like the tabs, with a line of dots beneath that heats up as the record moves along. */
.stoa-path { display: flex; margin: 16px 0 9px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); }
.stoa-path > * { position: relative; flex: 1 1 0; min-width: 0; height: 30px; padding: 0 10px; border: 0; background: transparent; color: var(--ui-text-tertiary); font-family: var(--stoa-mono); font-size: 11px; display: flex; align-items: center; justify-content: center; }
.stoa-path > * + * { border-left: 1px solid var(--ui-stroke-tertiary); }
.stoa-path > * > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stoa-path button { cursor: pointer; transition: background .15s ease, color .15s ease; }
.stoa-path button:not(:disabled):hover { background: var(--ui-control-hover-background); color: var(--ui-text-primary); }
.stoa-path button:disabled { cursor: default; }
.stoa-path [data-state="done"] { color: var(--ui-text-secondary); }
.stoa-path > *::after { content: ""; position: absolute; left: 0; right: 0; bottom: -7px; height: 4px; background: var(--ui-stroke-primary); }
.stoa-path [data-state="now"] { background: var(--ui-text-primary); color: var(--stoa-page); font-weight: 600; }
.stoa-path [data-state="won"] { background: var(--ui-text-primary); color: var(--stoa-page); font-weight: 600; box-shadow: 0 0 14px color-mix(in srgb, var(--ui-text-primary) 50%, transparent), 0 0 30px color-mix(in srgb, var(--stoa-h7) 40%, transparent); }
.stoa-path > .stoa-pathend[data-state="won"]::after { background: var(--ui-text-primary); }
.stoa-path [data-state="lost"] { background: color-mix(in srgb, var(--ui-red) 22%, transparent); color: var(--ui-text-primary); font-weight: 550; }
.stoa-pathend { flex: 0 1 auto !important; min-width: 84px !important; }
.stoa-rpbar { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px 20px; margin-top: 12px; padding: 12px 14px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: var(--ui-bg-card); }
.stoa-rpbar[data-plain="1"] { margin-top: 14px; padding: 0; border: 0; background: transparent; }
.stoa-rpnext { min-width: 0; flex: 1 1 260px; }
.stoa-rpnextline { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; margin-top: 3px; font-size: 14px; }
.stoa-rpgrid { display: grid; grid-template-columns: minmax(250px, 300px) minmax(0, 1fr) minmax(260px, 330px); gap: 14px; align-items: start; margin-top: 14px; }
@container (max-width: 1080px) { .stoa-rpgrid { grid-template-columns: minmax(240px, 290px) minmax(0, 1fr); } .stoa-rpgrid > :nth-child(3) { grid-column: 1 / -1; display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; align-items: start; } }
@container (max-width: 700px) { .stoa-rpgrid { grid-template-columns: minmax(0, 1fr); } .stoa-rpgrid > :nth-child(3) { display: flex; } }
.stoa-rpcol { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.stoa-rpsec { min-width: 0; }
.stoa-rpsec > .stoa-log:first-child { margin-top: 0; padding-top: 0; border-top: 0; }
.stoa-rpsec > .stoa-log:first-child > .stoa-sechead { margin: -2px 0 10px; padding: 0; }
.stoa-rpsechead { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin: -2px 0 10px; }
.stoa-rpsechead h3 { margin: 0; font-family: var(--stoa-mono); font-size: 10px; font-weight: 400; letter-spacing: .14em; text-transform: uppercase; color: var(--ui-text-tertiary); }
.stoa-rpsechead .stoa-btn { height: 24px; }
.stoa-dl { display: grid; grid-template-columns: minmax(0, 1fr); margin: 0; }
.stoa-dl dt { margin-top: 9px; font-family: var(--stoa-mono); font-size: 9.5px; letter-spacing: .12em; text-transform: uppercase; color: var(--ui-text-tertiary); }
.stoa-dl dt:first-child { margin-top: 0; }
.stoa-dl dd { margin: 2px 0 0; overflow-wrap: anywhere; }
.stoa-dl dd[data-empty="1"] { color: var(--ui-text-quaternary); }
.stoa-alink { color: var(--ui-text-primary); text-decoration: none; border-bottom: 1px dotted var(--ui-text-tertiary); }
.stoa-alink:hover { color: var(--stoa-accent-text); border-bottom-color: var(--stoa-accent-line); }
.stoa-form[data-one="1"] { grid-template-columns: minmax(0, 1fr); }
.stoa-logbox { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 6px; align-items: center; margin-bottom: 6px; }
.stoa-logbox .stoa-field { margin-top: 0; }
.stoa-timeline { list-style: none; margin: 0; padding: 0; }
.stoa-tlday { margin: 14px 0 4px; font-family: var(--stoa-mono); font-size: 10px; letter-spacing: .12em; text-transform: uppercase; color: var(--ui-text-tertiary); }
.stoa-tl { display: flex; gap: 10px; align-items: flex-start; padding: 7px 0; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-tlday + .stoa-tl { border-top: 0; }
.stoa-tlkind { flex: none; width: 66px; padding: 2px 0; font-family: var(--stoa-mono); font-size: 10px; letter-spacing: .06em; text-transform: uppercase; color: var(--ui-text-secondary); }
.stoa-tl[data-kind="stage"] .stoa-tlkind, .stoa-tl[data-kind="created"] .stoa-tlkind { color: var(--stoa-accent-text); }
.stoa-tl[data-kind="updated"] .stoa-tlkind { color: var(--ui-text-tertiary); }
.stoa-tl[data-undone="1"] .stoa-tltext { text-decoration: line-through; opacity: .55; }
.stoa-tltext { overflow-wrap: anywhere; white-space: pre-line; }
.stoa-rlist { display: flex; flex-direction: column; margin: 4px -8px 0; }
.stoa-rrow { display: flex; gap: 8px; align-items: center; width: 100%; padding: 6px 8px; border: 0; border-radius: var(--stoa-r); background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-rrow:hover { background: var(--ui-row-hover-background); }
.stoa-rrow[data-closed="1"] { opacity: .6; }
.stoa-stagegroup + .stoa-stagegroup { margin-top: 12px; }
.stoa-billform { flex-basis: 100%; display: grid; grid-template-columns: minmax(110px, 150px) minmax(110px, 150px) auto auto; gap: 8px 12px; align-items: end; padding: 6px 0 4px; animation: stoa-lift .18s ease both; }
.stoa-billform label .stoa-field { margin-top: 3px; }
.stoa-placed { margin-top: 10px; padding-top: 8px; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-placerow { display: flex; gap: 10px; align-items: center; padding: 4px 0; }
.stoa-placerow .stoa-select { flex: none; width: 140px; }
.stoa-billtotal b { display: block; font-family: var(--stoa-mono); font-size: 16px; font-weight: 600; font-variant-numeric: tabular-nums; }
.stoa-crow[data-cols="6"] { grid-template-columns: minmax(0, 1.6fr) 84px minmax(0, 1fr) 70px minmax(0, 1.3fr) 96px; }
.stoa-stagehead { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; padding-bottom: 2px; border-bottom: 1px dotted var(--ui-stroke-primary); }
`

const Style = () => h('style', null, CSS)
const Chip = ({ tone, children }) => h('span', { className: 'stoa-chip', 'data-tone': tone || '' }, children)
const Btn = ({ kind, children, ...props }) => h('button', { type: 'button', className: 'stoa-btn', 'data-kind': kind || '', ...props }, children)

function Fit({ value }) {
  const n = Math.max(0, Math.min(5, Number(value) || 0))
  return h('span', { className: 'stoa-fit', 'data-fit': n, title: 'Fit ' + n + ' of 5' }, '■'.repeat(n), h('s', null, '■'.repeat(5 - n)))
}

// -- drawn in dots ---------------------------------------------------------------------------------
/** Where a piece sits on the heat ramp: place `index` of `count`, cold first. Read by the style block as --pos and --size. */
const heatAt = (index, count) => ({ '--size': Math.max(1, count) * 100 + '%', '--pos': (count > 1 ? (100 * index) / (count - 1) : 0) + '%' })

// The figure at the head of the desk is set in dots, five across and seven down, the way a sign board spells it.
// Each row is the five dots read as a number (16 is the left-hand dot); a narrow mark has two dots a row.
const DOTS = {
  0: [14, 17, 19, 21, 25, 17, 14], 1: [4, 12, 4, 4, 4, 4, 14], 2: [14, 17, 1, 2, 4, 8, 31], 3: [31, 2, 4, 2, 1, 17, 14], 4: [2, 6, 10, 18, 31, 2, 2],
  5: [31, 16, 30, 1, 1, 17, 14], 6: [6, 8, 16, 30, 17, 17, 14], 7: [31, 1, 2, 4, 8, 8, 8], 8: [14, 17, 17, 14, 17, 17, 14], 9: [14, 17, 17, 15, 1, 2, 12],
  $: [4, 15, 20, 14, 5, 30, 4], '€': [6, 9, 28, 8, 28, 9, 6], '£': [6, 9, 8, 28, 8, 8, 31], k: [16, 16, 18, 20, 24, 20, 18], M: [17, 27, 21, 21, 17, 17, 17],
  '.': [0, 0, 0, 0, 0, 3, 3], ',': [0, 0, 0, 0, 3, 1, 2], ' ': null
}

function DotFigure({ text }) {
  const chars = [...String(text)]
  if (!chars.length || chars.some(ch => !(ch in DOTS))) return String(text)      // what it cannot spell is set in type
  const marks = []
  let x = 0
  chars.forEach((ch, at) => {
    const wide = DOTS[ch] && DOTS[ch].every(row => row < 4) ? 2 : 5
    ;(DOTS[ch] || []).forEach((row, y) => {
      for (let col = 0; col < wide; col++) {
        if ((row >> (wide - 1 - col)) & 1) marks.push(h('rect', { key: at + ':' + y + ':' + col, x: x + col + 0.1, y: y + 0.1, width: 0.8, height: 0.8 }))
      }
    })
    x += (DOTS[ch] ? wide : 2) + 1
  })
  return h('svg', { className: 'stoa-dotfig', viewBox: '0 0 ' + (x - 1) + ' 7', style: { aspectRatio: (x - 1) + ' / 7' }, role: 'img', 'aria-label': String(text) }, marks)
}

// The stoa itself. The plugin carries it as a grid of cells, each empty or a brightness from 1 to 15, and it is
// drawn here dot by dot on the screen's own pixels: sharp at any size, in the heat colours as the theme has
// them. On a light theme the brightest dots turn to ink, so the outlines read as a drawing on paper.
const HEAT_AT = [0.33, 0.47, 0.59, 0.675, 0.78, 0.85, 0.925]      // the brightness at which each heat colour sits

function unpack(piece) {
  const raw = atob(piece.cells), cells = new Uint8Array(piece.w * piece.h)
  for (let i = 0; i < cells.length; i++) { const byte = raw.charCodeAt(i >> 1); cells[i] = i & 1 ? byte & 15 : byte >> 4 }
  return cells
}

/** The fifteen brightnesses as colours, from the theme's heat ramp. `inks[0]` is unused: 0 is no dot. */
function inksOf(el) {
  const style = getComputedStyle(el)
  // Any colour the theme can name, as red, green and blue: painted on one pixel and read back.
  const probe = document.createElement('canvas').getContext('2d', { willReadFrequently: true })
  probe.canvas.width = probe.canvas.height = 1
  const see = colour => { probe.clearRect(0, 0, 1, 1); probe.fillStyle = colour; probe.fillRect(0, 0, 1, 1); return probe.getImageData(0, 0, 1, 1).data }
  const mix = (from, to, k) => [0, 1, 2].map(i => from[i] + (to[i] - from[i]) * k)
  const stops = [1, 2, 3, 4, 5, 6, 7].map(n => see(style.getPropertyValue('--stoa-h' + n).trim()))
  const words = see(style.color)
  const paper = words[0] + words[1] + words[2] < 384        // dark words mean a light theme
  const inks = ['']
  for (let level = 1; level <= 15; level++) {
    const t = level / 15
    let at = 0
    while (at < 5 && t >= HEAT_AT[at + 1]) at++
    let colour = t <= HEAT_AT[0] ? mix([0, 0, 0], stops[0], 0.62 + (0.38 * t) / HEAT_AT[0])
      : t >= HEAT_AT[6] ? mix(stops[6], words, (0.7 * (t - HEAT_AT[6])) / (1 - HEAT_AT[6]))
        : mix(stops[at], stops[at + 1], (t - HEAT_AT[at]) / (HEAT_AT[at + 1] - HEAT_AT[at]))
    if (paper) colour = mix(colour, words, t >= HEAT_AT[5] ? 0.62 : 0.3)
    inks.push('rgb(' + colour.map(Math.round).join(',') + ')')
  }
  return inks
}

/** Fit a canvas to its box on the screen's own pixels, cleared. Returns its size, or null while it has none. */
function fitted(el) {
  const ratio = window.devicePixelRatio || 1, box = el.getBoundingClientRect()
  const W = Math.round(box.width * ratio), H = Math.round(box.height * ratio)
  if (!W || !H) return null
  if (el.width !== W || el.height !== H) { el.width = W; el.height = H }
  const g = el.getContext('2d')
  g.clearRect(0, 0, W, H)
  return { g, W, H, ratio }
}

function paint(el, piece, cells) {
  const fit = fitted(el)
  if (!fit) return
  const { g, W, H, ratio } = fit
  const inks = inksOf(el)
  const pitch = Math.max(2, Math.round(2 * ratio)), gap = Math.max(1, Math.round(pitch / 4))
  const left = W - piece.w * pitch, top = H - piece.h * pitch       // it sits in the right-hand foot of its box
  for (let level = 1; level <= 15; level++) {
    g.fillStyle = inks[level]
    const side = level > 12 ? pitch : pitch - gap
    for (let i = 0; i < cells.length; i++) {
      if (cells[i] !== level) continue
      const x = left + (i % piece.w) * pitch, y = top + Math.floor(i / piece.w) * pitch
      if (x > -pitch && y > -pitch) g.fillRect(x, y, side, side)
    }
  }
}

/** Keep a canvas drawn: when it first shows, when its box changes size, when the fonts arrive, and when the theme
    changes. A theme change is seen from the canvas itself (the colour of its own text is looked at once a second),
    so nothing outside the plugin's own markup is watched. */
function useDrawn(canvas, draw, deps) {
  useEffect(() => {
    const el = canvas.current
    if (!el) return undefined
    let frame = 0
    const again = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(() => draw(el)) }
    again()
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(again)
    const size = new ResizeObserver(again)
    size.observe(el)
    let words = getComputedStyle(el).color
    const watch = setInterval(() => {
      if (document.hidden) return
      const now = getComputedStyle(el).color
      if (now !== words) { words = now; again() }
    }, 1000)
    return () => { cancelAnimationFrame(frame); size.disconnect(); clearInterval(watch) }
  }, deps)
}

function Art({ name }) {
  const query = useQuery({ queryKey: ['stoa', 'art'], queryFn: () => rest('/art'), staleTime: Infinity, retry: false })
  const canvas = useRef(null)
  const piece = query.data && query.data[name]
  const cells = useRef(null)
  useDrawn(canvas, el => {
    if (!piece || !piece.cells) return
    if (!cells.current || cells.current.of !== piece) cells.current = { of: piece, grid: unpack(piece) }
    paint(el, piece, cells.current.grid)
  }, [piece])
  return h('canvas', { ref: canvas, className: 'stoa-art', 'aria-hidden': true })
}

// -- your own name, drawn the way the colonnade is ---------------------------------------------------
// Any name is set in the app's mono capitals off screen, read back as a grid of dots, and lit like the building:
// the light comes from the upper left, so each stroke is shaded as a column is, bright where an edge catches the
// light, sparse and blue in shadow, with one warm patch part-way along. The name stays whole: only the stoa
// itself comes apart at its far end.
// The name and where it sits are kept with the desk (POST /sign), so everyone who opens it sees the same thing.
const $typing = atom(null) // a name being typed on Setup, shown at the head of the page before it is saved

function paintName(el, text, side) {
  const fit = fitted(el)
  if (!fit) return
  const { g, W, H, ratio } = fit
  const inks = inksOf(el)
  const pitch = Math.max(2, Math.round(1.5 * ratio))
  const cols = Math.floor(W / pitch), rows = Math.floor(H / pitch)
  if (cols < 24 || rows < 10) return
  // 1. The name as a shape: three samples a dot each way.
  const S = 3, mw = cols * S, mh = rows * S
  const off = document.createElement('canvas')
  off.width = mw
  off.height = mh
  const o = off.getContext('2d', { willReadFrequently: true })
  const label = [...text.toUpperCase()], family = getComputedStyle(el).fontFamily
  const face = px => '700 ' + px + 'px ' + family
  o.font = face(100)
  const step = (o.measureText('M').width / 100) * 0.94    // a letter's width in ems: the mono face, set a little tight
  const room = mw - 4 * S
  let size = mh * 1.02                                    // capitals about three quarters of the box high
  // A long name is narrowed a little before it is made smaller.
  const squeeze = Math.min(1, Math.max(0.8, room / (label.length * step * size)))
  if (label.length * step * size * squeeze > room) size = room / (label.length * step * squeeze)
  const wide = label.length * step * size * squeeze, cap = size * 0.73
  const start = side === 'right' ? mw - wide - 2 * S : 2 * S          // against the right of its box, or the left
  o.font = face(size)
  o.textAlign = 'center'
  o.lineJoin = 'round'
  o.lineWidth = size * 0.05                               // a little weight added, so a stroke has room for its shading
  label.forEach((letter, index) => {
    o.setTransform(squeeze, 0, 0, 1, start + (index + 0.5) * step * size * squeeze, 0)
    o.fillText(letter, 0, (mh + cap) / 2)
    o.strokeText(letter, 0, (mh + cap) / 2)
  })
  o.setTransform(1, 0, 0, 1, 0, 0)
  const alpha = o.getImageData(0, 0, mw, mh).data
  const cover = new Float32Array(cols * rows)
  for (let y = 0; y < rows; y++) {
    for (let x = 0; x < cols; x++) {
      let sum = 0
      for (let j = 0; j < S; j++) for (let i = 0; i < S; i++) sum += alpha[((y * S + j) * mw + x * S + i) * 4 + 3]
      cover[y * cols + x] = sum / (255 * S * S)
    }
  }
  // 2. A soft copy of the shape: its slope at any dot says which way the nearest edge faces.
  const reach = Math.max(2, Math.round((size * 0.09) / S))
  const blur = (from, across) => {
    const to = new Float32Array(from.length), n = across ? cols : rows, m = across ? rows : cols
    for (let line = 0; line < m; line++) {
      for (let k = 0; k < n; k++) {
        let sum = 0
        for (let d = -reach; d <= reach; d++) {
          const q = Math.min(n - 1, Math.max(0, k + d))
          sum += from[across ? line * cols + q : q * cols + line]
        }
        to[across ? line * cols + k : k * cols + line] = sum / (2 * reach + 1)
      }
    }
    return to
  }
  const soft = blur(blur(cover, true), false)
  const at = (field, x, y) => field[Math.min(rows - 1, Math.max(0, y)) * cols + Math.min(cols - 1, Math.max(0, x))]
  const inside = (x, y) => x >= 0 && y >= 0 && x < cols && y < rows && cover[y * cols + x] >= 0.5
  const dither = (x, y) => { const v = 52.9829189 * ((0.06711056 * x + 0.00583715 * y) % 1); return v - Math.floor(v) }
  const chance = (x, y, salt) => { const v = Math.sin(x * 12.9898 + y * 78.233 + salt * 37.719) * 43758.5453; return v - Math.floor(v) }
  const x0 = Math.floor(start / S), x1 = Math.ceil((start + wide) / S)        // where the name starts and ends, in dots
  const along = x => (x - x0) / Math.max(1, x1 - x0)
  const dots = Array.from({ length: 16 }, () => [])
  for (let y = 0; y < rows; y++) {
    for (let x = 0; x < cols; x++) {
      if (!inside(x, y)) continue
      const gx = at(soft, x + 1, y) - at(soft, x - 1, y), gy = at(soft, x, y + 1) - at(soft, x, y - 1)
      const steep = Math.hypot(gx, gy)
      const lit = steep > 1e-4 ? ((0.8 * gx + 0.6 * gy) / steep) * Math.min(1, steep * reach * 1.7) : 0
      const warm = Math.exp(-((along(x) - 0.26) ** 2) / 0.022)
      const edge = !inside(x - 1, y) || !inside(x + 1, y) || !inside(x, y - 1) || !inside(x, y + 1)
      let t = 0.5 + 0.27 * lit + 0.24 * warm + 0.06 * (chance(x, y, 1) - 0.5)
      let odds = Math.min(1, Math.max(0, (t - 0.02) / 0.58)) ** 1.15
      if (edge) {
        t = lit > 0.1 ? 0.9 + 0.1 * warm : 0.36
        odds = lit > 0.1 ? 1 : 0.74
      }
      if (dither(x, y) < odds) dots[Math.min(15, Math.max(1, Math.round(t * 15)))].push(x, y)
    }
  }
  for (let level = 1; level <= 15; level++) {
    g.fillStyle = inks[level]
    const dot = level > 12 && pitch > 2 ? pitch : pitch - 1
    const list = dots[level]
    for (let i = 0; i < list.length; i += 2) g.fillRect(list[i] * pitch, list[i + 1] * pitch, dot, dot)
  }
}

function NameArt({ text, side }) {
  const canvas = useRef(null)
  useDrawn(canvas, el => paintName(el, text, side), [text, side])
  return h('canvas', { ref: canvas, className: 'stoa-art stoa-nameart', role: 'img', 'aria-label': text })
}

// (Setup and Welcome say why with `why(error)`.)
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
    company: deal.company || '', group: deal.group || '', email: deal.email || '', close_on: deal.close_on || '' }
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
      open ? h(Field, { label: 'Expected close', type: 'date', value: v.close_on, title: 'The forecast puts it in this month', onChange: set('close_on') }) : h('span'),
      h(Field, { label: 'Email', type: 'email', value: v.email, onChange: set('email') })),
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
function NewRecord({ state, close, opened, preset }) {
  const pb = state.playbook, terms = pb.terms
  const [v, setV] = useState({ name: '', company: '', group: '', stage: pb.stages[0] ? pb.stages[0].id : '', value: '', owner: '', email: '', phone: '', next_step: '', next_due: '', ...(preset || {}) })
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
        if (result && result.last) offerUndo(result.last)
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
      h(Field, { label: 'Phone', type: 'tel', value: v.phone, onChange: set('phone') }),
      h(Field, { label: 'Next step', wide: true, value: v.next_step, onChange: set('next_step') }),
      h(Field, { label: 'Due', type: 'date', value: v.next_due, onChange: set('next_due') })),
    h('datalist', { id: 'stoa-new-groups' }, uniq(state.deals.map(deal => deal.group)).map(name => h('option', { key: name, value: name }))),
    h('datalist', { id: 'stoa-new-owners' }, uniq(state.deals.map(deal => deal.owner)).map(name => h('option', { key: name, value: name }))),
    h('div', { className: 'stoa-btns' },
      h(Btn, { kind: 'primary', disabled: busy || !v.name.trim(), onClick: save }, 'Add to the desk'),
      h(Btn, { kind: 'quiet', disabled: busy, onClick: close }, 'Cancel')))
}

// -- one record: what it is, what is next, and the buttons ---------------------------------------
function Record({ id, state: shown, sessionId, close, onCompany, onGroup }) {
  const query = useQuery({ queryKey: ['stoa', 'deal', id], queryFn: () => rest('/deal/' + encodeURIComponent(id)), staleTime: 1500 })
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState(false)
  const [finishing, setFinishing] = useState(false)
  const deal = query.data
  if (query.error) return h('div', { className: 'stoa-box stoa-sub' }, 'That record is no longer on the desk.')
  if (!deal) return h('div', { className: 'stoa-box stoa-sub' }, 'Loading…')
  const elsewhere = Boolean(deal.pipeline && shown.pipeline && deal.pipeline.id !== shown.pipeline)     // from a pipeline the page is not showing
  const state = deal.playbook ? { ...shown, playbook: deal.playbook } : shown
  const pb = state.playbook
  const open = deal.status === 'open'
  const run = body => { setBusy(true); act(id, body, sessionId).then(() => setBusy(false)) }
  const facts = [[cap(pb.terms.group), deal.group], [pb.terms.value, money(deal.value, pb.currency)], [cap(pb.terms.owner), deal.owner],
    ['Expected close', open && deal.close_on ? shortDate(deal.close_on) + (deal.close_on < today() ? ' (past)' : '') : '']]
    .concat(pb.fields.map(field => [field.label, (deal.fields || {})[field.key]]))
    .concat([['Email', deal.email], ['Phone', deal.phone]]).filter(pair => pair[1])
  return h('div', { className: 'stoa-box' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'flex-start' } },
      h('div', { className: 'stoa-grow' },
        elsewhere ? h('div', { className: 'stoa-eyebrow', style: { marginBottom: 3 } }, deal.pipeline.name) : null,
        h('div', { className: 'stoa-name', style: { fontSize: 14 } }, deal.name),
        deal.company && onCompany ? h('div', null, h('button', { type: 'button', className: 'stoa-proplink stoa-sub', title: 'Everything at ' + deal.company, onClick: () => onCompany(deal.company) }, deal.company + '  ›'))
          : deal.company && deal.company !== deal.name ? h('div', { className: 'stoa-sub' }, deal.company) : null,
        deal.group && onGroup ? h('div', null, h('button', { type: 'button', className: 'stoa-proplink stoa-sub', title: 'The ' + pb.terms.group + ' this is in: its client, its terms and who is on their side',
          onClick: () => onGroup(deal.group) }, deal.group + (deal.client ? ' · for ' + deal.client : '') + (deal.group_status ? ' · ' + deal.group_status.toLowerCase() : '') + '  ›')) : null),
      close ? h(Btn, { kind: 'quiet', onClick: close, 'aria-label': 'Close' }, '×') : null),
    h('div', { className: 'stoa-meter' }, pb.stages.map((stage, index) =>
      h('i', { key: stage.id, style: heatAt(index, pb.stages.length), 'data-on': deal.status === 'won' || (open && index <= deal.position) ? '1' : '0' }))),
    h('div', { className: 'stoa-sub', style: { display: 'flex', justifyContent: 'space-between' } },
      h('span', null, deal.stage_label + (deal.lost_reason ? ' · ' + deal.lost_reason : '')),
      h('span', null, open && deal.position >= 0 ? deal.position + 1 + ' of ' + pb.stages.length : ago(deal.stage_at))),
    deal.due || deal.stale || deal.pending_task || asleep(deal) ? h('div', { className: 'stoa-chips' },
      deal.due ? h(Chip, { tone: dueTone(deal) }, deal.next_due < today() ? 'overdue' : 'due today') : null,
      deal.stale && !deal.due ? h(Chip, null, 'quiet') : null,
      asleep(deal) ? h(Chip, null, 'snoozed to ' + shortDate(deal.snoozed_until)) : null,
      deal.pending_task ? h(Chip, { tone: 'good' }, 'draft waiting') : null) : null,
    deal.next_step ? h('div', { className: 'stoa-next' }, h('small', { className: 'stoa-eyebrow' }, 'Next'),
      h('div', null, deal.next_step, deal.next_due ? h('span', { className: 'stoa-sub' }, '  ' + dueLabel(deal)) : null)) : null,
    h('div', { className: 'stoa-btns' },
      open && deal.advance ? h(Btn, { kind: 'primary', disabled: busy, onClick: () => run({ verb: 'advance', extra: deal.stage }) }, deal.advance + '  →') : null,
      open && deal.next_step ? h(Btn, { disabled: busy, title: 'The next step is done: log it and say what comes next', onClick: () => setFinishing(!finishing) }, 'Done') : null,
      open ? h(Btn, { disabled: busy, onClick: () => run({ verb: 'draft' }) }, 'Draft follow-up') : null,
      open ? h(Snooze, { deal, busy, run }) : null,
      open ? h('select', { className: 'stoa-select', value: '', disabled: busy, 'aria-label': pb.lost.label,
        onChange: event => { if (event.target.value !== '') run({ verb: 'lost', extra: event.target.value }) } },
        [h('option', { key: 'none', value: '' }, pb.lost.label + '…')].concat(pb.lost.reasons.map((reason, index) =>
          h('option', { key: index, value: String(index) }, reason)))) : null,
      deal.status === 'won' && (deal.delivery_stages || []).length ? h('select', { className: 'stoa-select', value: deal.delivery || '', disabled: busy,
        'aria-label': 'Where the work stands', title: 'Where the work stands', onChange: event => run({ verb: 'update', delivery: event.target.value }) },
        deal.delivery_stages.map(stage => h('option', { key: stage.id, value: stage.id }, stage.label))) : null,
      !open ? h(Btn, { kind: deal.status === 'won' ? '' : 'primary', disabled: busy, onClick: () => run({ verb: 'reopen' }) }, 'Reopen') : null,
      h(Btn, { kind: 'quiet', onClick: () => setEditing(!editing) }, editing ? 'Close editing' : 'Edit')),
    finishing && open && deal.next_step ? h(NextStep, { deal, mode: 'done', close: () => setFinishing(false) }) : null,
    editing ? h(Edit, { key: deal.stage, deal, state, done: () => setEditing(false) }) : null,
    facts.length ? h('div', { className: 'stoa-facts' }, facts.map(pair =>
      h('div', { key: pair[0], style: pair[0] === 'Email' || pair[0] === 'Phone' ? { gridColumn: '1 / -1' } : undefined }, h('small', { className: 'stoa-eyebrow' }, pair[0]), h('span', null, String(pair[1]))))) : null,
    pb.billing !== 'none' && (deal.status === 'won' || (deal.invoices || []).length) ? h(Money, { deal, pb }) : null,
    close ? h(People, { deal, term: pb.terms.deal }) : (deal.people || []).length ? h('div', { className: 'stoa-log' },
      h('small', { className: 'stoa-eyebrow' }, 'Also on this ' + pb.terms.deal),
      deal.people.map(person => h('p', { key: person.id }, person.name, h('span', { className: 'stoa-sub' }, '  ' + [person.role, person.title].filter(Boolean).join(' · '))))) : null,
    h(RecordNotes, { key: 'notes-' + deal.id, deal }),
    h(RecordActivity, { key: 'log-' + deal.id, deal, sessionId, full: Boolean(close) }),
    deal.undo ? h(Btn, { kind: 'quiet', disabled: busy, title: 'Put the record back as it was before: ' + deal.undo.text,
      onClick: () => { setBusy(true); undo(deal.id, deal.undo.op).then(() => setBusy(false)) } },
    h('span', { style: { display: 'inline-flex', alignItems: 'center', gap: 6 } }, h(Icon, { name: 'back', size: 13 }), 'Take back the last change')) : null)
}

/** A few lines kept on the record itself: what to know about it. Edited in place; saved like any other change, so it can be taken back. */
function RecordNotes({ deal, bare }) {
  const [text, setText] = useState(null) // null: showing what is saved
  const [busy, setBusy] = useState(false)
  const saved = deal.notes || ''
  const changed = text !== null && text.trim() !== saved.trim()
  const save = () => {
    setBusy(true)
    act(deal.id, { verb: 'update', notes: text }).then(result => { setBusy(false); if (result) setText(null) })
  }
  return h('div', { className: bare ? '' : 'stoa-log' },
    bare ? null : h('small', { className: 'stoa-eyebrow' }, 'Notes'),
    h('textarea', { className: 'stoa-field stoa-notes', style: bare ? { marginTop: 0 } : undefined, rows: saved ? Math.min(8, saved.split('\n').length + 1) : 2, maxLength: 4000,
      value: text === null ? saved : text, placeholder: 'What to know about them: how they found you, what they care about, what to avoid…',
      onChange: event => setText(event.target.value),
      onKeyDown: event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && changed) { event.preventDefault(); save() } } }),
    changed ? h('div', { className: 'stoa-btns', style: { marginTop: 8 } }, h(Btn, { kind: 'primary', disabled: busy, onClick: save }, 'Save notes'),
      h(Btn, { kind: 'quiet', disabled: busy, onClick: () => setText(null) }, 'Cancel')) : null)
}

const LOG_KINDS = { call: 'Call', email: 'Email', meeting: 'Meeting', note: 'Note', slack: 'Slack', linkedin: 'LinkedIn', stage: 'Stage',
  created: 'Added', updated: 'Edited', task: 'Task', enriched: 'Lookup', invoice: 'Invoice', payment: 'Payment' }

/** What happened on the record, newest first, with a line to add to it. */
function RecordActivity({ deal, sessionId, full }) {
  const [all, setAll] = useState(false)
  const [line, setLine] = useState('')
  const [busy, setBusy] = useState(false)
  const entries = deal.activity || []
  const shown = all ? entries : entries.slice(0, full ? 6 : 4)
  const add = () => {
    const text = line.trim()
    if (!text) return
    setBusy(true)
    act(deal.id, { verb: 'log', kind: 'note', text }, sessionId).then(result => { setBusy(false); if (result) setLine('') })
  }
  return h('div', { className: 'stoa-log' },
    h('div', { className: 'stoa-sechead', style: { padding: 0, alignItems: 'center' } },
      h('small', { className: 'stoa-eyebrow' }, 'Activity'),
      entries.length > shown.length || all ? h('button', { type: 'button', className: 'stoa-proplink stoa-sub', onClick: () => setAll(!all) },
        all ? 'Fewer' : 'All ' + entries.length) : null),
    full ? h('div', { style: { display: 'flex', gap: 6, margin: '6px 0 8px' } },
      h('input', { className: 'stoa-field stoa-grow', value: line, disabled: busy, placeholder: 'Log a call, an email, a thought…', 'aria-label': 'Add to the log',
        onChange: event => setLine(event.target.value), onKeyDown: event => { if (event.key === 'Enter') { event.preventDefault(); add() } } }),
      line.trim() ? h(Btn, { disabled: busy, onClick: add }, 'Add') : null) : null,
    shown.length ? shown.map((entry, index) =>
      h('p', { key: index, style: entry.undone ? { textDecoration: 'line-through', opacity: 0.55 } : undefined },
        LOG_KINDS[entry.kind] && !['created', 'updated', 'stage'].includes(entry.kind) ? h('b', { style: { fontWeight: 550 } }, LOG_KINDS[entry.kind] + '  ') : null,
        entry.text,
        h('span', { className: 'stoa-sub' }, '  ' + ago(entry.at) + (entry.by ? ' · ' + entry.by : ''))))
      : h('p', { className: 'stoa-sub' }, 'Nothing logged yet.'))
}

// -- a list sent in: tick what to keep, then Apply ------------------------------------------------
function changeText(change) {
  if (change.field === 'Person') return 'add ' + change.to
  if (change.field === 'Due') return change.from ? 'Due: ' + shortDate(change.from) + ' → ' + shortDate(change.to) : 'Due: ' + shortDate(change.to)
  if (change.field === 'Note') return 'note: ' + change.to
  return change.from ? change.field + ': ' + change.from + ' → ' + change.to : change.field + ': ' + change.to
}

function Review({ id }) {
  const query = useQuery({ queryKey: ['stoa', 'intake', id], queryFn: () => rest('/intake/' + encodeURIComponent(id)), staleTime: 1500 })
  const [off, setOff] = useState({})
  const [placed, setPlaced] = useState({}) // what to do with each column that has no place: notes (the default), field or skip
  const [busy, setBusy] = useState(false)
  const intake = query.data
  if (query.error) return h('div', { className: 'stoa-box stoa-sub' }, 'That list is no longer here.')
  if (!intake) return h('div', { className: 'stoa-box stoa-sub' }, 'Loading…')
  const open = intake.status === 'open'
  const usable = intake.rows.filter(row => (row.kind === 'new' || row.kind === 'update') && (!open || !row.done))
  const rest_ = intake.rows.filter(row => row.kind !== 'new' && row.kind !== 'update')
  const chosen = usable.filter(row => !off[row.n])
  const counts = intake.counts, result = intake.result || {}
  const send = verb => {
    setBusy(true)
    rest('/intake/' + encodeURIComponent(id) + '/' + verb, { method: 'POST', body: verb === 'apply' ? { rows: chosen.map(row => row.n), columns: placed } : {} })
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
      h('span', { className: 'stoa-eyebrow' }, (intake.origin === 'proposal' ? 'Proposed · ' : 'Intake · ') + intake.source),
      open ? null : h(Chip, { tone: intake.status === 'applied' ? 'good' : '' }, intake.status)),
    h('div', { className: 'stoa-name', style: { marginTop: 4 } },
      [counts.new ? counts.new + ' new' : '', counts.update ? counts.update + ' to update' : '', counts.same ? counts.same + ' already there' : '',
        counts.unclear ? counts.unclear + ' unclear' : '', counts.problem ? counts.problem + ' unusable' : ''].filter(Boolean).join(' · ') || 'Nothing to add'),
    usable.length ? h('div', { className: 'stoa-list' }, usable.map(row => h('label', { key: row.n, className: 'stoa-pick', 'data-off': open && off[row.n] ? '1' : '0' },
      open ? h('input', { type: 'checkbox', checked: !off[row.n], disabled: busy, onChange: () => setOff({ ...off, [row.n]: !off[row.n] }) })
        : h('i', { className: 'stoa-dot', 'data-tone': row.applied ? 'good' : '', title: row.applied ? 'applied' : row.done === 'declined' ? 'declined' : 'left out' }),
      h('span', { className: 'stoa-grow' },
        h('span', { className: 'stoa-name' }, row.name),
        h('span', { className: 'stoa-sub' }, '  ' + [row.company !== row.name ? row.company : '', row.group].filter(Boolean).join(' · ')),
        h('span', { className: 'stoa-sub', style: { display: 'block' } },
          row.kind === 'new' ? 'New' + (row.stage ? ' at ' + row.stage : '') : row.changes.map(changeText).join(' · ')),
        row.case ? h('span', { className: 'stoa-sub', style: { display: 'block' } }, row.case) : null,
        (row.warnings || []).map((warning, index) => h('span', { key: index, className: 'stoa-sub', style: { display: 'block' } }, warning))),
      h(Chip, { tone: row.kind === 'new' ? 'good' : '' }, row.kind === 'new' ? 'new' : 'update')))) : null,
    open && (intake.columns || []).length ? h('div', { className: 'stoa-placed' },
      h('div', { className: 'stoa-sub', style: { marginBottom: 4 } }, 'These columns have no place on the desk. What should happen to them?'),
      intake.columns.map(column => h('div', { key: column.name, className: 'stoa-placerow' },
        h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name' }, column.name),
          h('span', { className: 'stoa-sub' }, '  e.g. ' + column.example)),
        h('select', { className: 'stoa-select', value: placed[column.name] || 'notes', disabled: busy, 'aria-label': 'What to do with ' + column.name,
          onChange: event => setPlaced({ ...placed, [column.name]: event.target.value }) },
        h('option', { value: 'notes' }, 'Keep in notes'), h('option', { value: 'field' }, 'Add as a field'), h('option', { value: 'skip' }, 'Leave out'))))) : null,
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
    f.stage && state.playbook.stages.some(stage => stage.id === f.stage) ? h('button', { type: 'button', className: 'stoa-toggle', 'data-on': '1',
      title: 'This stage is picked out on the board. Press to let go.', onClick: () => setFilter({ stage: '' }) },
    state.playbook.stages.find(stage => stage.id === f.stage).label + '  ×') : null,
    filtering(f) || f.stage ? h('button', { type: 'button', className: 'stoa-toggle', onClick: () => setFilter({ ...NO_FILTER }) }, 'Clear') : null)
}

// -- the funnel: how many reach each stage, and what share moves on ------------------------------
// Press a stage to pick out its column on the board (the others stay, dimmed); press it again to let go.
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
      links.push(h('polygon', { key: 'l' + index, className: 'stoa-fn-link', mask: 'url(#stoa-fn-mask)',
        points: [x + block, mid - tallness / 2, right, mid - after / 2, right, mid + after / 2, x + block, mid + tallness / 2].join(' ') }))
      links.push(h('rect', { key: 'q' + index, className: 'stoa-fn-pill', x: centre - 19, y: mid - 10, width: 38, height: 20, rx: 2 }))
      links.push(h('text', { key: 'p' + index, className: 'stoa-fn-pct', x: centre, y: mid + 4, textAnchor: 'middle' },
        step.reached ? Math.round((100 * next.reached) / step.reached) + '%' : '–'))
    }
    const on = Boolean(step.id) && filter.stage === step.id
    groups.push(h('g', { key: 'g' + index, className: 'stoa-fn-step', 'data-on': on ? '1' : '0', 'data-press': step.id ? '1' : '0',
      'data-heat': Math.min(6, 1 + Math.round((5 * index) / Math.max(1, steps.length - 2))),
      onClick: step.id ? () => setFilter({ stage: on ? '' : step.id }) : undefined },
      h('title', null, step.id ? (on ? 'Let go of ' + step.label : 'Pick out ' + step.label + ' on the board') : step.label),
      h('rect', { className: 'stoa-fn-hit', x: x - 4, y: 0, width: col - 2, height: H }),
      h('rect', { className: step.won ? 'stoa-fn-won' : 'stoa-fn-block', mask: step.won ? undefined : 'url(#stoa-fn-mask)', x, y: mid - tallness / 2, width: block, height: tallness,
        style: { animationDelay: index * 55 + 'ms' } }),
      h('text', { className: 'stoa-fn-label', x, y: 11 }, step.label.toUpperCase().slice(0, Math.max(6, Math.floor(col / 7.6)))),
      h('text', { className: 'stoa-fn-num', x, y: 31 }, String(step.reached)),
      h('text', { className: 'stoa-fn-sub', x, y: mid + tall / 2 + 22 },
        step.won ? (step.value ? money(step.value, pb.currency) : '') : step.here + ' here' + (step.value ? ' · ' + money(step.value, pb.currency) : '')),
      step.lost ? h('text', { className: 'stoa-fn-sub', x, y: mid + tall / 2 + 35 }, step.lost + ' lost here') : null))
  })
  return h('div', { className: 'stoa-funnel' },
    h('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'How many records reach each stage, and what share moves on' },
      h('defs', null,       // a mask of dots: whatever is drawn through it comes out stippled
        h('pattern', { id: 'stoa-fn-dots', width: 4, height: 4, patternUnits: 'userSpaceOnUse' }, h('rect', { width: 2.4, height: 2.4, fill: '#fff' })),
        h('mask', { id: 'stoa-fn-mask', maskUnits: 'userSpaceOnUse', x: 0, y: 0, width: W, height: H }, h('rect', { width: W, height: H, fill: 'url(#stoa-fn-dots)' }))),
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

/** The pane while the desk page is open beside it: not the same lists twice, but what the page is not showing. */
function Beside({ state }) {
  const tab = useValue($tab)
  const day = state.today || {}
  const byId = new Map(state.deals.map(deal => [deal.id, deal]))
  const coming = tab === 'today' ? [] : (day.coming || []).filter(row => byId.has(row.id))
  const lately = tab === 'activity' ? [] : (state.activity || []).slice(0, 30)
  return h('div', { className: 'stoa stoa-pane' }, h(Style),
    h('div', { className: 'stoa-scroll' },
      h('div', { style: { padding: '0 8px' } },
        state.tour ? h('div', { className: 'stoa-tourline' }, 'Sample data · nothing here is real') : null,
        h('div', { className: 'stoa-eyebrow' }, 'Stoa · beside the desk'),
        h('p', { className: 'stoa-sub', style: { margin: '6px 0 0' } }, 'The desk is open, so this shows what the page is not showing. Press a name to open it there.'),
        h('div', { className: 'stoa-rule' })),
      coming.length ? h('div', { className: 'stoa-sec' },
        h('div', { className: 'stoa-sechead' }, h('span', { className: 'stoa-eyebrow' }, 'Coming up this week'), h('span', { className: 'stoa-num' }, String(coming.length))),
        coming.slice(0, 7).map(row => h(Row, { key: row.id, deal: byId.get(row.id), sub: byId.get(row.id).next_step || byId.get(row.id).stage_label,
          right: shortDate(row.due), onClick: () => $want.set(row.id) }))) : null,
      lately.length ? h('div', { className: 'stoa-sec' },
        h('div', { className: 'stoa-sechead' }, h('span', { className: 'stoa-eyebrow' }, 'Lately on the desk')),
        lately.map(entry => h('div', { key: entry.id, className: 'stoa-row', style: { cursor: 'default' } },
          h('i', { className: 'stoa-dot', 'data-tone': entry.kind === 'stage' && !entry.undone ? 'good' : '' }),
          h('span', { className: 'stoa-grow', style: entry.undone ? { opacity: 0.55 } : undefined },
            byId.has(entry.deal) ? h('button', { type: 'button', className: 'stoa-proplink stoa-name', title: 'Open the record on the desk', onClick: () => $want.set(entry.deal) }, entry.name)
              : h('span', { className: 'stoa-name' }, entry.name),
            h('span', { style: { display: 'block', overflowWrap: 'anywhere', textDecoration: entry.undone ? 'line-through' : undefined } }, entry.text),
            h('span', { className: 'stoa-sub', style: { display: 'block' } }, ago(entry.at) + ' · ' + (entry.undone ? 'taken back' : entry.kind) + (entry.by ? ' · ' + entry.by : '')))))) : null,
      !coming.length && !lately.length ? h('p', { className: 'stoa-sub', style: { padding: 8 } }, 'Nothing has happened on the desk yet.') : null))
}

function Pane() {
  const query = useDesk()
  const filter = useValue($filter)
  const beside = useValue($onPage)
  const [openId, setOpenId] = useState('')
  if (!query.data) return h(Unavailable, { error: query.error, loading: query.isLoading })
  if (beside) return h(Beside, { state: query.data })
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
        state.tour ? h('div', { className: 'stoa-tourline' }, 'Sample data · nothing here is real') : null,
        h('div', { className: 'stoa-eyebrow' }, 'Stoa · ' + pb.title),
        h('div', { className: 'stoa-figure' }, h(DotFigure, { text: money(totals.open_value, pb.currency) || String(totals.open) }), h('small', null, totals.open_value ? 'in play' : pb.terms.deals + ' open')),
        h('div', { className: 'stoa-sub' }, [plural(totals.open, 'open', 'open'), totals.due ? totals.due + ' due' : '', totals.stale ? totals.stale + ' quiet' : '']
          .filter(Boolean).join(' · ')),
        h('div', { className: 'stoa-rule' }),
        state.deals.length ? h('div', { style: { marginTop: 10 } }, h(Filters, { state })) : null,
        filtering(filter) ? h('div', { className: 'stoa-sub', style: { marginTop: 6 } }, 'Showing ' + open.length + ' of ' + everyone.length) : null),
      (state.intakes || []).length ? h('div', { className: 'stoa-sec' },
        h('div', { className: 'stoa-sechead' }, h('span', { className: 'stoa-eyebrow' }, 'Waiting for your approval'), h('span', { className: 'stoa-num' }, state.intakes.length)),
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
  upchev: 'M6 14.5l6-6 6 6',
  x: 'M6.5 6.5l11 11M17.5 6.5l-11 11',
  back: 'M9 14.5L4 9.5l5-5M4 9.5h10.5a5.5 5.5 0 0 1 0 11H11'
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
  const draft = useValue($draft)
  useEffect(() => {
    if (!draft) return
    setText(draft)
    $draft.set('')
    const el = area.current
    if (el) { el.focus(); setTimeout(() => el.setSelectionRange(draft.length, draft.length), 0) }
  }, [draft])
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
    h(UndoBar),
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
        h('span', { className: 'stoa-prompt', 'aria-hidden': true }, '›'),
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

// -- Today: what wants you, most pressing first, one move per row ---------------------------------
const tomorrow = () => { const d = new Date(Date.now() + 864e5); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0') }
const shortDate = iso => { const d = new Date(iso + 'T12:00:00'); return isNaN(d) ? iso : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) }
const asleep = deal => Boolean(deal.snoozed_until) && deal.snoozed_until > today()

/** What comes after a step is done, or the first step for a record that has none. Nothing is saved until the button. */
function NextStep({ deal, mode, close }) {
  const [step, setStep] = useState('')
  const [due, setDue] = useState('')
  const [busy, setBusy] = useState(false)
  const save = () => {
    if (busy || (mode === 'plan' && !step.trim())) return
    setBusy(true)
    act(deal.id, { verb: mode === 'done' ? 'done' : 'update', next_step: step.trim(), next_due: due }).then(result => { setBusy(false); if (result) close() })
  }
  return h('div', { className: 'stoa-nextform' },
    mode === 'done' ? h('div', { className: 'stoa-sub', style: { gridColumn: '1 / -1' } }, 'Done: ' + deal.next_step + '. What comes next?') : null,
    h('input', { className: 'stoa-field', autoFocus: true, value: step, 'aria-label': 'The next step',
      placeholder: mode === 'done' ? 'The next step (leave empty if nothing is planned)' : 'The next step',
      onChange: event => setStep(event.target.value),
      onKeyDown: event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) save(); if (event.key === 'Escape') close() } }),
    h('input', { className: 'stoa-field', type: 'date', value: due, min: today(), 'aria-label': 'Due', onChange: event => setDue(event.target.value) }),
    h(Btn, { kind: 'primary', disabled: busy || (mode === 'plan' && !step.trim()), onClick: save }, mode === 'done' ? 'Mark done' : 'Save'),
    h(Btn, { kind: 'quiet', disabled: busy, onClick: close }, 'Cancel'))
}

/** Put a record aside for a while, or until a date. Until then it is on no list that asks for attention. */
function Snooze({ deal, busy, run }) {
  const [picking, setPicking] = useState(false)
  const [date, setDate] = useState('')
  if (picking) {
    return h('span', { className: 'stoa-pickdate' },
      h('input', { className: 'stoa-field', type: 'date', autoFocus: true, min: tomorrow(), value: date, 'aria-label': 'Snooze until', onChange: event => setDate(event.target.value),
        onKeyDown: event => { if (event.key === 'Escape') setPicking(false) } }),
      h(Btn, { disabled: busy || !date || date <= today(), onClick: () => { run({ verb: 'snooze', until: date }); setPicking(false) } }, 'Snooze'),
      h(Btn, { kind: 'quiet', onClick: () => setPicking(false), 'aria-label': 'Cancel' }, '×'))
  }
  const off = asleep(deal)
  return h('select', { className: 'stoa-select', value: '', disabled: busy, 'aria-label': 'Snooze',
    title: 'Put it aside: it leaves Today, the due list and the quiet list until then',
    onChange: event => {
      const pick = event.target.value
      if (pick === 'date') setPicking(true)
      else if (pick === 'wake') run({ verb: 'snooze', days: 0 })
      else if (pick) run({ verb: 'snooze', days: Number(pick) })
    } },
  [h('option', { key: 'none', value: '' }, off ? 'Snoozed to ' + shortDate(deal.snoozed_until) : 'Snooze…')]
    .concat(off ? [h('option', { key: 'wake', value: 'wake' }, 'Wake it now')] : [])
    .concat([['1', 'Until tomorrow'], ['3', '3 days'], ['7', 'A week'], ['14', 'Two weeks'], ['30', 'A month']].map(pair => h('option', { key: pair[0], value: pair[0] }, pair[1])))
    .concat([h('option', { key: 'date', value: 'date' }, 'Pick a date…')]))
}

/** Hand a record's next step to Hermes from the page: it writes in the dock's conversation. */
function draftFor(deal, quiet) {
  if (quiet || !deal.next_step) return act(deal.id, { verb: 'draft' })
  toChat('On the Stoa desk, ' + deal.name + ' (record ' + deal.id + ') has this next step due: “' + deal.next_step + '”. Draft what it needs and show me.')
  return Promise.resolve(null)
}

// Each kind of thing that wants you is a group that opens. Closed, it is one line: how many, what, and who.
const $folds = atom({}) // which groups on Today are open, kept between visits: { overdue: true, … }
function setFold(id, open) {
  const next = { ...$folds.get(), [id]: open }
  $folds.set(next)
  if (storage) storage.set('folds', next)
}

/** The first few names and how many more: enough to know a group without opening it. */
function names(list, limit = 3) {
  const all = [...new Set(list.filter(Boolean))]
  if (all.length > limit) return all.slice(0, limit).join(', ') + ' and ' + (all.length - limit) + ' more'
  return all.length > 1 ? all.slice(0, -1).join(', ') + ' and ' + all[all.length - 1] : all.join('')
}

function Fold({ id, count, tone, title, help, summary, aside, children }) {
  const open = Boolean(useValue($folds)[id])
  return h('section', { className: 'stoa-fold', 'data-open': open ? '1' : '0' },
    h('div', { className: 'stoa-foldhead' },
      h('button', { type: 'button', className: 'stoa-foldmain', 'aria-expanded': open, title: help, onClick: () => setFold(id, !open) },
        h('span', { className: 'stoa-foldmark', 'aria-hidden': true }, open ? '[−]' : '[+]'),
        h('b', { className: 'stoa-foldcount', 'data-tone': tone || '' }, String(count)),
        h('span', { className: 'stoa-foldtitle' }, title),
        h('span', { className: 'stoa-foldsum stoa-clip' }, open ? '' : summary)),
      open && aside ? h('span', { className: 'stoa-foldaside' }, aside) : null),
    open ? h('div', { className: 'stoa-foldbody' }, children) : null)
}

const TODAY_LISTS = [
  { key: 'overdue', title: 'Overdue', tone: 'bad', help: 'The next step had a date, and the date has passed.' },
  { key: 'due_today', title: 'Due today', tone: 'warn', help: 'The next step is dated today.' },
  { key: 'drafts', title: 'Writing waiting for Hermes', tone: 'good', help: 'A button asked for a draft and nobody has picked it up yet.' },
  { key: 'invoices_overdue', title: 'Invoices overdue', tone: 'bad', help: 'Sent, not paid in full, and past the due date.' },
  { key: 'to_invoice', title: 'Won, not invoiced', tone: 'warn', help: 'Won in the last two months with no invoice on the record. Shown once the desk keeps invoices at all.' },
  { key: 'quiet', title: 'Gone quiet', tone: '', help: 'No call, email, note or move for longer than the stage allows. Your playbook sets the days for each stage.' },
  { key: 'new', title: 'New, nothing planned', tone: '', help: 'Added in the last week, with no next step yet.' }
]

function Today({ state, openId, setOpenId, openClient }) {
  const pb = state.playbook, day = state.today
  const [form, setForm] = useState(null) // the row with its "what comes next" form open: { id, mode }
  const [busy, setBusy] = useState('')
  const byId = new Map(state.deals.map(deal => [deal.id, deal]))
  const run = (id, body) => { setBusy(id); return act(id, body).then(() => setBusy('')) }
  const facts = {
    overdue: (deal, row) => [deal.next_step || deal.stage_label, row.days + 'd overdue'],
    due_today: deal => [deal.next_step || deal.stage_label, 'today'],
    drafts: deal => [deal.stage_label + ' · a draft is waiting to be written', ''],
    invoices_overdue: (deal, row) => ['Invoice' + (row.number ? ' #' + row.number : '') + ' · ' + exact(row.owed, pb.currency) + ' owed', row.days + 'd late'],
    to_invoice: (deal, row) => [exact(row.amount, pb.currency) + ' · ' + pb.won.label.toLowerCase() + (row.days ? ' ' + row.days + 'd ago' : ' today'), ''],
    quiet: (deal, row) => [deal.stage_label + ' · nothing for ' + row.days + 'd', 'nudge due after ' + row.expected + 'd'],
    new: deal => [[deal.company, 'added ' + ago(deal.created_at)].filter(Boolean).join(' · '), '']
  }
  const moves = {
    overdue: deal => [h(Btn, { key: 'done', kind: 'go', title: 'Log it as done and say what comes next', onClick: () => setForm({ id: deal.id, mode: 'done' }) }, '✓  Done'),
      h(Btn, { key: 'draft', title: 'Hermes drafts what this step needs', onClick: () => draftFor(deal) }, 'Draft it'),
      h(Snooze, { key: 'snooze', deal, busy: busy === deal.id, run: body => run(deal.id, body) })],
    invoices_overdue: (deal, row) => [
      h(Btn, { key: 'paid', kind: 'go', title: 'Everything still owed came in', onClick: () => bill(deal.id, { verb: 'pay', invoice: String(row.invoice) }) }, 'Paid'),
      h(Btn, { key: 'remind', title: 'Hermes drafts a payment reminder', onClick: () => toChat('On the Stoa desk, ' + deal.name + ' (record ' + deal.id + ') has an invoice'
        + (row.number ? ' #' + row.number : '') + ' with ' + exact(row.owed, pb.currency) + ' still owed, ' + row.days + ' days past due. Draft a short, polite payment reminder and show me.') }, 'Draft a reminder'),
      h(Snooze, { key: 'snooze', deal, busy: busy === deal.id, run: body => run(deal.id, body) })],
    to_invoice: deal => [
      h(Btn, { key: 'bill', kind: 'go', onClick: () => { $billing.set(deal.id); setOpenId(deal.id) } }, 'Add invoice'),
      h(Snooze, { key: 'snooze', deal, busy: busy === deal.id, run: body => run(deal.id, body) })],
    drafts: deal => [h(Btn, { key: 'write', kind: 'go', onClick: () => toChat('Do the writing task waiting on ' + deal.name + ' on the Stoa desk (record ' + deal.id + ').') }, 'Write it now')],
    quiet: deal => [h(Btn, { key: 'draft', kind: 'go', title: 'Hermes drafts a follow-up from the record and its log', onClick: () => draftFor(deal, true) }, 'Draft a nudge'),
      h(Snooze, { key: 'snooze', deal, busy: busy === deal.id, run: body => run(deal.id, body) })],
    new: deal => [h(Btn, { key: 'plan', kind: 'go', onClick: () => setForm({ id: deal.id, mode: 'plan' }) }, 'Plan the next step')],
  }
  moves.due_today = moves.overdue
  const rows = (key, tone) => (day[key] || []).map(row => {
    const deal = byId.get(row.id)
    if (!deal) return null
    const [sub, right] = facts[key](deal, row)
    const open = Boolean(form) && form.id === deal.id
    return h('div', { key: key + deal.id + ':' + (row.invoice || ''), className: 'stoa-trow', 'data-open': openId === deal.id ? '1' : '0' },
      h('button', { type: 'button', className: 'stoa-tmain', title: 'Open the record', onClick: () => setOpenId(openId === deal.id ? '' : deal.id) },
        h('i', { className: 'stoa-dot', 'data-tone': tone }),
        h('span', { className: 'stoa-grow' },
          h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, deal.name,
            deal.value ? h('span', { className: 'stoa-sub', style: { fontWeight: 400 } }, '  ' + money(deal.value, pb.currency)) : null),
          h('span', { className: 'stoa-sub stoa-clip', style: { display: 'block' } }, sub))),
      right ? h('span', { className: 'stoa-num', 'data-tone': tone }, right) : null,
      h('span', { className: 'stoa-tacts' }, moves[key](deal, row)),
      open ? h(NextStep, { deal, mode: form.mode, close: () => setForm(null) }) : null)
  })
  const counts = day.counts
  // What a closed group says about itself: who is in it, and the one figure that matters most.
  const brief = key => {
    const list = day[key] || []
    const most = Math.max(0, ...list.map(row => row.days || 0))
    const sum = field => exact(list.reduce((total, row) => total + (row[field] || 0), 0), pb.currency)
    const tail = key === 'overdue' && most ? 'oldest ' + most + 'd' : key === 'quiet' && most ? 'longest ' + most + 'd'
      : key === 'invoices_overdue' ? sum('owed') + ' owed' : key === 'to_invoice' ? sum('amount') : ''
    return [names(list.map(row => (byId.get(row.id) || {}).name)), tail].filter(Boolean).join(' · ')
  }
  const hour = new Date().getHours()
  const hello = hour >= 5 && hour < 12 ? 'Good morning' : hour >= 12 && hour < 18 ? 'Good afternoon' : 'Good evening'
  return h('div', { className: 'stoa-today' },
    h('div', { className: 'stoa-todayhead' },
      h('h2', { className: 'stoa-hello' }, hello + '. ' + (day.needs ? plural(day.needs, 'thing needs you', 'things need you') : 'Nothing needs you today') + '.'),
      h('p', { className: 'stoa-sub' }, day.needs ? 'Most pressing first. Open a group to see what is in it and deal with it.'
        : counts.coming ? 'What is coming up is on the right.' : state.deals.length ? 'No next step is due and nobody has gone quiet.' : '')),
    h('div', { className: 'stoa-folds' },
      h(Approvals, { state, setOpenId }),
      (day.to_bill || []).length ? h(Fold, { id: 'to_bill', count: day.to_bill.length, tone: 'warn', title: 'To bill',
        help: 'Months that have ended for clients billed by the month, not billed yet', summary: names(day.to_bill.map(row => row.client)) },
      day.to_bill.map(row => h(BillMonth, { key: row.client + row.period, row, pb, showClient: true, openClient }))) : null,
      (day.client_overdue || []).length ? h(Fold, { id: 'client_overdue', count: day.client_overdue.length, tone: 'bad', title: 'Client invoices overdue',
        help: 'Sent to a client, not paid in full, and past the due date.',
        summary: [names(day.client_overdue.map(row => row.client)), exact(day.client_overdue.reduce((total, row) => total + (row.owed || 0), 0), pb.currency) + ' owed'].join(' · ') },
      day.client_overdue.map(row => h('div', { key: row.invoice, className: 'stoa-irow' },
        h('span', { className: 'stoa-grow' },
          h('button', { type: 'button', className: 'stoa-proplink stoa-name', onClick: () => openClient(row.client) }, row.client),
          h('span', { className: 'stoa-sub', style: { display: 'block' } }, [row.period ? monthLabel(row.period) : '', row.number ? '#' + row.number : '', exact(row.owed, pb.currency) + ' owed'].filter(Boolean).join(' · '))),
        h('span', { className: 'stoa-num', 'data-tone': 'bad' }, row.days + 'd late'),
        h('span', { className: 'stoa-tacts' }, h(Btn, { kind: 'go', onClick: () => billClient(row.client, { verb: 'pay', invoice: String(row.invoice) }) }, 'Paid'))))) : null,
      TODAY_LISTS.map(list => (day[list.key] || []).length ? h(Fold, { key: list.key, id: list.key, count: counts[list.key], tone: list.tone,
        title: list.key === 'to_invoice' ? pb.won.label + ', not invoiced' : list.title, help: list.help, summary: brief(list.key) },
      rows(list.key, list.tone)) : null)))
}

/** Beside Today while no record is open: what is coming, how the week is going, and what is put aside. */
function Ahead({ state, setOpenId }) {
  const day = state.today, totals = state.totals, pb = state.playbook
  const byId = new Map(state.deals.map(deal => [deal.id, deal]))
  const line = (row, right, extra) => {
    const deal = byId.get(row.id)
    return deal ? h('div', { key: row.id, className: 'stoa-aheadrow' },
      h('button', { type: 'button', className: 'stoa-row', style: { padding: '5px 6px' }, title: 'Open the record', onClick: () => setOpenId(deal.id) },
        h('span', { className: 'stoa-grow' },
          h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, deal.name),
          h('span', { className: 'stoa-sub stoa-clip', style: { display: 'block' } }, deal.next_step || deal.stage_label)),
        h('span', { className: 'stoa-num' }, right)), extra || null) : null
  }
  const coming = day.coming || [], snoozed = day.snoozed || []
  return h('div', { className: 'stoa-box stoa-ahead' },
    h('div', { className: 'stoa-sechead', style: { padding: 0 } }, h('span', { className: 'stoa-eyebrow' }, 'Coming up this week'), h('span', { className: 'stoa-num' }, String(coming.length))),
    coming.length ? coming.slice(0, 8).map(row => line(row, shortDate(row.due)))
      : h('div', { className: 'stoa-sub', style: { padding: '6px 0 2px' } }, 'No next step is dated in the next seven days.'),
    coming.length > 8 ? h('div', { className: 'stoa-sub', style: { padding: '4px 6px 0' } }, 'and ' + (coming.length - 8) + ' more') : null,
    h('div', { className: 'stoa-edit', style: { paddingTop: 10 } },
      h('div', { className: 'stoa-eyebrow' }, 'The last seven days'),
      h('div', { className: 'stoa-weekline' }, [[totals.new_week, 'new'], [totals.moves_week, totals.moves_week === 1 ? 'stage move' : 'stage moves']].map(pair =>
        h('span', { key: pair[1] }, h('b', null, String(pair[0])), ' ' + pair[1])))),
    snoozed.length ? h('div', { className: 'stoa-edit', style: { paddingTop: 10 } },
      h('div', { className: 'stoa-sechead', style: { padding: 0 } }, h('span', { className: 'stoa-eyebrow', title: 'Put aside until a date: on no list until then' }, 'Snoozed'),
        h('span', { className: 'stoa-num' }, String(snoozed.length))),
      snoozed.slice(0, 8).map(row => line(row, 'to ' + shortDate(row.until),
        h(Btn, { kind: 'quiet', title: 'Bring it back now', onClick: () => act(row.id, { verb: 'snooze', days: 0 }) }, 'Wake')))) : null)
}

// -- the way in: a desk nobody has set up yet --------------------------------------------------------
async function tour(action, example) {
  try {
    const result = await rest('/tour', { method: 'POST', body: { action, example: example || '' } })
    refresh()
    if (action === 'start') host.notify({ kind: 'info', message: 'Sample data: ' + result.title + '. Your own desk is untouched.' })
    else if (action === 'leave') host.notify({ kind: 'info', message: 'Back on your own desk.' })
    else host.notify({ kind: 'success', message: plural(result.removed || 0, 'sample record', 'sample records') + ' removed from your desk.' })
    return result
  } catch (error) {
    host.notifyError(error, 'Stoa could not do that')
    return null
  }
}

// -- setting a pipeline up: pick its shape, then its stages. One window, always in view ------------------
const $setupFlow = atom(null) // open: { mode: 'first' | 'add' | 'edit', step: 'shape' | 'stages', shape }

function ShapeCard({ shape, picked, current, onPick }) {
  return h('button', { type: 'button', className: 'stoa-shape', 'data-on': picked ? '1' : '0', onClick: () => onPick(shape) },
    h('div', { className: 'stoa-shapehead' }, h('span', { className: 'stoa-shapetitle' }, shape.title), current ? h(Chip, { tone: 'good' }, 'now') : null),
    h('div', { className: 'stoa-sub', style: { marginTop: 3 } }, shape.motion),
    h('ul', { className: 'stoa-uses' }, shape.uses.map(use => h('li', { key: use }, use))),
    h('div', { className: 'stoa-chain', style: { marginBottom: 0 } }, shape.stages.map(stage => stage.label).concat([shape.won]).join('  →  ')))
}

/** The stages, in order, as a list to rename, reorder, add to and take from. */
function StageEditor({ stages, setStages, most, record }) {
  const move = (index, by) => {
    const next = stages.slice()
    const [item] = next.splice(index, 1)
    next.splice(index + by, 0, item)
    setStages(next)
  }
  return h('div', null,
    h('ol', { className: 'stoa-stagelist' }, stages.map((stage, index) => h('li', { key: stage.key },
      h('span', { className: 'stoa-stagenum' }, String(index + 1)),
      h('input', { className: 'stoa-field', value: stage.label, maxLength: 40, 'aria-label': 'Stage ' + (index + 1), placeholder: 'Stage name',
        onChange: event => setStages(stages.map((item, at) => (at === index ? { ...item, label: event.target.value } : item))) }),
      h('button', { type: 'button', className: 'stoa-icon', disabled: index === 0, title: 'Earlier', 'aria-label': 'Move earlier', onClick: () => move(index, -1) }, h(Icon, { name: 'upchev', size: 14 })),
      h('button', { type: 'button', className: 'stoa-icon', disabled: index === stages.length - 1, title: 'Later', 'aria-label': 'Move later', onClick: () => move(index, 1) }, h(Icon, { name: 'down', size: 14 })),
      h('button', { type: 'button', className: 'stoa-icon', disabled: stages.length <= 2, title: 'Remove this stage', 'aria-label': 'Remove', onClick: () => setStages(stages.filter((item, at) => at !== index)) },
        h(Icon, { name: 'x', size: 13 }))))),
    h('div', { className: 'stoa-btns', style: { marginTop: 6 } },
      h(Btn, { disabled: stages.length >= most, onClick: () => setStages(stages.concat([{ key: 'n' + Date.now(), id: '', label: '' }])) },
        stages.length >= most ? 'Seven stages is the most' : '+  Add a stage'),
      h('span', { className: 'stoa-sub', style: { alignSelf: 'center' } }, 'Every ' + record + ' moves through these, in order, until it is won or lost.')))
}

function SetupFlow() {
  const flow = useValue($setupFlow)
  const query = useQuery({ queryKey: ['stoa', 'shapes', $pipe.get()], queryFn: () => rest('/shapes'), staleTime: 2000, enabled: Boolean(flow) })
  const [step, setStep] = useState('shape')
  const [shape, setShape] = useState(null)
  const [name, setName] = useState('')
  const [stages, setStages] = useState([])
  const [won, setWon] = useState('')
  const [lost, setLost] = useState('')
  const [describe, setDescribe] = useState('')
  const [busy, setBusy] = useState(false)
  const panel = useRef(null)
  const data = query.data
  const close = () => { if (!busy) $setupFlow.set(null) }
  const begin = (chosen, current) => {
    setShape(chosen)
    const same = current && current.shape === chosen.id
    const list = same ? current.stages : chosen.stages
    setStages(list.map((stage, index) => ({ key: 's' + index + stage.id, id: stage.id, label: stage.label })))
    setWon(same ? current.won : chosen.won)
    setLost(same ? current.lost : chosen.lost)
    setStep('stages')
  }
  // open where it was asked to: at the shapes, or straight at the stages of a shape
  useEffect(() => {
    if (!flow || !data) return
    setName(flow.mode === 'first' ? '' : flow.mode === 'add' ? '' : data.current.pipeline)
    setDescribe('')
    const wanted = data.shapes.find(item => item.id === (flow.shape || (flow.mode === 'edit' && flow.step === 'stages' ? data.current.shape : '')))
    if (flow.step === 'stages' && wanted) begin(wanted, flow.mode === 'edit' ? data.current : null)
    else { setShape(null); setStep('shape') }
  }, [flow, Boolean(data)])
  useEffect(() => {
    if (!flow) return undefined
    const key = event => { if (event.key === 'Escape') close() }
    document.addEventListener('keydown', key)
    return () => document.removeEventListener('keydown', key)
  }, [flow, busy])
  useEffect(() => { if (panel.current) panel.current.scrollTop = 0 }, [step])
  if (!flow) return null
  const most = data ? data.most_stages : 7
  const words = shape ? shape.record : 'record'
  const named = stages.map(stage => stage.label.trim()).filter(Boolean)
  const repeated = new Set(named.map(label => label.toLowerCase())).size !== named.length
  const ready = shape && named.length === stages.length && named.length >= 2 && named.length <= most && !repeated && (flow.mode !== 'add' || name.trim())
  const verb = flow.mode
  const submit = async ask => {
    if (!ready || busy) return
    setBusy(true)
    try {
      const result = await rest('/pipeline/setup', { method: 'POST', body: { verb, shape: shape.id, name: name.trim(), won: won.trim(), lost: lost.trim(),
        stages: stages.map(stage => ({ id: stage.id, label: stage.label.trim() })) } })
      if (result.ask) toChat(result.ask)
      if (result.added) { $pipe.set(result.added.id); if (storage) storage.set('pipe', result.added.id) }
      refresh()
      $setupFlow.set(null)
      host.notify({ kind: 'success', message: verb === 'add' ? (name.trim() + ' is open: ' + shape.title.toLowerCase() + ', ' + named.length + ' stages.')
        : verb === 'first' ? 'Your pipeline is set up. Add your first ' + words + ', or drop a list on the page.' : 'Saved: ' + named.join(' → ') + '.' })
      if (ask) say('Set up the stages of my ' + (name.trim() || (data && data.current.pipeline) || 'Stoa') + ' pipeline (' + shape.title.toLowerCase()
        + ') to fit how I work, at most ' + most + ' before it is won or lost. Ask me anything you need, then set them with gtm_playbook action stages. About my business: ' + ask)
    } catch (error) {
      host.notifyError(error, 'Stoa could not set that up')
    }
    setBusy(false)
  }
  const title = step === 'shape'
    ? (verb === 'add' ? 'A new pipeline: what kind of work is it for?' : verb === 'edit' ? 'Change what this pipeline is for' : 'What kind of work is your pipeline for?')
    : (verb === 'edit' ? 'The stages of ' + ((data && data.current.pipeline) || 'this pipeline') : 'Name it, and set its stages')
  return h('div', { className: 'stoa stoa-modal', role: 'dialog', 'aria-modal': true, 'aria-label': title, onMouseDown: event => { if (event.target === event.currentTarget) close() } },
    h('div', { className: 'stoa-modalpanel', ref: panel },
      h('div', { className: 'stoa-modalhead' },
        h('div', null,
          h('div', { className: 'stoa-eyebrow' }, step === 'shape' ? 'Step 1 of 2' : (flow.step === 'stages' && flow.mode === 'edit' ? shape && shape.title : 'Step 2 of 2 · ' + (shape ? shape.title : ''))),
          h('div', { className: 'stoa-modaltitle' }, title)),
        h('button', { type: 'button', className: 'stoa-icon', 'aria-label': 'Close', onClick: close }, h(Icon, { name: 'x' }))),
      !data ? h('p', { className: 'stoa-sub' }, query.error ? 'Stoa is not answering (' + why(query.error) + ').' : 'Loading…')
        : step === 'shape' ? h('div', { style: { paddingBottom: 22 } },
          h('p', { className: 'stoa-sub', style: { margin: '0 0 12px' } }, 'Pick the one closest to how the work moves. You set the stages next, in your own words.'),
          verb === 'edit' && data.current.open ? h('p', { className: 'stoa-warn' }, 'This pipeline has ' + plural(data.current.open, 'open record', 'open records') + '. A different shape only works if they are moved or closed first; for another kind of work, add a new pipeline instead.') : null,
          h('div', { className: 'stoa-shapes' }, data.shapes.map(item => h(ShapeCard, { key: item.id, shape: item, picked: shape && shape.id === item.id,
            current: verb === 'edit' && data.current.shape === item.id, onPick: chosen => begin(chosen, verb === 'edit' ? data.current : null) }))))
          : h('div', null,
            verb !== 'edit' ? h('div', { className: 'stoa-form', 'data-one': '1', style: { marginTop: 0, maxWidth: 420 } },
              h(Field, { label: verb === 'add' ? 'Name of the new pipeline' : 'Name your pipeline (you can change it later)', autoFocus: true, value: name,
                placeholder: shape && shape.id === 'roles' ? 'Contract searches' : shape && shape.id === 'leads' ? 'Client campaigns' : 'Sales',
                onChange: event => setName(event.target.value) })) : null,
            h('div', { className: 'stoa-modalsec' }, h('div', { className: 'stoa-name' }, 'Stages'),
              h('div', { className: 'stoa-sub', style: { marginBottom: 8 } }, 'Rename them to the words you use, put them in your order, add or take away. Two to ' + most + '.'),
              h(StageEditor, { stages, setStages, most, record: words }),
              repeated ? h('p', { className: 'stoa-warn' }, 'Two stages have the same name.') : null),
            h('div', { className: 'stoa-modalsec' },
              h('div', { className: 'stoa-form', style: { marginTop: 0, maxWidth: 520 } },
                h(Field, { label: 'When it is won, it is', value: won, placeholder: shape && shape.won, onChange: event => setWon(event.target.value) }),
                h(Field, { label: 'When it is lost, it is', value: lost, placeholder: shape && shape.lost, onChange: event => setLost(event.target.value) }))),
            h('details', { className: 'stoa-modalsec stoa-describe' },
              h('summary', null, 'Not sure? Describe your business and Hermes sets the stages'),
              h('textarea', { className: 'stoa-field stoa-notes', rows: 3, value: describe, placeholder: 'I am a contract recruiter for a VC fund; I place engineers and product people at its portfolio companies…',
                onChange: event => setDescribe(event.target.value) }),
              h('div', { className: 'stoa-btns' }, h(Btn, { disabled: !ready || busy || !describe.trim(), onClick: () => submit(describe.trim()) },
                verb === 'add' ? 'Open it, then ask Hermes' : 'Save these, then ask Hermes'),
              h('span', { className: 'stoa-sub', style: { alignSelf: 'center' } }, 'Hermes answers in the box at the foot of the page.')))),
      data && step === 'stages' ? h('div', { className: 'stoa-modalfoot' },
        flow.step === 'stages' && flow.mode === 'edit' ? h('span') : h(Btn, { kind: 'quiet', disabled: busy, onClick: () => setStep('shape') }, '‹  Back'),
        h('span', { className: 'stoa-grow' }),
        h(Btn, { kind: 'quiet', disabled: busy, onClick: close }, 'Cancel'),
        h(Btn, { kind: 'primary', disabled: !ready || busy, onClick: () => submit('') }, verb === 'add' ? 'Open the pipeline' : verb === 'first' ? 'Set it up' : 'Save stages')) : null))
}

function Welcome({ skip }) {
  const query = useQuery({ queryKey: ['stoa', 'shapes', 'welcome'], queryFn: () => rest('/shapes'), staleTime: 5000 })
  const setupQuery = useQuery({ queryKey: ['stoa', 'setup'], queryFn: () => rest('/setup'), staleTime: 3000 })
  const [busy, setBusy] = useState(false)
  const data = query.data, setup = setupQuery.data
  if (!data) return h('p', { className: 'stoa-sub' }, query.error ? 'The desk is not answering yet (' + why(query.error) + '). Reload in a moment.' : 'Opening the desk…')
  return h('div', { className: 'stoa-welcome' },
    h('div', { className: 'stoa-hero' },
      h('div', { className: 'stoa-heroart' }, h(Art, { name: 'hero' })),
      h('div', { className: 'stoa-eyebrow' }, 'Welcome to Stoa'),
      h('div', { className: 'stoa-herotitle' }, 'The CRM Hermes runs for you.'),
      h('p', { className: 'stoa-reading', style: { marginTop: 0 } },
        'You say what happened, in your own words. Hermes keeps the pipeline, logs your email and calls, drafts the follow-ups, and asks before anything changes. First: what kind of work is your pipeline for?')),
    h('div', { className: 'stoa-shapes', style: { marginTop: 14 } }, data.shapes.map(item => h(ShapeCard, { key: item.id, shape: item,
      onPick: chosen => $setupFlow.set({ mode: 'first', step: 'stages', shape: chosen.id }) }))),
    h('div', { className: 'stoa-sub', style: { marginTop: 10 } }, 'Two kinds of work? Set up one now; add the other as a second pipeline under Setup.'),
    h('div', { className: 'stoa-box', style: { marginTop: 16 } },
      h('div', { className: 'stoa-name' }, 'Or look around first'),
      h('div', { className: 'stoa-sub' }, 'A full pipeline of made-up people and companies, kept in a file of its own. Your desk stays empty, and one press brings you back.'),
      h('div', { className: 'stoa-btns' }, (setup ? setup.examples : []).map(example =>
        h(Btn, { key: example.id, disabled: busy, onClick: () => { setBusy(true); tour('start', example.id).then(() => setBusy(false)) } }, cap(example.title))),
        h(Btn, { kind: 'quiet', disabled: busy, onClick: skip }, 'Skip: I will set it up later'))))
}

// -- Setup: what is connected, the rules in force, and how to work the desk ------------------------
function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type: type || 'text/plain' }))
  const link = document.createElement('a')
  link.href = url
  link.download = name
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 4000)
}

const SAY = ['Add Priya Raman from Lumen Health, met at the conference, call her Friday', 'I spoke to Dana: she wants the proposal by Thursday',
  'Add their CFO, Lena Ortiz, to the Kestrel deal', 'Who do we know at Kestrel?', 'Undo that',
  'I invoiced Kestrel 12k today, net 30', 'Kestrel paid half of invoice 1042', 'How is cash looking, and what is likely to close this month?',
  'Move Marcus to the next stage and draft the email', 'Who has gone quiet, and what should I send each of them?', 'Here are my leads (then drop a CSV on the page)',
  'Map the market for us and add the three best fits', 'Write me a playbook for selling to dental practices']
const BY_HAND = ['Drag a card to another column to move it; drop it on the last column to close it as won.', 'Changed your mind? Undo appears above the box at the foot of the page, and on the record.',
  'Press a stage in the funnel to pick out its column on the board; press it again to let go.', 'Press a company name on a record, or Companies on the pipeline, for everything at that company.',
  'Press a card or a row to open the record: advance it, mark the step done, snooze it, or edit any fact.', 'Drop a CSV anywhere on the page. It comes back as a review; nothing is written until you apply.',
  'Use the microphone in the box at the foot of the page to dictate.']
const TYPED = [['/gtm', 'the board, in any Hermes chat'], ['/gtm today', 'what needs you, most pressing first'],
  ['/gtm cash', 'won, invoiced, collected, overdue, and the forecast'], ['/gtm brief', 'the morning brief'], ['/gtm <name>', 'one record as a card'],
  ['/gtm map', 'your market map'], ['!gtm …', 'the same in Slack, which keeps the slash for itself'], ['/blueprint', 'switch on an automation'], ['hermes stoa status', 'in a terminal: what is connected']]
const ALWAYS = ['Keeps one ledger: every change to a record or a person, what it replaced and what it left. Lines are never altered.',
  'Keeps each person once, however many records they are on.', 'Gives each record one next step and a date, and tells you when the date comes.',
  'Calls a record quiet when nothing has happened for longer than its stage allows.', 'Holds any list, and anything Hermes suggests, for your yes or no, with what it rests on.',
  'Remembers a no, and does not ask again.', 'Lets you take a change back, whoever made it, unless that would overwrite a later one.',
  'Takes a record off the desk without destroying it, so it can be put back.',
  'Logs what it finds in your email, calls and Slack on the right record, whoever on it sent it.', 'Posts cards in Slack whose buttons act at once, and only for the people allowed to press them.',
  'Keeps the invoices on a record in whole cents, and tells you what is late and what was won but never billed.',
  'Lets you snooze anything until a date, and export everything at any time.']

/** An error from the desk's backend, as a line a person can pass on: its message, and the status if there is one. */
function why(error) {
  if (!error) return ''
  const parts = [error.status || (error.response && error.response.status) || '', error.detail || (error.body && error.body.detail) || '', error.message || String(error)]
  return [...new Set(parts.map(part => String(part || '').trim()).filter(Boolean))].join(' · ').slice(0, 400) || 'No reason was given.'
}

function Lamp({ on }) {
  return h('i', { className: 'stoa-lamp', 'data-on': on === true ? '1' : on === false ? '0' : '', 'aria-label': on === true ? 'on' : on === false ? 'off' : 'not checked' })
}

/** The pipelines on this desk: each has its own records and its own playbook. Add one, rename one, remove an empty one. */
function Pipelines({ state, setup }) {
  const pipes = state.pipelines || []
  const [renaming, setRenaming] = useState('')
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const send = (body, said) => {
    setBusy(true)
    return rest('/pipeline', { method: 'POST', body })
      .then(result => { refresh(); if (said) host.notify({ kind: 'success', message: said }); return result })
      .catch(error => { host.notifyError(error, 'Stoa could not do that'); return null })
      .then(result => { setBusy(false); return result })
  }
  const rename = id => {
    if (!title.trim() || busy) return
    send({ verb: 'rename', id, name: title.trim() }).then(result => { if (result) setRenaming('') })
  }
  return h('div', { className: 'stoa-setbox', 'data-wide': '0' }, h('div', { className: 'stoa-box' },
    h('div', { className: 'stoa-sechead', style: { padding: '0 0 8px' } }, h('span', { className: 'stoa-eyebrow' }, 'Pipelines'),
      h('span', { className: 'stoa-sub' }, pipes.length > 1 ? pipes.length + ' side by side' : 'one')),
    pipes.map(item => h('div', { key: item.id, className: 'stoa-piperow' },
      renaming === item.id
        ? h('input', { className: 'stoa-field stoa-grow', autoFocus: true, value: title, 'aria-label': 'Name', onChange: event => setTitle(event.target.value),
          onKeyDown: event => { if (event.key === 'Enter') rename(item.id); if (event.key === 'Escape') setRenaming('') } })
        : h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name', style: { display: 'block' } }, item.name),
          h('span', { className: 'stoa-sub' }, item.title + ' · ' + item.open + ' open' + (item.id === state.pipeline ? ' · on screen' : ''))),
      renaming === item.id
        ? h(Btn, { kind: 'primary', disabled: busy || !title.trim(), onClick: () => rename(item.id) }, 'Save')
        : h(Btn, { kind: 'quiet', disabled: busy, onClick: () => { setRenaming(item.id); setTitle(item.name) } }, 'Rename'),
      !item.first && !item.open && renaming !== item.id ? h(Btn, { kind: 'quiet', disabled: busy, title: 'Only a pipeline with nothing in it can be removed',
        onClick: () => send({ verb: 'remove', id: item.id }, item.name + ' was removed.') }, 'Remove') : null)),
    h('p', { className: 'stoa-sub', style: { margin: '10px 0 0' } }, 'Each pipeline has its own records, its own playbook and its own autonomy setting: your own sales beside the work you do for clients, say. In a chat, name the pipeline; left out, Hermes means the first.'),
    pipes.length < 6 ? h('div', { className: 'stoa-btns', style: { marginTop: 10 } }, h(Btn, { onClick: () => $setupFlow.set({ mode: 'add', step: 'shape' }) }, '+  Pipeline')) : null))
}

/** The desk's own name: typed here, drawn at the head of the page as it is typed, and saved a moment after. */
function NameBox({ state }) {
  const sign = state.sign || { text: '', place: 'art' }
  const [text, setText] = useState(sign.text || '')
  const save = body => rest('/sign', { method: 'POST', body }).then(refresh).catch(error => host.notifyError(error, 'Stoa could not save the name'))
  useEffect(() => {
    if (text.trim() === (sign.text || '')) { $typing.set(null); return undefined }
    $typing.set(text)
    const timer = setTimeout(() => save({ text }), 700)
    return () => clearTimeout(timer)
  }, [text, sign.text])
  useEffect(() => () => $typing.set(null), [])
  return h('div', null,
    h('p', { className: 'stoa-sub', style: { margin: '0 0 10px' } }, 'Type your business name and it is drawn in dots at the head of the page, lit the way the stoa is. Leave it empty and the stoa stays. Everyone who opens this desk sees it.'),
    h('div', { className: 'stoa-newgroup', style: { margin: 0, maxWidth: 'none', alignItems: 'flex-end' } },
      h('label', null, h('span', { className: 'stoa-eyebrow' }, 'Name'),
        h('input', { className: 'stoa-field', maxLength: 26, value: text, placeholder: 'Your business name', onChange: event => setText(event.target.value) })),
      h('div', { className: 'stoa-tabs', style: { margin: 0 }, role: 'group', 'aria-label': 'Where the name sits' },
        [['art', 'In place of the stoa'], ['beside', 'Beside the stoa']].map(item => h('button', { type: 'button', key: item[0], className: 'stoa-tab', style: { height: 28 },
          'data-on': (sign.place === 'beside' ? 'beside' : 'art') === item[0] ? '1' : '0', 'aria-pressed': (sign.place === 'beside' ? 'beside' : 'art') === item[0],
          onClick: () => save({ place: item[0] }) }, item[1])))))
}

function Setup({ state, go }) {
  const query = useQuery({ queryKey: ['stoa', 'setup'], queryFn: () => rest('/setup'), staleTime: 3000, refetchInterval: 15000 })
  const [busy, setBusy] = useState(false)
  const setup = query.data
  if (!setup) {
    if (!query.error) return h('p', { className: 'stoa-sub' }, 'Loading…')
    // Say what actually went wrong: this is the tab people open when something is wrong.
    return h('div', { className: 'stoa-box', style: { maxWidth: 720 } },
      h('div', { className: 'stoa-name' }, 'Setup is not answering'),
      h('p', { className: 'stoa-sub', style: { margin: '6px 0 0', overflowWrap: 'anywhere' } }, why(query.error)),
      h('p', { className: 'stoa-sub', style: { margin: '6px 0 0' } }, 'The rest of the desk is not affected. If Stoa was just updated, restart the gateway (hermes gateway restart) and reload.'),
      h('div', { className: 'stoa-btns' }, h(Btn, { disabled: query.isFetching, onClick: () => query.refetch() }, query.isFetching ? 'Trying…' : 'Try again')))
  }
  const data = setup.data, sets = setup.playbook_sets, left = setup.checklist.filter(item => !item.done).length
  const wait = promise => { setBusy(true); return Promise.resolve(promise).then(() => setBusy(false)) }
  const button = item => {
    if (item.done) return null
    if (item.id === 'playbook') return setup.tour ? null : h(Btn, { onClick: () => $setupFlow.set({ mode: state.fresh ? 'first' : 'edit', step: 'shape' }) }, 'Set it up')
    if (item.id === 'records') return setup.tour ? null : h(Btn, { onClick: () => go('add') }, '+  New ' + state.playbook.terms.deal)
    if (item.id === 'sweep') return h(Btn, { onClick: () => $draft.set('/blueprint') }, 'Put /blueprint in the box')
    return null
  }
  const exportAll = () => wait(rest('/export').then(file => { download(file.filename, file.csv, 'text/csv'); host.notify({ kind: 'success', message: plural(file.rows, 'record', 'records') + ' exported.' }) })
    .catch(error => host.notifyError(error, 'Stoa could not export')))
  const box = (title, right, children, wide) => h('div', { className: 'stoa-setbox', 'data-wide': wide ? '1' : '0' }, h('div', { className: 'stoa-box' },
    h('div', { className: 'stoa-sechead', style: { padding: '0 0 8px' } }, h('span', { className: 'stoa-eyebrow' }, title), right ? h('span', { className: 'stoa-sub' }, right) : null),
    children))
  return h('div', { className: 'stoa-setup' },
    (setup.problems || []).length ? h('div', { className: 'stoa-setbox', 'data-wide': '1' }, h('div', { className: 'stoa-box', style: { borderColor: 'var(--ui-orange)' } },
      h('div', { className: 'stoa-name' }, 'Some of this could not be read'),
      setup.problems.map((line, index) => h('p', { key: index, className: 'stoa-sub', style: { margin: '4px 0 0', overflowWrap: 'anywhere' } }, line)))) : null,
    box('Your name on the desk', '', h(NameBox, { state }), true),
    box('Get started', left ? left + ' left' : 'all done', setup.checklist.map(item => h('div', { key: item.id, className: 'stoa-check', 'data-done': item.done ? '1' : '0' },
      h('i', { className: 'stoa-tick', 'aria-hidden': true }, item.done ? '✓' : ''),
      h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name', style: { display: 'block' } }, item.title), h('span', { className: 'stoa-sub' }, item.detail)),
      button(item))), true),
    box('Connected', '', setup.connections.map(item => h('div', { key: item.id, className: 'stoa-setrow' },
      h(Lamp, { on: item.on }), h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name', style: { display: 'block' } }, item.label), h('span', { className: 'stoa-sub' }, item.detail))))),
    box('What Hermes may do by itself', setup.autonomy.levels.find(level => level.id === setup.autonomy.level).label, h(Autonomy, { setup })),
    box('Rules in force', 'set in the plugin\'s settings', setup.rules.filter(rule => !rule.own).map(rule => h('div', { key: rule.label, className: 'stoa-setrow', style: { display: 'block' } },
      h('small', { className: 'stoa-eyebrow', style: { display: 'block' } }, rule.label), h('span', { style: { overflowWrap: 'anywhere' } }, rule.value)))),
    setup.tour ? null : h(Pipelines, { key: 'pipes', state, setup }),
    box('This pipeline', setup.playbook.shape_title || setup.playbook.title, [
      h('div', { key: 'title', className: 'stoa-name', style: { fontSize: 14 } }, setup.playbook.shape_title || setup.playbook.title),
      h('div', { key: 'sum', className: 'stoa-sub' }, setup.playbook.summary),
      h('div', { key: 'chain', className: 'stoa-chain' }, sets.stages.join('  →  ')),
      h('div', { key: 'edit', className: 'stoa-btns', style: { marginTop: 4 } },
        h(Btn, { kind: 'primary', disabled: Boolean(setup.tour), onClick: () => $setupFlow.set({ mode: 'edit', step: 'stages' }) }, 'Edit stages'),
        h(Btn, { disabled: Boolean(setup.tour), onClick: () => $setupFlow.set({ mode: 'edit', step: 'shape' }) }, 'Change what it is for')),
      h('div', { key: 'facts', className: 'stoa-facts', style: { marginTop: 10 } },
        [['The words', sets.words], ['A nudge is due after', sets.quiet.map(item => item.stage + ' ' + item.days + 'd').join(' · ')], ['Reasons for a loss', sets.lost_reasons.join(' · ')],
          ['Extra facts on a record', sets.fields.join(' · ') || 'none'], ['Hermes drafts when a record enters', sets.drafts.join(' · ') || 'nothing'],
          ['A lookup finds', sets.lookup === 'none' ? 'nothing (switched off)' : 'the ' + sets.lookup]].map(pair =>
          h('div', { key: pair[0], style: { gridColumn: '1 / -1' } }, h('small', { className: 'stoa-eyebrow' }, pair[0]), h('span', null, pair[1])))),
      h('div', { key: 'btns', className: 'stoa-btns' },
        h(Btn, { kind: 'quiet', onClick: () => $draft.set('Change my Stoa playbook: ') }, 'Ask Hermes for anything else (fields, drafts, timing)'))]),
    box('Your data', data.records ? plural(data.records, 'record', 'records') : 'empty', [
      h('div', { key: 'counts', className: 'stoa-facts', style: { marginTop: 0, paddingTop: 0, borderTop: 0 } },
        [['Open', data.open], ['Won', data.won], ['Lost', data.lost], ['Log entries', data.activities], ['Market maps', data.maps], ['Lists waiting', data.lists_waiting],
          ['Changes on the ledger', data.ledger], ['Taken off the desk', data.archived]].map(pair =>
          h('div', { key: pair[0] }, h('small', { className: 'stoa-eyebrow' }, pair[0]), h('span', null, String(pair[1]))))),
      h('div', { key: 'where', className: 'stoa-sub', style: { marginTop: 10, overflowWrap: 'anywhere' } }, 'One SQLite file on the machine Hermes runs on: ' + data.path),
      h('div', { key: 'copies', className: 'stoa-sub', style: { marginTop: 4, overflowWrap: 'anywhere' } }, data.backups && data.backups.count
        ? 'A copy is kept each day, the last fourteen: ' + plural(data.backups.count, 'copy', 'copies') + ' so far, the newest from ' + data.backups.newest + ', in ' + data.backups.folder + '.'
        : 'A copy of the file is kept each day once there is something on the desk, the last fourteen, in a backups folder beside it.'),
      h('div', { key: 'btns', className: 'stoa-btns' }, h(Btn, { disabled: busy, onClick: exportAll }, setup.tour ? 'Export the sample data as CSV' : 'Export everything as CSV')),
      h('div', { key: 'tour', className: 'stoa-edit', style: { paddingTop: 10 } },
        h('div', { className: 'stoa-name' }, setup.tour ? 'Sample data is on show' : 'Sample data'),
        h('div', { className: 'stoa-sub' }, setup.tour ? cap(setup.tour.title) + ', kept in a file of its own. Your desk is untouched and comes back when you leave.'
          : 'See a full pipeline without touching yours: made-up people and companies in a file of their own.'),
        h('div', { className: 'stoa-btns' }, setup.tour ? h(Btn, { kind: 'primary', disabled: busy, onClick: () => wait(tour('leave')) }, 'Leave the tour')
          : setup.examples.map(example => h(Btn, { key: example.id, disabled: busy, onClick: () => wait(tour('start', example.id)) }, cap(example.title)))),
        data.samples && !setup.tour ? h('div', { style: { marginTop: 10 } },
          h('div', { className: 'stoa-sub' }, plural(data.samples, 'sample record is', 'sample records are') + ' mixed into your own desk (an earlier version of Stoa put them there).'),
          h('div', { className: 'stoa-btns', style: { marginTop: 6 } }, h(Btn, { disabled: busy, onClick: () => wait(tour('tidy')) }, 'Remove them from my desk'))) : null)]),
    box('Automations', 'type /blueprint in a chat to switch one on', setup.automations.map(item => h('div', { key: item.name, className: 'stoa-setrow', style: { display: 'block' } },
      h('span', { className: 'stoa-name', style: { display: 'block' } }, item.title), h('span', { className: 'stoa-sub' }, item.description)))),
    box('How to work the desk', '', h('div', { className: 'stoa-cols', style: { gap: 22 } },
      h('div', null, h('div', { className: 'stoa-name' }, 'Say it'), h('div', { className: 'stoa-sub', style: { marginBottom: 6 } }, 'Press one to put it in the box below.'),
        SAY.map(line => h('button', { type: 'button', key: line, className: 'stoa-sayline', onClick: () => $draft.set(line.replace(/ \(then.*\)$/, '')) }, '“' + line + '”'))),
      h('div', null, h('div', { className: 'stoa-name', style: { marginBottom: 6 } }, 'Or do it by hand'), BY_HAND.map(line => h('p', { key: line, className: 'stoa-tip' }, line))),
      h('div', null, h('div', { className: 'stoa-name', style: { marginBottom: 6 } }, 'Or type it'),
        TYPED.map(pair => h('p', { key: pair[0], className: 'stoa-tip' }, h('code', null, pair[0]), '  ' + pair[1])))), true),
    box('What the desk always does, and what your playbook decides', '', h('div', { className: 'stoa-cols', style: { gap: 22 } },
      h('div', null, h('div', { className: 'stoa-name', style: { marginBottom: 6 } }, 'Always, whatever you sell'), ALWAYS.map(line => h('p', { key: line, className: 'stoa-tip' }, line))),
      h('div', null, h('div', { className: 'stoa-name', style: { marginBottom: 6 } }, 'Decided by your playbook'),
        [['What a record is called', sets.words], ['The stages', sets.stages.join(' → ')], ['How long before a nudge is due', 'per stage, ' + Math.min(...sets.quiet.map(item => item.days)) + ' to ' + Math.max(...sets.quiet.map(item => item.days)) + ' days'],
          ['Why records are lost', sets.lost_reasons.length + ' reasons to pick from'], ['The extra facts worth keeping', sets.fields.length ? sets.fields.join(', ') : 'none'],
          ['What Hermes writes at each stage', sets.drafts.length ? plural(sets.drafts.length, 'stage has', 'stages have') + ' a draft' : 'nothing yet']].map(pair =>
          h('p', { key: pair[0], className: 'stoa-tip' }, h('b', null, pair[0] + ': '), pair[1])))), true),
    h('div', { className: 'stoa-sub stoa-setbox', 'data-wide': '1', style: { textAlign: 'center' } }, 'Stoa ' + setup.version))
}

// -- what Hermes proposes: one decision each, with what it rests on -------------------------------
const EVIDENCE = { email: 'Email', call: 'Call', slack: 'Slack', meeting: 'Meeting', note: 'Note', calendar: 'Calendar' }

async function decide(item, verb) {
  try {
    const result = await rest('/intake/' + encodeURIComponent(item.intake) + '/row', { method: 'POST', body: { n: item.n, verb } })
    refresh()
    if (result.last) offerUndo(result.last)
    else if (result.note) host.notify({ kind: 'info', message: result.note })
    return result
  } catch (error) {
    host.notifyError(error, 'Stoa could not do that')
    return null
  }
}

function Proposal({ item, busy, run, open, follow }) {
  const what = item.kind === 'new' ? 'New' + (item.stage ? ' at ' + item.stage : '') : cap(item.changes.map(changeText).join(' · '))
  const where = [item.company !== item.name ? item.company : '', item.group !== item.company ? item.group : ''].filter(Boolean).join(' · ')
  return h('div', { className: 'stoa-proposal', 'data-follow': follow ? '1' : '0' },
    h('div', { className: 'stoa-prophead' },
      follow ? h('span', { className: 'stoa-sub' }, 'Also for ' + item.name)
        : item.deal ? h('button', { type: 'button', className: 'stoa-proplink stoa-name', title: 'Open the record', onClick: open }, item.name) : h('span', { className: 'stoa-name' }, item.name),
      h('span', { className: 'stoa-sub stoa-clip stoa-grow' }, follow ? '' : where),
      h('span', { className: 'stoa-sub', style: { flex: 'none' } }, item.source + ' · ' + ago(item.at))),
    h('div', { className: 'stoa-propchange' }, what),
    item.case ? h('div', { className: 'stoa-sub', style: { marginTop: 2 } }, item.case) : null,
    (item.evidence || []).map((entry, index) => h('blockquote', { key: index, className: 'stoa-evidence' },
      h('small', { className: 'stoa-eyebrow' }, [EVIDENCE[entry.kind] || cap(entry.kind) || 'Evidence', entry.at, entry.who].filter(Boolean).join(' · ')),
      entry.text ? h('span', null, entry.text) : null)),
    (item.warnings || []).map((warning, index) => h('div', { key: 'w' + index, className: 'stoa-sub' }, warning)),
    h('div', { className: 'stoa-btns', style: { marginTop: 10 } },
      h(Btn, { kind: 'go', disabled: busy, onClick: () => run('approve') }, 'Approve'),
      h(Btn, { kind: 'quiet', disabled: busy, title: 'Leave the desk as it is. Hermes will not propose this again.', onClick: () => run('decline') }, 'Decline')))
}

/** What sort of change a proposal is, by the heaviest thing it would do. */
const KINDS = [['steps', 'next step', 'next steps'], ['people', 'person', 'people'], ['stage', 'stage change', 'stage changes'],
  ['money', 'money entry', 'money entries'], ['new', 'new record', 'new records']]
function kindOf(item) {
  if (item.kind === 'new') return 'new'
  const fields = (item.changes || []).map(change => change.field)
  if (fields.includes('Invoice') || fields.includes('Payment')) return 'money'
  if (fields.includes('Stage')) return 'stage'
  if (fields.includes('Person')) return 'people'
  return 'steps'
}

/** The top of Today: everything waiting on a yes or a no. Nothing here has been written. */
function Approvals({ state, setOpenId }) {
  const items = (state.today && state.today.approvals) || []
  const [busy, setBusy] = useState('')
  if (!items.length) return null
  const key = item => item.intake + ':' + item.n
  // Suggestions about one record sit together, in the order the first of them arrived.
  const order = {}
  items.forEach((item, index) => { const about = item.list ? 'list:' + item.intake : item.deal || key(item); if (!(about in order)) order[about] = index })
  const sorted = items.map((item, index) => ({ item, index, at: order[item.list ? 'list:' + item.intake : item.deal || key(item)] }))
    .sort((a, b) => a.at - b.at || a.index - b.index).map(entry => entry.item)
  const proposals = sorted.filter(item => !item.list)
  const run = (item, verb) => { setBusy(key(item)); return decide(item, verb).then(() => setBusy('')) }
  const many = async (which, label) => {
    setBusy(label)
    try {
      const result = await rest('/approvals', { method: 'POST', body: { verb: 'approve', items: which.map(item => ({ intake: item.intake, n: item.n })) } })
      refresh()
      host.notify({ kind: 'success', message: plural(result.applied, 'change', 'changes') + ' made.' + ((result.notes || []).length ? ' ' + result.notes[0] : '') })
    } catch (error) {
      host.notifyError(error, 'Stoa could not do that')
    }
    setBusy('')
  }
  const kinds = KINDS.map(kind => ({ id: kind[0], one: kind[1], several: kind[2], items: proposals.filter(item => kindOf(item) === kind[0]) })).filter(kind => kind.items.length > 1)
  return h(Fold, { id: 'approvals', count: items.length, tone: 'good', title: 'Waiting for your approval',
    help: 'Changes Hermes suggests and lists you were sent. Nothing is written until you approve it.',
    summary: names(sorted.map(item => (item.list ? 'a list you were sent' : item.name))),
    aside: proposals.length > 1 ? h(Btn, { disabled: Boolean(busy), onClick: () => many(proposals, 'all') }, 'Approve all ' + proposals.length) : null },
    kinds.length && kinds[0].items.length < proposals.length ? h('div', { className: 'stoa-kinds' }, kinds.map(kind =>
      h(Btn, { key: kind.id, disabled: Boolean(busy), title: 'Approve every waiting ' + kind.one + ' at once. Each can still be taken back by itself.',
        onClick: () => many(kind.items, kind.id) }, 'Approve ' + kind.items.length + ' ' + kind.several))) : null,
    sorted.map((item, index) => item.list
      ? h('div', { key: item.intake, style: { marginBottom: 10 } }, h(Review, { id: item.intake }))
      : h(Proposal, { key: key(item), item, follow: index > 0 && !sorted[index - 1].list && Boolean(item.deal) && sorted[index - 1].deal === item.deal,
        busy: busy === 'all' || busy === kindOf(item) || busy === key(item), run: verb => run(item, verb), open: () => setOpenId(item.deal) })))
}

// -- taking a change back ----------------------------------------------------------------------------
function UndoBar() {
  const last = useValue($last)
  if (!last) return null
  return h('div', { className: 'stoa-undobar', role: 'status' },
    h('span', { className: 'stoa-clip' }, h('b', null, last.name), '  ' + last.text),
    h(Btn, { onClick: () => undo(last.deal, last.op) }, 'Undo'),
    h('button', { type: 'button', className: 'stoa-icon', style: { width: 24, height: 24 }, 'aria-label': 'Dismiss', onClick: () => $last.set(null) }, h(Icon, { name: 'x', size: 12 })))
}

// -- the other people on a record ---------------------------------------------------------------------
function People({ deal, term, title }) {
  const blank = { name: '', email: '', title: '', role: '', phone: '' }
  const [adding, setAdding] = useState(false)
  const [v, setV] = useState(blank)
  const [busy, setBusy] = useState(false)
  const set = key => event => setV({ ...v, [key]: event.target.value })
  const send = body => {
    setBusy(true)
    return rest('/deal/' + encodeURIComponent(deal.id) + '/person', { method: 'POST', body })
      .then(result => { refresh(); if (result.last) offerUndo(result.last); return result })
      .catch(error => { host.notifyError(error, 'Stoa could not save that'); return null })
      .then(result => { setBusy(false); return result })
  }
  const add = () => {
    if (!v.name.trim() || busy) return
    send({ verb: 'add', ...v }).then(result => {
      if (!result) return
      if (result.exists) host.notify({ kind: 'info', message: v.name.trim() + ' is already on this record.' })
      setV(blank)
      setAdding(false)
    })
  }
  const enter = event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) add() }
  const people = deal.people || []
  return h('div', { className: 'stoa-log' },
    h('div', { className: 'stoa-sechead', style: { padding: 0, alignItems: 'center' } },
      h('small', { className: 'stoa-eyebrow', title: 'Email, calls and messages from these people are filed on this record too' }, title || 'Also on this ' + term),
      h(Btn, { kind: 'quiet', onClick: () => setAdding(!adding) }, adding ? 'Close' : '+  Person')),
    people.map(person => h('div', { key: person.id, className: 'stoa-person' },
      h('span', { className: 'stoa-grow' },
        h('span', { className: 'stoa-name' }, person.name),
        person.role || person.title ? h('span', { className: 'stoa-sub' }, '  ' + [person.role, person.title].filter(Boolean).join(' · ')) : null,
        person.email || person.phone || person.also_on ? h('span', { className: 'stoa-sub', style: { display: 'block', overflowWrap: 'anywhere' } },
          [person.email, person.phone, person.also_on ? 'also on ' + plural(person.also_on, 'other record', 'other records') : ''].filter(Boolean).join(' · ')) : null),
      h('button', { type: 'button', className: 'stoa-icon', style: { width: 24, height: 24 }, disabled: busy, title: 'Take ' + person.name + ' off this record',
        'aria-label': 'Remove ' + person.name, onClick: () => send({ verb: 'remove', person: String(person.id) }) }, h(Icon, { name: 'x', size: 12 })))),
    !people.length && !adding ? h('div', { className: 'stoa-sub' }, 'Nobody else yet. Add the people around this ' + term + ' (a hiring manager, an assistant, finance), and what they send is filed here too.') : null,
    adding ? h('div', null,
      h('div', { className: 'stoa-form', style: { marginTop: 8 } },
        h(Field, { label: 'Name', autoFocus: true, value: v.name, onChange: set('name'), onKeyDown: enter }),
        h(Field, { label: 'Role here', value: v.role, placeholder: 'Decision maker, finance…', onChange: set('role'), onKeyDown: enter }),
        h(Field, { label: 'Title', value: v.title, onChange: set('title'), onKeyDown: enter }),
        h(Field, { label: 'Email', type: 'email', value: v.email, onChange: set('email'), onKeyDown: enter }),
        h(Field, { label: 'Phone', type: 'tel', value: v.phone, onChange: set('phone'), onKeyDown: enter })),
      h('div', { className: 'stoa-btns', style: { marginTop: 8 } }, h(Btn, { kind: 'primary', disabled: busy || !v.name.trim(), onClick: add }, 'Add'))) : null)
}

/** Every company on the desk, biggest open value first. Press one for the whole picture. */
function Companies({ state, current, openCompany }) {
  const filter = useValue($filter)
  const pb = state.playbook
  const text = filter.text.toLowerCase()
  const rows = (state.companies || []).filter(row => !text || row.name.toLowerCase().includes(text))
  if (!rows.length) return h('p', { className: 'stoa-sub' }, text ? 'No company matches “' + filter.text + '”.' : 'No company is named on any record yet.')
  return h('div', { className: 'stoa-companies' },
    h('div', { className: 'stoa-crow stoa-chead' }, ['Company', 'Open', pb.won.label, 'People', 'Last touch'].map(label => h('span', { key: label, className: 'stoa-eyebrow' }, label))),
    rows.map(row => h('button', { type: 'button', key: row.key, className: 'stoa-crow', 'data-open': current && current.toLowerCase() === row.name.toLowerCase() ? '1' : '0', onClick: () => openCompany(row.name) },
      h('span', { className: 'stoa-name stoa-clip' }, row.name),
      h('span', { className: 'stoa-num' }, row.open ? row.open + (row.open_value ? ' · ' + money(row.open_value, pb.currency) : '') : '—'),
      h('span', { className: 'stoa-num' }, row.won ? row.won + (row.won_value ? ' · ' + money(row.won_value, pb.currency) : '') : '—'),
      h('span', { className: 'stoa-num' }, String(row.people)),
      h('span', { className: 'stoa-num' }, ago(row.last_touch) || '—'))))
}

// -- a group: a search, an account, a client. Who it is for, where it stands, its terms, who is on their side ----
const GROUP_STATUS = [['open', 'Open'], ['hold', 'On hold'], ['done', 'Done'], ['closed', 'Closed']]

/** Every group on the desk: who it is for, where it stands, what is open in it. Open ones first. */
function Groups({ state, current, openGroup }) {
  const filter = useValue($filter)
  const pb = state.playbook, term = pb.terms.group
  const [name, setName] = useState('')
  const [client, setClient] = useState('')
  const [busy, setBusy] = useState(false)
  const text = filter.text.toLowerCase()
  const rows = (state.groups || []).filter(row => !text || (row.name + ' ' + row.client).toLowerCase().includes(text))
  const open = () => {
    if (!name.trim() || busy) return
    setBusy(true)
    rest('/group', { method: 'POST', body: { name: name.trim(), new: true, client: client.trim() } })
      .then(result => { refresh(); setName(''); setClient(''); openGroup(result.group.name) })
      .catch(error => host.notifyError(error, 'Stoa could not open that'))
      .then(() => setBusy(false))
  }
  const enter = event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) open() }
  return h('div', { className: 'stoa-companies' },
    h('div', { className: 'stoa-newgroup' },
      h(Field, { label: 'New ' + term, value: name, placeholder: 'Its name', onChange: event => setName(event.target.value), onKeyDown: enter }),
      h(Field, { label: 'Client', value: client, placeholder: 'The company it is for', onChange: event => setClient(event.target.value), onKeyDown: enter }),
      h(Btn, { disabled: busy || !name.trim(), onClick: open }, 'Open it')),
    !rows.length ? h('p', { className: 'stoa-sub' }, text ? 'No ' + term + ' matches “' + filter.text + '”.' : 'No ' + term + ' yet. Open one above, or name one on a ' + pb.terms.deal + '.') : null,
    rows.length ? h('div', { className: 'stoa-crow stoa-chead', 'data-cols': '7' }, [cap(term), 'Client', 'Status', 'Open', pb.won.label, 'People', 'Last touch'].map(label =>
      h('span', { key: label, className: 'stoa-eyebrow' }, label))) : null,
    rows.map(row => h('button', { type: 'button', key: row.key, className: 'stoa-crow', 'data-cols': '7', 'data-dim': row.status === 'open' ? '0' : '1',
      'data-open': current && current.toLowerCase() === row.name.toLowerCase() ? '1' : '0', onClick: () => openGroup(row.name) },
    h('span', { className: 'stoa-name stoa-clip' }, row.name),
    h('span', { className: 'stoa-clip' }, row.client || '—'),
    h('span', { className: 'stoa-num' }, row.status_label),
    h('span', { className: 'stoa-num' }, row.open ? row.open + (row.open_value ? ' · ' + money(row.open_value, pb.currency) : '') : '—'),
    h('span', { className: 'stoa-num' }, row.won ? row.won + (row.won_value ? ' · ' + money(row.won_value, pb.currency) : '') : '—'),
    h('span', { className: 'stoa-num' }, String(row.people)),
    h('span', { className: 'stoa-num' }, ago(row.last_touch) || '—'))))
}

// -- how much a scheduled job may change by itself ----------------------------------------------------
function Autonomy({ setup }) {
  const [busy, setBusy] = useState(false)
  const auto = setup.autonomy
  const pick = level => {
    if (level === auto.level || busy) return
    setBusy(true)
    rest('/autonomy', { method: 'POST', body: { level } })
      .then(result => { refresh(); host.notify({ kind: 'success', message: 'Scheduled jobs: ' + result.label + '.' }) })
      .catch(error => host.notifyError(error, 'Stoa could not change that'))
      .then(() => setBusy(false))
  }
  const forget = () => {
    setBusy(true)
    rest('/declined/forget', { method: 'POST', body: {} })
      .then(result => { refresh(); host.notify({ kind: 'info', message: plural(result.forgotten, 'declined proposal', 'declined proposals') + ' forgotten.' }) })
      .catch(error => host.notifyError(error, 'Stoa could not do that'))
      .then(() => setBusy(false))
  }
  return h('div', null,
    h('div', { className: 'stoa-sub', style: { marginBottom: 8 } }, 'In a chat, Hermes does what you ask. This is for the jobs that run when you are not there. Only you can change it here: Hermes has no way to.'),
    auto.levels.map(level => h('button', { type: 'button', key: level.id, className: 'stoa-level', 'data-on': level.id === auto.level ? '1' : '0', disabled: busy,
      'aria-pressed': level.id === auto.level, onClick: () => pick(level.id) },
    h(Lamp, { on: level.id === auto.level }),
    h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name', style: { display: 'block' } }, level.label), h('span', { className: 'stoa-sub' }, level.summary)))),
    h('div', { className: 'stoa-edit', style: { paddingTop: 10 } },
      h('div', { className: 'stoa-sub' }, setup.declined ? plural(setup.declined, 'proposal you declined is', 'proposals you declined are') + ' remembered for 90 days and not made again.'
        : 'Anything it may not do is held on Today for your yes or no. A no is remembered for 90 days and not asked again.'),
      setup.declined ? h('div', { className: 'stoa-btns', style: { marginTop: 6 } }, h(Btn, { disabled: busy, onClick: forget }, 'Forget them')) : null))
}

// -- money: invoices on a record, where won work stands, and what is likely to come in ---------------
// Stoa sends no invoices and moves no money. It keeps the account of them, so the desk can say what
// is owed, what is late, and what was won but never billed.
const $billing = atom('') // the record whose "new invoice" form should be open when its panel appears
const BILL_TONE = { overdue: 'bad', partial: 'warn', paid: 'good' }
const BILL_WORD = { overdue: 'overdue', partial: 'part paid', sent: 'sent', draft: 'draft', paid: 'paid', void: 'void' }

/** An amount in full: an invoice is not rounded to the nearest thousand. */
function exact(value, currency) {
  const number = Number(value || 0)
  const text = number.toLocaleString(undefined, { minimumFractionDigits: Number.isInteger(number) ? 0 : 2, maximumFractionDigits: 2 })
  const sign = SYMBOL[currency] ?? ''
  return sign ? sign + text : text + ' ' + (currency || '')
}

const inDays = days => { const d = new Date(Date.now() + days * 864e5); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0') }

async function bill(dealId, body) {
  try {
    const result = await rest('/deal/' + encodeURIComponent(dealId) + '/invoice', { method: 'POST', body })
    refresh()
    if (result.last) offerUndo(result.last)
    return result
  } catch (error) {
    host.notifyError(error, 'Stoa could not save that')
    return null
  }
}

/** One invoice, with the moves that fit where it stands. */
function Invoice({ invoice, dealId, client, currency, name, onOpen }) {
  const [busy, setBusy] = useState(false)
  const [part, setPart] = useState(null) // null, or the amount being typed for a part payment
  const run = body => {
    setBusy(true)
    return (client ? billClient(client, { invoice: String(invoice.id), ...body }) : bill(dealId, { invoice: String(invoice.id), ...body }))
      .then(result => { setBusy(false); return result })
  }
  const owing = invoice.state === 'sent' || invoice.state === 'partial' || invoice.state === 'overdue'
  const takePart = () => { const amount = Number(part); if (amount > 0) run({ verb: 'pay', amount }).then(result => { if (result) setPart(null) }) }
  const when = invoice.state === 'paid' ? (invoice.paid_on ? 'paid ' + shortDate(invoice.paid_on) : 'paid')
    : invoice.state === 'overdue' ? invoice.days_overdue + 'd late'
      : invoice.due_on && owing ? 'due ' + shortDate(invoice.due_on) : ''
  return h('div', { className: 'stoa-irow', 'data-state': invoice.state },
    h('span', { className: 'stoa-grow', style: { minWidth: 0 } },
      name ? h('button', { type: 'button', className: 'stoa-proplink stoa-name stoa-clip', style: { display: 'block', maxWidth: '100%' }, title: 'Open the record', onClick: onOpen }, name) : null,
      h('span', { className: name ? 'stoa-sub' : 'stoa-name' }, (invoice.number ? '#' + invoice.number + ' · ' : '') + exact(invoice.amount, currency)),
      invoice.state === 'partial' || (invoice.state === 'overdue' && invoice.paid) ? h('span', { className: 'stoa-sub' }, ' · ' + exact(invoice.owed, currency) + ' still owed') : null,
      invoice.note ? h('span', { className: 'stoa-sub', style: { display: 'block' } }, invoice.note) : null),
    when ? h('span', { className: 'stoa-num', 'data-tone': BILL_TONE[invoice.state] || '' }, when) : null,
    h(Chip, { tone: BILL_TONE[invoice.state] || '' }, BILL_WORD[invoice.state] || invoice.state),
    h('span', { className: 'stoa-tacts' },
      part !== null ? [
        h('input', { key: 'amount', className: 'stoa-field', type: 'number', min: 0, step: 'any', autoFocus: true, value: part, placeholder: 'Amount received', 'aria-label': 'Amount received',
          style: { width: 124, marginTop: 0 }, onChange: event => setPart(event.target.value),
          onKeyDown: event => { if (event.key === 'Enter') takePart(); if (event.key === 'Escape') setPart(null) } }),
        h(Btn, { key: 'ok', kind: 'go', disabled: busy || !(Number(part) > 0), onClick: takePart }, 'Record'),
        h(Btn, { key: 'no', kind: 'quiet', onClick: () => setPart(null), 'aria-label': 'Cancel' }, '×')
      ] : [
        owing ? h(Btn, { key: 'paid', kind: 'go', disabled: busy, title: 'Everything still owed came in', onClick: () => run({ verb: 'pay' }) }, 'Paid') : null,
        owing ? h(Btn, { key: 'part', disabled: busy, title: 'Part of it came in', onClick: () => setPart('') }, 'Part') : null,
        invoice.state === 'draft' ? h(Btn, { key: 'send', kind: 'go', disabled: busy, onClick: () => run({ verb: 'send' }) }, 'Mark sent') : null,
        h('select', { key: 'more', className: 'stoa-select', value: '', disabled: busy, 'aria-label': 'More', style: { maxWidth: 74 },
          onChange: event => { const verb = event.target.value; if (verb) run({ verb }) } },
        [h('option', { key: '', value: '' }, 'More…')].concat(invoice.state !== 'void' && invoice.state !== 'paid' ? [h('option', { key: 'void', value: 'void' }, 'Void it')] : [],
          [h('option', { key: 'remove', value: 'remove' }, 'Remove (entered by mistake)')]))
      ]))
}

/** The money on one record: what was billed, what came in, and a form to add an invoice. */
function Money({ deal, pb }) {
  const billing = useValue($billing)
  const money = deal.money || { invoiced: 0, paid: 0, owed: 0, uninvoiced: 0 }
  const invoices = deal.invoices || []
  const suggested = deal.status === 'won' ? money.uninvoiced : Math.max(0, (deal.value || 0) - money.invoiced)
  const [adding, setAdding] = useState(false)
  const [v, setV] = useState({ amount: '', number: '', due: inDays(30), draft: false })
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (billing !== deal.id) return
    setAdding(true)
    $billing.set('')
  }, [billing, deal.id])
  useEffect(() => { if (adding) setV(now => ({ ...now, amount: now.amount || (suggested ? String(suggested) : '') })) }, [adding])
  const set = key => event => setV({ ...v, [key]: event.target.value })
  const add = () => {
    if (!(Number(v.amount) > 0) || busy) return
    setBusy(true)
    bill(deal.id, { verb: 'add', amount: Number(v.amount), number: v.number.trim(), due: v.due, status: v.draft ? 'draft' : 'sent' }).then(result => {
      setBusy(false)
      if (result) { setAdding(false); setV({ amount: '', number: '', due: inDays(30), draft: false }) }
    })
  }
  if (!invoices.length && deal.status !== 'won' && !adding) {
    return h('div', { className: 'stoa-log' }, h('div', { className: 'stoa-sechead', style: { padding: 0, alignItems: 'center' } },
      h('small', { className: 'stoa-eyebrow' }, 'Money'), h(Btn, { kind: 'quiet', onClick: () => setAdding(true) }, '+  Invoice')))
  }
  const line = [money.invoiced ? 'invoiced ' + exact(money.invoiced, pb.currency) : '', money.paid ? 'paid ' + exact(money.paid, pb.currency) : '',
    money.owed ? 'owed ' + exact(money.owed, pb.currency) : '', deal.status === 'won' && money.uninvoiced ? exact(money.uninvoiced, pb.currency) + ' not invoiced' : '']
    .filter(Boolean).join(' · ')
  return h('div', { className: 'stoa-log' },
    h('div', { className: 'stoa-sechead', style: { padding: 0, alignItems: 'center' } },
      h('small', { className: 'stoa-eyebrow' }, 'Money'), h(Btn, { kind: 'quiet', onClick: () => setAdding(!adding) }, adding ? 'Close' : '+  Invoice')),
    line ? h('div', { className: 'stoa-sub', style: { marginBottom: 2 } }, cap(line)) : null,
    deal.value ? h(BillBar, { value: deal.value, invoiced: money.invoiced, paid: money.paid }) : null,
    invoices.map(invoice => h(Invoice, { key: invoice.id, invoice, dealId: deal.id, currency: pb.currency })),
    adding ? h('div', null,
      h('div', { className: 'stoa-form', style: { marginTop: 8 } },
        h(Field, { label: 'Amount', type: 'number', min: 0, step: 'any', autoFocus: true, value: v.amount, onChange: set('amount') }),
        h(Field, { label: 'Number', value: v.number, placeholder: '1042', onChange: set('number') }),
        h(Field, { label: 'Due', type: 'date', value: v.due, onChange: set('due') }),
        h('label', { style: { alignSelf: 'end', display: 'flex', gap: 6, alignItems: 'center', height: 28, fontSize: 12 } },
          h('input', { type: 'checkbox', checked: v.draft, onChange: event => setV({ ...v, draft: event.target.checked }) }), 'Not sent yet')),
      h('div', { className: 'stoa-btns', style: { marginTop: 8 } },
        h(Btn, { kind: 'primary', disabled: busy || !(Number(v.amount) > 0), onClick: add }, 'Add invoice'),
        h('span', { className: 'stoa-sub', style: { alignSelf: 'center' } }, 'Stoa keeps the record. It does not send the invoice.'))) : null)
}

/** How much of a record's value has been billed, and how much of that has come in. */
function BillBar({ value, invoiced, paid }) {
  const of = Math.max(value || 0, invoiced || 0, 1)
  return h('div', { className: 'stoa-billbar', title: 'Paid, then invoiced, out of the full amount' },
    h('i', { 'data-kind': 'invoiced', style: { width: Math.min(100, (100 * (invoiced || 0)) / of) + '%' } }),
    h('i', { 'data-kind': 'paid', style: { width: Math.min(100, (100 * (paid || 0)) / of) + '%' } }))
}

const BASIS = {
  history: 'Each stage counts for how often records there have gone on to close on this desk.',
  playbook: 'Each stage counts for the odds your playbook gives it. Your own history takes over once twelve have closed.',
  even: 'Each stage counts for an even step toward closing. Your own history takes over once twelve have closed.'
}

/** The Cash tab: what came in and what is owed, what the open pipeline is likely to bring, and where won work stands. */
function Cash({ state, openId, setOpenId, openClient }) {
  const cash = state.cash, pb = state.playbook
  const [over, setOver] = useState('')
  if (!cash) return h('p', { className: 'stoa-sub' }, 'The money side needs the gateway restarted after this update (hermes gateway restart).')
  const strip = cash.strip, forecast = cash.forecast, lane = cash.delivery
  const records = pb.billing !== 'none'          // records themselves are billed here (not only clients by the month)
  const toBill = cash.to_bill || []
  const tiles = [
    [pb.won.label + ' this month', strip.won_month.value, plural(strip.won_month.count, pb.terms.deal, pb.terms.deals), '', 'What was closed as ' + pb.won.label.toLowerCase() + ' since the first of the month.'],
    ['Collected this month', strip.collected_month.value, 'payments recorded', 'good', 'Every payment recorded on an invoice this month.'],
    ['Invoiced, not yet paid', strip.owed.value, plural(strip.owed.count, 'invoice', 'invoices'), '', 'What is still owed on invoices that have been sent.'],
    ['Overdue', strip.overdue.value, plural(strip.overdue.count, 'invoice', 'invoices') + ' past due', strip.overdue.count ? 'bad' : '', 'Owed on invoices whose due date has passed.'],
    [pb.won.label + ', not invoiced', strip.uninvoiced.value, plural(strip.uninvoiced.count, pb.terms.deal, pb.terms.deals), strip.uninvoiced.count ? 'warn' : '', 'Won work whose invoices add up to less than its value.'],
    ['To bill', (strip.to_bill || { value: 0 }).value, plural((strip.to_bill || { count: 0 }).count, 'month', 'months') + ' ended, not billed', (strip.to_bill || { count: 0 }).count ? 'warn' : '', 'Months that have ended for clients billed by the month.']]
    .filter((tile, index) => (records || (index !== 0 && index !== 4)) && (index !== 5 || toBill.length || (cash.invoices || []).some(item => item.client)))
  const most = Math.max(1, ...forecast.buckets.map(bucket => bucket.value))
  const shown = forecast.buckets.filter(bucket => bucket.count)
  const stages = lane.stages.length ? lane.stages : [{ id: '', label: pb.won.label }]
  const invoiceProps = invoice => invoice.client
    ? { key: invoice.id, invoice, client: invoice.client, currency: pb.currency, name: invoice.client + (invoice.period ? ' · ' + monthLabel(invoice.period) : ''), onOpen: () => openClient(invoice.client) }
    : { key: invoice.id, invoice, dealId: invoice.deal, currency: pb.currency, name: invoice.name, onOpen: () => setOpenId(invoice.deal) }
  const owing = cash.invoices.filter(invoice => invoice.state !== 'paid')
  const settled = cash.invoices.filter(invoice => invoice.state === 'paid')
  const drop = (event, stage) => {
    const id = event.dataTransfer.getData(DRAG)
    setOver('')
    if (!id || !stage.id) return
    event.preventDefault()
    const record = lane.records.find(item => item.id === id)
    if (record && record.delivery !== stage.id) act(id, { verb: 'update', delivery: stage.id })
  }
  return h('div', { className: 'stoa-cash' },
    h('div', { className: 'stoa-strip' }, tiles.map(tile => h('div', { key: tile[0], className: 'stoa-tile', 'data-tone': tile[3], title: tile[4] },
      h('span', { className: 'stoa-eyebrow' }, tile[0]), h('b', null, money(tile[1], pb.currency) || '0'), h('span', { className: 'stoa-sub' }, tile[2])))),
    toBill.length ? h('div', { className: 'stoa-h2' }, 'To bill') : null,
    toBill.length ? h('div', { style: { maxWidth: 980 } }, toBill.map(row => h(BillMonth, { key: row.client + row.period, row, pb, showClient: true, openClient }))) : null,
    records ? h('div', { className: 'stoa-h2' }, 'Likely to come in') : null,
    records ? h('div', { className: 'stoa-box' },
      h('div', { style: { display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', gap: '4px 14px' } },
        h('span', { className: 'stoa-name', style: { fontSize: 18 } }, money(forecast.weighted, pb.currency) || '0'),
        h('span', { className: 'stoa-sub' }, 'weighted, of ' + (money(forecast.value, pb.currency) || '0') + ' open')),
      shown.length ? h('div', { style: { marginTop: 10 } }, shown.map(bucket => h('div', { key: bucket.key, className: 'stoa-fc', 'data-key': bucket.key },
        h('span', { className: 'stoa-clip' }, bucket.label),
        h('u', null, h('i', { style: { width: (100 * bucket.value) / most + '%' } }), h('b', { style: { width: (100 * bucket.weighted) / most + '%' } })),
        h('span', { className: 'stoa-num' }, (money(bucket.weighted, pb.currency) || '0') + ' of ' + (money(bucket.value, pb.currency) || '0') + ' · ' + bucket.count))))
        : h('div', { className: 'stoa-sub', style: { marginTop: 6 } }, 'Nothing is open.'),
      h('div', { className: 'stoa-sub', style: { marginTop: 10 } }, BASIS[forecast.basis] + ' A ' + pb.terms.deal + ' goes in a month by its expected close date (Edit on the record).'),
      h('div', { className: 'stoa-chain', style: { margin: '6px 0 0' } }, forecast.odds.map(item => item.label + ' ' + item.odds + '%').join('  ·  '))) : null,
    records ? h('div', { className: 'stoa-h2' }, 'After the win') : null,
    !records ? null : lane.records.length ? h('div', { className: 'stoa-board', style: { gridAutoColumns: 'minmax(200px, 1fr)' } }, stages.map(stage => {
      const here = lane.records.filter(record => (record.delivery || '') === stage.id)
      return h('div', { key: stage.id || 'won', className: 'stoa-col', 'data-over': over === stage.id && stage.id ? '1' : '0',
        onDragOver: event => { if (dragging(event) && stage.id) { event.preventDefault(); if (over !== stage.id) setOver(stage.id) } },
        onDragLeave: event => { if (!event.currentTarget.contains(event.relatedTarget)) setOver('') },
        onDrop: event => drop(event, stage) },
      h('div', { className: 'stoa-colhead' }, h('span', { className: 'stoa-eyebrow stoa-clip' }, stage.label), h('span', { className: 'stoa-num' }, String(here.length))),
      h('div', { className: 'stoa-weight' }),
      here.length ? here.map(record => h('button', { type: 'button', key: record.id, className: 'stoa-card', 'data-open': openId === record.id ? '1' : '0',
        draggable: Boolean(stage.id), title: stage.id ? 'Drag to another column as the work moves on' : undefined,
        onDragStart: event => { event.dataTransfer.setData(DRAG, record.id); event.dataTransfer.effectAllowed = 'move' }, onDragEnd: () => setOver(''),
        onClick: () => setOpenId(openId === record.id ? '' : record.id) },
      h('h5', null, h('span', { className: 'stoa-clip' }, record.name), record.value ? h('span', { className: 'stoa-num' }, money(record.value, pb.currency)) : null),
      h('div', { className: 'stoa-sub stoa-clip' }, record.company || ' '),
      record.value ? h(BillBar, { value: record.value, invoiced: record.invoiced, paid: record.paid }) : null,
      h('div', { className: 'stoa-chips', style: { marginTop: 6 } },
        record.overdue ? h(Chip, { tone: 'bad' }, 'invoice overdue') : null,
        !record.invoiced && record.value ? h(Chip, { tone: 'warn' }, 'not invoiced')
          : record.uninvoiced ? h(Chip, null, (money(record.uninvoiced, pb.currency) || '') + ' to bill') : null,
        record.value && record.paid >= record.value ? h(Chip, { tone: 'good' }, 'paid') : record.owed && !record.overdue ? h(Chip, null, 'awaiting payment') : null)))
        : h('div', { className: 'stoa-empty' }, over === stage.id && stage.id ? 'Drop it here' : '—'))
    })) : h('p', { className: 'stoa-sub' }, 'Nothing has been ' + pb.won.label.toLowerCase() + ' yet. When something is, it lands here: where the work stands, and whether it has been billed.'),
    h('div', { className: 'stoa-h2' }, 'Invoices'),
    owing.length || settled.length ? h('div', { style: { maxWidth: 980 } },
      owing.map(invoice => h(Invoice, invoiceProps(invoice))),
      settled.length ? h('div', { className: 'stoa-eyebrow', style: { margin: '14px 0 2px' } }, 'Lately paid') : null,
      settled.map(invoice => h(Invoice, invoiceProps(invoice))))
      : h('p', { className: 'stoa-sub' }, 'No invoices yet. Open a record and press + Invoice, or tell Stoa: “I invoiced Kestrel 12k today, net 30.”'))
}

// -- a record, a group, a company: each on a page of its own, laid out the way a CRM is read ----------
// Across the top: what it is, where it stands, and what to do next. Below it, three columns: the details on the
// left, what happened in the middle, and what it is connected to on the right. On a narrow screen they stack.

const isEmpty = value => value === null || value === undefined || value === ''
const sameName = (a, b) => Boolean(a) && Boolean(b) && String(a).trim().toLowerCase() === String(b).trim().toLowerCase()
const dayLabel = stamp => {
  const d = new Date(stamp)
  if (isNaN(d)) return ''
  const now = new Date()
  if (d.toDateString() === now.toDateString()) return 'Today'
  if (d.toDateString() === new Date(Date.now() - 864e5).toDateString()) return 'Yesterday'
  return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' })
}
const clock = stamp => { const d = new Date(stamp); return isNaN(d) ? '' : d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }) }
const daysSince = stamp => { const then = Date.parse(stamp || ''); return then ? Math.max(0, Math.floor((Date.now() - then) / 864e5)) : null }
const webLink = url => (/^https?:\/\//i.test(url) ? url : 'https://' + url)
const bareLink = url => String(url || '').replace(/^https?:\/\/(www\.)?/i, '').replace(/\/$/, '')

function BackRow({ back, label }) {
  return h('div', { className: 'stoa-backrow' },
    h('button', { type: 'button', className: 'stoa-back', onClick: back, title: 'Back to ' + label }, h('span', { 'aria-hidden': true }, '‹'), ' ' + label))
}

function Section({ title, aside, children, main }) {
  return h('section', { className: 'stoa-box stoa-rpsec', 'data-main': main ? '1' : '0' },
    h('div', { className: 'stoa-rpsechead' }, h('h3', null, title), aside || null),
    children)
}

/** Label and value, one under the other; an empty one says so, so you can see what is not known yet. */
function Facts({ rows }) {
  return h('dl', { className: 'stoa-dl' }, rows.flatMap(row => [
    h('dt', { key: row[0] + ':k' }, row[0]),
    h('dd', { key: row[0] + ':v', 'data-empty': isEmpty(row[1]) ? '1' : '0' }, isEmpty(row[1]) ? '—' : row[1])]))
}

/** Records in a short list: name, where it stands, the next step, and what it is worth. */
function RecordList({ rows, pb, openRecord, showCompany, underStage }) {
  return h('div', { className: 'stoa-rlist' }, rows.map(record => {
    const open = record.status === 'open'
    const due = open && record.next_due && record.next_due <= today()
    return h('button', { type: 'button', key: record.id, className: 'stoa-rrow', 'data-closed': open ? '0' : '1', onClick: () => openRecord(record.id) },
      h('span', { className: 'stoa-grow' },
        h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, record.name,
          showCompany && record.company && !sameName(record.company, record.name) ? h('span', { className: 'stoa-sub' }, '  ' + record.company) : null),
        h('span', { className: 'stoa-sub stoa-clip', style: { display: 'block' } },
          [underStage && open ? '' : record.stage_label, open ? record.next_step || (underStage ? 'Nothing planned' : '') : ''].filter(Boolean).join(' · '))),
      due ? h(Chip, { tone: record.next_due < today() ? 'bad' : 'warn' }, record.next_due < today() ? 'overdue' : 'today') : null,
      record.value ? h('span', { className: 'stoa-num' }, money(record.value, pb.currency)) : null)
  }))
}

/** The stages as one bar, the record's place on it plain to see. A stage pressed moves it there (and can be taken back). */
function Path({ deal, pb, busy, run }) {
  const open = deal.status === 'open'
  const end = deal.status === 'won' ? ['won', pb.won.label] : deal.status === 'lost' ? ['lost', pb.lost.label + (deal.lost_reason ? ': ' + deal.lost_reason : '')] : ['todo', pb.won.label]
  return h('div', { className: 'stoa-path', role: 'list', 'aria-label': 'Stages' },
    pb.stages.map((stage, index) => {
      const at = deal.status === 'won' || (open && index < deal.position) ? 'done' : open && index === deal.position ? 'now' : 'todo'
      return h('button', { type: 'button', key: stage.id, role: 'listitem', 'data-state': at, disabled: busy || !open || at === 'now', style: heatAt(index, pb.stages.length + 1),
        title: open && at !== 'now' ? 'Move to ' + stage.label : stage.label, onClick: () => run({ verb: 'move', stage: stage.id }) }, h('span', null, stage.label))
    }),
    h('span', { className: 'stoa-pathend', role: 'listitem', 'data-state': end[0], style: heatAt(pb.stages.length, pb.stages.length + 1) }, h('span', null, end[1])))
}

/** Every fact on a record, in one form: the playbook's own fields included. */
function DetailsForm({ deal, pb, state, done }) {
  const terms = pb.terms, open = deal.status === 'open'
  const base = { name: deal.name || '', company: deal.company || '', group: deal.group || '', email: deal.email || '', phone: deal.phone || '',
    linkedin: deal.linkedin || '', owner: deal.owner || '', value: deal.value ? String(deal.value) : '', close_on: deal.close_on || '', source: deal.source || '' }
  const baseFields = Object.fromEntries(pb.fields.map(field => [field.key, String((deal.fields || {})[field.key] || '')]))
  const [v, setV] = useState(base)
  const [f, setF] = useState(baseFields)
  const [busy, setBusy] = useState(false)
  const set = key => event => setV({ ...v, [key]: event.target.value })
  const save = () => {
    if (!v.name.trim() || busy) return
    const body = { verb: 'update' }
    for (const key of Object.keys(v)) {
      const now = String(v[key]).trim()
      if (now !== base[key]) body[key] = key === 'value' ? Number(now) || 0 : now
    }
    const fields = {}
    for (const key of Object.keys(f)) if (String(f[key]).trim() !== baseFields[key]) fields[key] = String(f[key]).trim()
    if (Object.keys(fields).length) body.fields = fields
    if (Object.keys(body).length === 1) return done()
    setBusy(true)
    act(deal.id, body).then(result => { setBusy(false); if (result) done() })
  }
  const enter = event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) save(); if (event.key === 'Escape') done() }
  return h('div', null,
    h('div', { className: 'stoa-form', 'data-one': '1' },
      h(Field, { label: 'Name', value: v.name, onChange: set('name'), onKeyDown: enter }),
      h(Field, { label: cap(terms.company), value: v.company, onChange: set('company'), onKeyDown: enter }),
      h(Field, { label: cap(terms.group), list: 'stoa-edit-groups', value: v.group, onChange: set('group'), onKeyDown: enter }),
      h(Field, { label: 'Email', type: 'email', value: v.email, onChange: set('email'), onKeyDown: enter }),
      h(Field, { label: 'Phone', type: 'tel', value: v.phone, onChange: set('phone'), onKeyDown: enter }),
      h(Field, { label: 'LinkedIn', value: v.linkedin, placeholder: 'linkedin.com/in/…', onChange: set('linkedin'), onKeyDown: enter }),
      h(Field, { label: cap(terms.owner), list: 'stoa-edit-owners', value: v.owner, onChange: set('owner'), onKeyDown: enter }),
      h(Field, { label: cap(terms.value), type: 'number', min: 0, value: v.value, onChange: set('value'), onKeyDown: enter }),
      open ? h(Field, { label: 'Expected close', type: 'date', value: v.close_on, title: 'The forecast puts it in this month', onChange: set('close_on') }) : null,
      pb.fields.map(field => h(Field, { key: field.key, label: field.label, value: f[field.key], onChange: event => setF({ ...f, [field.key]: event.target.value }), onKeyDown: enter })),
      h(Field, { label: 'Source', value: v.source, placeholder: 'Referral, LinkedIn, an event…', onChange: set('source'), onKeyDown: enter })),
    h('datalist', { id: 'stoa-edit-groups' }, uniq((state.deals || []).map(item => item.group)).map(name => h('option', { key: name, value: name }))),
    h('datalist', { id: 'stoa-edit-owners' }, uniq((state.deals || []).map(item => item.owner)).map(name => h('option', { key: name, value: name }))),
    h('div', { className: 'stoa-btns' },
      h(Btn, { kind: 'primary', disabled: busy || !v.name.trim(), onClick: save }, 'Save'),
      h(Btn, { kind: 'quiet', disabled: busy, onClick: done }, 'Cancel')))
}

function RecordDetails({ deal, pb, state, openGroup, openCompany }) {
  const [editing, setEditing] = useState(false)
  const terms = pb.terms, open = deal.status === 'open'
  const link = (text, onClick, title) => h('button', { type: 'button', className: 'stoa-proplink stoa-alink', title, onClick }, text)
  // Where an account or a client is the company, the two are one line, and it opens the one page about them.
  const oneLine = pb.group_is_company && deal.group && (!deal.company || sameName(deal.company, deal.group))
  const rows = [
    oneLine ? [cap(terms.group), link(deal.group, () => openGroup(deal.group), 'Everything about ' + deal.group)]
      : [cap(terms.company), deal.company ? link(deal.company, () => (pb.group_is_company && deal.group ? openGroup(deal.group) : openCompany(deal.company)), 'Everything at ' + deal.company) : ''],
    oneLine ? null : [cap(terms.group), deal.group ? link(deal.group, () => openGroup(deal.group), 'The ' + terms.group + ' it is in') : ''],
    ['Email', deal.email ? h('a', { className: 'stoa-alink', href: 'mailto:' + deal.email }, deal.email) : ''],
    ['Phone', deal.phone ? h('a', { className: 'stoa-alink', href: 'tel:' + deal.phone.replace(/[^+\d]/g, '') }, deal.phone) : ''],
    ['LinkedIn', deal.linkedin ? h('a', { className: 'stoa-alink', href: webLink(deal.linkedin), target: '_blank', rel: 'noreferrer' }, bareLink(deal.linkedin)) : ''],
    [cap(terms.owner), deal.owner],
    [cap(terms.value), money(deal.value, pb.currency)],
    open ? ['Expected close', deal.close_on ? shortDate(deal.close_on) + (deal.close_on < today() ? ' (past)' : '') : ''] : null]
    .concat(pb.fields.map(field => [field.label, (deal.fields || {})[field.key]]))
    .concat([['Source', deal.source], ['Added', deal.created_at ? shortDate(deal.created_at.slice(0, 10)) : '']])
    .filter(Boolean)
  return h(Section, { title: 'Details', aside: h(Btn, { kind: 'quiet', onClick: () => setEditing(!editing) }, editing ? 'Close' : 'Edit') },
    editing ? h(DetailsForm, { deal, pb, state, done: () => setEditing(false) }) : h(Facts, { rows }))
}

const LOG_PLACEHOLDER = { note: 'A thought, something to remember…', call: 'What was said on the call…', email: 'What the email said…', meeting: 'What came out of the meeting…' }

/** What happened, newest first, a day at a time, with a line at the top to add to it. */
function Timeline({ deal, sessionId }) {
  const [kind, setKind] = useState('call')
  const [line, setLine] = useState('')
  const [saving, setSaving] = useState(false)
  const [undoing, setUndoing] = useState(false)
  const add = () => {
    const text = line.trim()
    if (!text || saving) return
    setSaving(true)
    act(deal.id, { verb: 'log', kind, text }, sessionId).then(result => { setSaving(false); if (result) setLine('') })
  }
  const entries = deal.activity || []
  let day = ''
  const items = []
  entries.forEach((entry, index) => {
    const label = dayLabel(entry.at)
    if (label !== day) { day = label; items.push(h('li', { key: 'day' + index, className: 'stoa-tlday' }, label)) }
    items.push(h('li', { key: index, className: 'stoa-tl', 'data-kind': entry.kind, 'data-undone': entry.undone ? '1' : '0' },
      h('span', { className: 'stoa-tlkind' }, LOG_KINDS[entry.kind] || cap(entry.kind)),
      h('div', { className: 'stoa-grow' },
        h('div', { className: 'stoa-tltext' }, entry.text),
        h('div', { className: 'stoa-sub' }, [clock(entry.at), entry.by, entry.undone ? 'taken back' : ''].filter(Boolean).join(' · ')))))
  })
  return h(Section, { main: true, title: 'Activity',
    aside: deal.undo ? h(Btn, { kind: 'quiet', disabled: undoing, title: 'Put the record back as it was before: ' + deal.undo.text,
      onClick: () => { setUndoing(true); undo(deal.id, deal.undo.op).then(() => setUndoing(false)) } },
    h('span', { style: { display: 'inline-flex', alignItems: 'center', gap: 6 } }, h(Icon, { name: 'back', size: 13 }), 'Take back the last change')) : null },
  h('div', { className: 'stoa-logbox' },
    h('select', { className: 'stoa-select', value: kind, 'aria-label': 'What it was', onChange: event => setKind(event.target.value) },
      [['call', 'Call'], ['email', 'Email'], ['meeting', 'Meeting'], ['note', 'Note']].map(pair => h('option', { key: pair[0], value: pair[0] }, pair[1]))),
    h('input', { className: 'stoa-field', value: line, disabled: saving, placeholder: LOG_PLACEHOLDER[kind], 'aria-label': 'Add to the log',
      onChange: event => setLine(event.target.value), onKeyDown: event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); add() } } }),
    h(Btn, { kind: 'primary', disabled: saving || !line.trim(), onClick: add }, 'Log it')),
  items.length ? h('ol', { className: 'stoa-timeline' }, items) : h('p', { className: 'stoa-sub' }, 'Nothing logged yet.'))
}

/** What the record is connected to: the group it is in and the others there, others at the same company, its people, its money. */
function RecordRelated({ deal, pb, state, openRecord, openGroup, openCompany, openClient }) {
  const deals = state.deals || []
  const inGroup = deal.group ? deals.filter(item => item.id !== deal.id && sameName(item.group, deal.group)) : []
  // Others at the same company that the group above does not already show (a candidate's colleagues, say).
  const atCompany = !pb.group_is_company && deal.company
    ? deals.filter(item => item.id !== deal.id && sameName(item.company, deal.company) && !(deal.group && sameName(item.group, deal.group)))
    : []
  const sortOpen = rows => rows.slice().sort((a, b) => (a.status !== 'open') - (b.status !== 'open') || (b.position || 0) - (a.position || 0) || a.name.localeCompare(b.name))
  const billed = pb.billing !== 'none' && (deal.status === 'won' || (deal.invoices || []).length > 0)
  const groupSub = deal.client || deal.group_status ? h('span', null,
    deal.client ? ['for ', h('button', { key: 'c', type: 'button', className: 'stoa-proplink', onClick: () => openClient(deal.client) }, deal.client)] : null,
    deal.group_status ? (deal.client ? ' · ' : '') + deal.group_status.toLowerCase() : null) : null
  return h('div', { className: 'stoa-rpcol' },
    deal.group ? h(Section, { title: cap(pb.terms.group), aside: h('button', { type: 'button', className: 'stoa-proplink stoa-sub', onClick: () => openGroup(deal.group) }, 'Open  ›') },
      h('button', { type: 'button', className: 'stoa-proplink stoa-name', style: { fontSize: 14 }, onClick: () => openGroup(deal.group) }, deal.group),
      groupSub ? h('div', { className: 'stoa-sub' }, groupSub) : null,
      inGroup.length ? h('div', { style: { marginTop: 10 } },
        h('small', { className: 'stoa-eyebrow' }, 'Also in it'),
        h(RecordList, { rows: sortOpen(inGroup).slice(0, 6), pb, openRecord, showCompany: !pb.group_is_company }),
        inGroup.length > 6 ? h('button', { type: 'button', className: 'stoa-proplink stoa-sub', onClick: () => openGroup(deal.group) }, 'and ' + (inGroup.length - 6) + ' more  ›') : null)
        : h('div', { className: 'stoa-sub', style: { marginTop: 6 } }, 'Nothing else in it yet.')) : null,
    atCompany.length ? h(Section, { title: 'Also at ' + deal.company, aside: h('button', { type: 'button', className: 'stoa-proplink stoa-sub', onClick: () => openCompany(deal.company) }, 'Open  ›') },
      h(RecordList, { rows: sortOpen(atCompany).slice(0, 5), pb, openRecord })) : null,
    h('section', { className: 'stoa-box stoa-rpsec' }, h(People, { deal, term: pb.terms.deal, title: 'Other people' })),
    billed ? h('section', { className: 'stoa-box stoa-rpsec' }, h(Money, { deal, pb })) : null)
}

function RecordPage({ id, state: shown, sessionId, back, backLabel, openRecord, openGroup, openCompany, openClient }) {
  const query = useQuery({ queryKey: ['stoa', 'deal', id], queryFn: () => rest('/deal/' + encodeURIComponent(id)), staleTime: 1500 })
  const [busy, setBusy] = useState(false)
  const [nextForm, setNextForm] = useState('') // '', 'done' or 'plan'
  const deal = query.data
  if (query.error) return h('div', null, h(BackRow, { back, label: backLabel }), h('div', { className: 'stoa-box stoa-sub' }, 'That record is no longer on the desk.'))
  if (!deal) return h('div', null, h(BackRow, { back, label: backLabel }), h('div', { className: 'stoa-sub' }, 'Loading…'))
  const elsewhere = Boolean(deal.pipeline && shown.pipeline && deal.pipeline.id !== shown.pipeline)
  const state = deal.playbook ? { ...shown, playbook: deal.playbook } : shown
  const pb = state.playbook, terms = pb.terms
  const open = deal.status === 'open'
  const run = body => { setBusy(true); act(id, body, sessionId).then(() => setBusy(false)) }
  const inStage = daysSince(deal.stage_at)
  const oneLine = pb.group_is_company && deal.group && (!deal.company || sameName(deal.company, deal.group))
  // Where it sits: "at" its company, "in" its group (and who that group is for, when the name does not already say).
  const where = []
  const forClient = deal.client && !pb.group_is_company
    ? h('span', null, ', for ', h('button', { type: 'button', className: 'stoa-proplink', onClick: () => openClient(deal.client) }, deal.client)) : null
  if (oneLine) where.push(h('span', { key: 'g' }, h('button', { type: 'button', className: 'stoa-proplink', onClick: () => openGroup(deal.group) }, deal.group)))
  else {
    if (deal.company) where.push(h('span', { key: 'c' }, 'at ', h('button', { type: 'button', className: 'stoa-proplink', onClick: () => (pb.group_is_company && deal.group ? openGroup(deal.group) : openCompany(deal.company)) }, deal.company)))
    if (deal.group) where.push(h('span', { key: 'g' }, 'in ', h('button', { type: 'button', className: 'stoa-proplink', onClick: () => openGroup(deal.group) }, deal.group), forClient))
  }
  const status = deal.status === 'won' ? pb.won.label + (deal.delivery_label ? ' · ' + deal.delivery_label : '') : deal.status === 'lost' ? pb.lost.label : ''
  return h('div', { className: 'stoa-rp' },
    h(BackRow, { back, label: backLabel }),
    h('div', { className: 'stoa-rphead' },
      h('div', { className: 'stoa-grow' },
        h('div', { className: 'stoa-eyebrow' }, [elsewhere ? deal.pipeline.name : '', cap(terms.deal)].filter(Boolean).join(' · ')),
        h('h2', { className: 'stoa-rptitle' }, deal.name),
        where.length ? h('div', { className: 'stoa-rpwhere' }, where.flatMap((item, index) => (index ? [h('span', { key: 's' + index, className: 'stoa-rpsep', 'aria-hidden': true }, ' / '), item] : [item]))) : null,
        h('div', { className: 'stoa-chips' },
          status ? h(Chip, { tone: deal.status === 'won' ? 'good' : '' }, status) : null,
          deal.due ? h(Chip, { tone: dueTone(deal) }, deal.next_due < today() ? 'overdue' : 'due today') : null,
          deal.stale && !deal.due ? h(Chip, null, 'quiet') : null,
          asleep(deal) ? h(Chip, null, 'snoozed to ' + shortDate(deal.snoozed_until)) : null,
          deal.pending_task ? h(Chip, { tone: 'good' }, 'draft waiting') : null)),
      h('div', { className: 'stoa-rpfig' },
        deal.value ? h('b', null, money(deal.value, pb.currency)) : null,
        h('span', { className: 'stoa-sub' }, [deal.owner, open && inStage !== null ? (inStage === 0 ? 'in this stage since today' : inStage + 'd in this stage') : ''].filter(Boolean).join(' · ')))),
    h(Path, { deal, pb, busy, run }),
    h('div', { className: 'stoa-rpbar' },
      h('div', { className: 'stoa-rpnext' },
        h('small', { className: 'stoa-eyebrow' }, 'Next step'),
        open ? (deal.next_step
          ? h('div', { className: 'stoa-rpnextline' }, h('span', { className: 'stoa-name' }, deal.next_step), deal.next_due ? h('span', { className: 'stoa-num', 'data-tone': dueTone(deal) }, dueLabel(deal)) : null)
          : h('div', { className: 'stoa-sub' }, 'Nothing planned.'))
          : h('div', { className: 'stoa-sub' }, (deal.status === 'won' ? pb.won.label : pb.lost.label + (deal.lost_reason ? ': ' + deal.lost_reason : '')) + '. Nothing more to do here unless it is reopened.')),
      h('div', { className: 'stoa-btns', style: { marginTop: 0 } },
        open && deal.next_step ? h(Btn, { disabled: busy, title: 'The next step is done: log it and say what comes next', onClick: () => setNextForm(nextForm === 'done' ? '' : 'done') }, 'Done') : null,
        open ? h(Btn, { disabled: busy, onClick: () => setNextForm(nextForm === 'plan' ? '' : 'plan') }, deal.next_step ? 'Change' : 'Plan the next step') : null,
        open ? h(Btn, { disabled: busy, onClick: () => run({ verb: 'draft' }) }, 'Draft follow-up') : null,
        open ? h(Snooze, { deal, busy, run }) : null,
        open ? h('select', { className: 'stoa-select', value: '', disabled: busy, 'aria-label': pb.lost.label,
          onChange: event => { if (event.target.value !== '') run({ verb: 'lost', extra: event.target.value }) } },
        [h('option', { key: 'none', value: '' }, pb.lost.label + '…')].concat(pb.lost.reasons.map((reason, index) => h('option', { key: index, value: String(index) }, reason)))) : null,
        deal.status === 'won' && (deal.delivery_stages || []).length ? h('select', { className: 'stoa-select', value: deal.delivery || '', disabled: busy,
          'aria-label': 'Where the work stands', title: 'Where the work stands', onChange: event => run({ verb: 'update', delivery: event.target.value }) },
        deal.delivery_stages.map(stage => h('option', { key: stage.id, value: stage.id }, stage.label))) : null,
        !open ? h(Btn, { disabled: busy, onClick: () => run({ verb: 'reopen' }) }, 'Reopen') : null,
        open && deal.advance ? h(Btn, { kind: 'primary', disabled: busy, onClick: () => run({ verb: 'advance', extra: deal.stage }) }, deal.advance + '  →') : null)),
    nextForm && open ? h('div', { className: 'stoa-box', style: { marginTop: 10 } }, h(NextStep, { deal, mode: nextForm === 'done' && deal.next_step ? 'done' : 'plan', close: () => setNextForm('') })) : null,
    h('div', { className: 'stoa-rpgrid' },
      h('div', { className: 'stoa-rpcol' }, h(RecordDetails, { deal, pb, state, openGroup, openCompany })),
      h('div', { className: 'stoa-rpcol' },
        h(Section, { title: 'Notes' }, h(RecordNotes, { key: 'notes-' + deal.id, deal, bare: true })),
        h(Timeline, { deal, sessionId })),
      h(RecordRelated, { deal, pb, state, openRecord, openGroup, openCompany, openClient })))
}

/** The records in a group or at a company, a stage at a time in the playbook's order, the closed ones last. */
function ByStage({ records, pb, openRecord, showCompany }) {
  const sections = pb.stages.map(stage => [stage.label, records.filter(record => record.status === 'open' && record.stage === stage.id)])
    .concat([[pb.won.label, records.filter(record => record.status === 'won')], [pb.lost.label, records.filter(record => record.status === 'lost')]])
    .filter(pair => pair[1].length)
  if (!sections.length) return h('p', { className: 'stoa-sub' }, 'None yet.')
  return h('div', null, sections.map(pair => h('div', { key: pair[0], className: 'stoa-stagegroup' },
    h('div', { className: 'stoa-stagehead' }, h('span', { className: 'stoa-eyebrow' }, pair[0]),
      h('span', { className: 'stoa-sub' }, [String(pair[1].length), money(pair[1].reduce((sum, record) => sum + (record.value || 0), 0), pb.currency)].filter(Boolean).join(' · '))),
    h(RecordList, { rows: pair[1], pb, openRecord, showCompany, underStage: true }))))
}

/** Someone known there: their name, what they are, where they are on the desk. */
function PersonLine({ person, openRecord, remove, busy }) {
  const where = person.primary ? '' : person.on === 'record' ? 'on ' + person.record_name : ''
  return h('div', { className: 'stoa-person' },
    h('span', { className: 'stoa-grow' },
      person.record && person.primary
        ? h('button', { type: 'button', className: 'stoa-proplink stoa-name', onClick: () => openRecord(person.record) }, person.name)
        : h('span', { className: 'stoa-name' }, person.name),
      person.role || person.title ? h('span', { className: 'stoa-sub' }, '  ' + [person.role, person.title].filter(Boolean).join(' · ')) : null,
      person.email || person.phone || where ? h('span', { className: 'stoa-sub', style: { display: 'block', overflowWrap: 'anywhere' } },
        [person.email, person.phone, where].filter(Boolean).join(' · ')) : null),
    remove ? h('button', { type: 'button', className: 'stoa-icon', style: { width: 24, height: 24 }, disabled: busy, title: 'Take ' + person.name + ' off',
      'aria-label': 'Remove ' + person.name, onClick: remove }, h(Icon, { name: 'x', size: 12 })) : null)
}

/** Recent activity across several records, each line naming its record. */
function Lately({ entries, openRecord }) {
  if (!entries.length) return h('p', { className: 'stoa-sub' }, 'Nothing logged yet.')
  return h('ol', { className: 'stoa-timeline' }, entries.map((entry, index) => h('li', { key: index, className: 'stoa-tl', 'data-kind': entry.kind },
    h('span', { className: 'stoa-tlkind' }, LOG_KINDS[entry.kind] || cap(entry.kind)),
    h('div', { className: 'stoa-grow' },
      h('div', { className: 'stoa-tltext' }, h('button', { type: 'button', className: 'stoa-proplink stoa-name', onClick: () => openRecord(entry.deal) }, entry.name), '  ' + entry.text),
      h('div', { className: 'stoa-sub' }, [ago(entry.at), entry.by].filter(Boolean).join(' · '))))))
}

function GroupPage({ name, state, sessionId, back, backLabel, openRecord, openClient, onRenamed }) {
  const query = useQuery({ queryKey: ['stoa', 'group', name], queryFn: () => rest('/group', { method: 'POST', body: { name } }), staleTime: 1500 })
  const blank = { name: '', email: '', title: '', role: '', phone: '' }
  const [draft, setDraft] = useState(null) // null until something is edited
  const [editing, setEditing] = useState(false)
  const [note, setNote] = useState(null) // the notes, while being edited
  const [busy, setBusy] = useState(false)
  const [addingPerson, setAddingPerson] = useState(false)
  const [addingRecord, setAddingRecord] = useState(false)
  const [v, setV] = useState(blank)
  const data = query.data
  if (query.error) return h('div', null, h(BackRow, { back, label: backLabel }), h('div', { className: 'stoa-box stoa-sub' }, 'That one is no longer on the desk.'))
  if (!data) return h('div', null, h(BackRow, { back, label: backLabel }), h('div', { className: 'stoa-sub' }, 'Loading…'))
  const pb = state.playbook, group = data.group, term = pb.terms.group
  const company = data.company // set when a group here is a company: one place for both
  const was = { title: group.name, client: group.client, status: group.status, value: group.value ? String(group.value) : '', note: group.note, fields: {},
    domain: company ? company.domain : '' }
  const now = draft || was
  const set = patch => setDraft({ ...now, ...patch })
  const fact = item => (item.key in now.fields ? now.fields[item.key] : item.value)
  const send = (path, body, said) => {
    setBusy(true)
    return rest(path, { method: 'POST', body })
      .then(result => { refresh(); if (said) host.notify({ kind: 'success', message: said }); return result })
      .catch(error => { host.notifyError(error, 'Stoa could not save that'); return null })
      .then(result => { setBusy(false); return result })
  }
  const saveNote = () => send('/group', { name: group.name, note: note }, 'Notes saved on ' + group.name + '.').then(result => { if (result) setNote(null) })
  const save = async () => {
    const body = { name: group.name, status: now.status, fields: now.fields }
    if (!company) body.client = now.client
    if (now.value.trim() !== was.value) body.value = now.value.trim() === '' ? 0 : now.value.trim()
    if (company && now.domain.trim().toLowerCase() !== (company.domain || '')) { body.company = company.name; body.domain = now.domain.trim() }
    const saved = await send('/group', body, 'Saved on ' + group.name + '.')
    if (!saved) return
    const title = now.title.trim()
    setDraft(null)
    setEditing(false)
    if (title && title !== group.name) {
      const moved = await send('/group', { name: group.name, to: title }, 'Renamed to ' + title + '.')
      if (moved && onRenamed) onRenamed(moved.group.name)
    }
  }
  const addPerson = () => {
    if (!v.name.trim() || busy) return
    send('/group/person', { verb: 'add', group: group.name, ...v }).then(result => {
      if (!result) return
      if (result.exists) host.notify({ kind: 'info', message: v.name.trim() + ' is already here.' })
      setV(blank)
      setAddingPerson(false)
    })
  }
  const enter = event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) addPerson() }
  const field = key => event => setV({ ...v, [key]: event.target.value })
  const exportIt = share => send('/export', { group: group.name, share }).then(file => {
    if (!file) return
    download(file.filename, file.csv, 'text/csv')
    host.notify({ kind: 'success', message: plural(file.rows, pb.terms.deal, pb.terms.deals) + ' exported: only ' + group.name + (share ? ', without your own columns.' : '.') })
  })
  const stats = [['Open', data.open ? data.open + (data.open_value ? ' · ' + money(data.open_value, pb.currency) : '') : '0'],
    [pb.won.label, data.won ? data.won + (data.won_value ? ' · ' + money(data.won_value, pb.currency) : '') : '0'],
    [pb.lost.label, String(data.lost)], ['Last touch', ago(data.last_touch) || '—']]
  const sub = company ? [company.domain, group.status !== 'open' ? group.status_label.toLowerCase() : ''].filter(Boolean).join(' · ')
    : [group.client ? 'for ' + group.client : 'No client named yet', group.status !== 'open' ? group.status_label.toLowerCase() : ''].filter(Boolean).join(' · ')
  const ours = data.people.filter(person => person.on === 'group'), theirs = data.people.filter(person => person.on !== 'group')
  return h('div', { className: 'stoa-rp' },
    h(BackRow, { back, label: backLabel }),
    h('div', { className: 'stoa-rphead' },
      h('div', { className: 'stoa-grow' },
        h('div', { className: 'stoa-eyebrow' }, cap(term)),
        h('h2', { className: 'stoa-rptitle' }, group.name),
        !company && group.client ? h('div', { className: 'stoa-rpwhere' }, 'for ', h('button', { type: 'button', className: 'stoa-proplink', onClick: () => openClient(group.client) }, group.client),
          group.status !== 'open' ? h('span', { className: 'stoa-sub' }, '  ·  ' + group.status_label.toLowerCase()) : null)
          : sub ? h('div', { className: 'stoa-rpwhere stoa-sub' }, sub) : null),
      h('div', { className: 'stoa-rpstats' }, stats.map(pair => h('div', { key: pair[0] }, h('span', { className: 'stoa-eyebrow' }, pair[0]), h('b', null, pair[1]))))),
    h('div', { className: 'stoa-rpbar', 'data-plain': '1' },
      h('div', { className: 'stoa-btns', style: { marginTop: 0 } },
        h(Btn, { kind: 'primary', onClick: () => setAddingRecord(!addingRecord) }, addingRecord ? 'Close' : '+  New ' + pb.terms.deal + ' here'),
        data.records.length ? h(Btn, { disabled: busy, title: 'A CSV of the ' + pb.terms.deals + ' here and nothing else, with every column', onClick: () => exportIt(false) }, 'Export') : null,
        data.records.length && !company ? h(Btn, { disabled: busy, title: 'The copy to hand the client: no value, owner, next steps, dates or sources', onClick: () => exportIt(true) }, 'Copy to share') : null)),
    h('div', { className: 'stoa-rpgrid' },
      h('div', { className: 'stoa-rpcol' },
        h(Section, { title: 'Details', aside: h(Btn, { kind: 'quiet', onClick: () => { setEditing(!editing); setDraft(null) } }, editing ? 'Close' : 'Edit') },
          editing ? h('div', null,
            h('div', { className: 'stoa-form', 'data-one': '1', style: { marginTop: 0 } },
              h(Field, { label: 'Name', value: now.title, onChange: event => set({ title: event.target.value }) }),
              company ? h(Field, { label: 'Email domain', value: now.domain, placeholder: 'kestrel.com', title: 'Mail from anyone at this domain is recognised as coming from here',
                onChange: event => set({ domain: event.target.value }) })
                : h(Field, { label: 'Client', value: now.client, placeholder: 'The company it is for', title: 'The company that pays: not always the company on a ' + pb.terms.deal,
                  onChange: event => set({ client: event.target.value }) }),
              h(Field, { label: 'Status' },
                h('select', { className: 'stoa-field', value: now.status, onChange: event => set({ status: event.target.value }) },
                  GROUP_STATUS.map(pair => h('option', { key: pair[0], value: pair[0] }, pair[1])))),
              h(Field, { label: cap(pb.terms.value) + ' for each ' + pb.terms.deal, value: now.value, placeholder: 'Left blank: each says its own',
                title: 'A new ' + pb.terms.deal + ' here takes this unless it brings its own', onChange: event => set({ value: event.target.value }) }),
              (group.facts || []).map(item => h(Field, { key: item.key, label: item.label, value: fact(item), onChange: event => set({ fields: { ...now.fields, [item.key]: event.target.value } }) }))),
            h('div', { className: 'stoa-btns' }, h(Btn, { kind: 'primary', disabled: busy || !now.title.trim() || !draft, onClick: save }, 'Save'),
              h(Btn, { kind: 'quiet', disabled: busy, onClick: () => { setDraft(null); setEditing(false) } }, 'Cancel')))
            : h(Facts, { rows: [company ? ['Email domain', company.domain ? company.domain + (company.domain_known ? '' : ' (from their email)') : ''] : ['Client', group.client],
              ['Status', group.status_label], [cap(pb.terms.value) + ' for each ' + pb.terms.deal, group.value ? money(group.value, pb.currency) : '']]
              .concat((group.facts || []).map(item => [item.label, item.value])) })),
        h(Section, { title: 'Notes' },
          h('textarea', { className: 'stoa-field stoa-notes', style: { marginTop: 0 }, rows: 4, value: note === null ? group.note : note,
            placeholder: 'What to know about them: how they decide, what was agreed, what to avoid…', onChange: event => setNote(event.target.value) }),
          company && company.note && company.note !== (note === null ? group.note : note) ? h('p', { className: 'stoa-sub', style: { margin: '8px 0 0', whiteSpace: 'pre-line' } }, 'Kept on the company before: ' + company.note) : null,
          note !== null && note !== group.note ? h('div', { className: 'stoa-btns' }, h(Btn, { kind: 'primary', disabled: busy, onClick: saveNote }, 'Save notes'),
            h(Btn, { kind: 'quiet', disabled: busy, onClick: () => setNote(null) }, 'Cancel')) : null)),
      h('div', { className: 'stoa-rpcol' },
        addingRecord ? h(NewRecord, { state, preset: { group: group.name, company: company ? company.name : '' }, close: () => setAddingRecord(false),
          opened: id => { setAddingRecord(false); if (id) openRecord(id) } }) : null,
        h(Section, { main: true, title: cap(pb.terms.deals) + ' (' + data.records.length + ')' },
          h(ByStage, { records: data.records, pb, openRecord, showCompany: !company }))),
      h('div', { className: 'stoa-rpcol' },
        company && data.billing ? h(ClientBilling, { key: 'bill:' + group.name, name: group.name, billing: data.billing, pb }) : null,
        h(Section, { title: company ? 'People there' : 'On their side', aside: h(Btn, { kind: 'quiet', onClick: () => setAddingPerson(!addingPerson) }, addingPerson ? 'Close' : '+  Person') },
          ours.map(person => h(PersonLine, { key: 'g' + person.id, person, openRecord, busy,
            remove: () => send('/group/person', { verb: 'remove', group: group.name, person: String(person.id) }) })),
          theirs.map(person => h(PersonLine, { key: 'r' + person.id, person, openRecord })),
          !data.people.length && !addingPerson ? h('div', { className: 'stoa-sub' }, company ? 'Nobody yet.' : 'Nobody yet. Add the people you deal with there, and what they send is recognised.') : null,
          addingPerson ? h('div', null,
            h('div', { className: 'stoa-form', 'data-one': '1' },
              h(Field, { label: 'Name', autoFocus: true, value: v.name, onChange: field('name'), onKeyDown: enter }),
              h(Field, { label: 'Role here', value: v.role, placeholder: company ? 'Decision maker, finance…' : 'Hiring manager, sponsor…', onChange: field('role'), onKeyDown: enter }),
              h(Field, { label: 'Title', value: v.title, onChange: field('title'), onKeyDown: enter }),
              h(Field, { label: 'Email', type: 'email', value: v.email, onChange: field('email'), onKeyDown: enter }),
              h(Field, { label: 'Phone', type: 'tel', value: v.phone, onChange: field('phone'), onKeyDown: enter })),
            h('div', { className: 'stoa-btns' }, h(Btn, { kind: 'primary', disabled: busy || !v.name.trim(), onClick: addPerson }, 'Add'))) : null),
        h(Section, { title: 'Lately' }, h(Lately, { entries: data.activity || [], openRecord })))))
}

function CompanyPage({ name, state, back, backLabel, openRecord, openGroup }) {
  const query = useQuery({ queryKey: ['stoa', 'company', name], queryFn: () => rest('/company', { method: 'POST', body: { name } }), staleTime: 1500 })
  const [note, setNote] = useState(null) // null until edited
  const [domain, setDomain] = useState(null)
  const [busy, setBusy] = useState(false)
  const data = query.data
  if (query.error) return h('div', null, h(BackRow, { back, label: backLabel }), h('div', { className: 'stoa-box stoa-sub' }, 'Nothing on the desk is at that company any more.'))
  if (!data) return h('div', null, h(BackRow, { back, label: backLabel }), h('div', { className: 'stoa-sub' }, 'Loading…'))
  const pb = state.playbook, company = data.company || { name, domain: '', note: '' }
  const changed = (note !== null && note !== company.note) || (domain !== null && domain.trim().toLowerCase() !== company.domain)
  const save = () => {
    setBusy(true)
    rest('/company', { method: 'POST', body: { name: company.name, note: note === null ? company.note : note, domain: domain === null ? company.domain : domain } })
      .then(() => { refresh(); setNote(null); setDomain(null); host.notify({ kind: 'success', message: 'Saved on ' + company.name + '.' }) })
      .catch(error => host.notifyError(error, 'Stoa could not save that'))
      .then(() => setBusy(false))
  }
  const stats = [['Open', data.open ? data.open + (data.open_value ? ' · ' + money(data.open_value, pb.currency) : '') : '0'],
    [pb.won.label, data.won ? data.won + (data.won_value ? ' · ' + money(data.won_value, pb.currency) : '') : '0'],
    [pb.lost.label, String(data.lost || 0)], ['Last touch', ago(data.last_touch) || '—']]
  const groups = uniq((data.records || []).map(record => record.group))
  return h('div', { className: 'stoa-rp' },
    h(BackRow, { back, label: backLabel }),
    h('div', { className: 'stoa-rphead' },
      h('div', { className: 'stoa-grow' },
        h('div', { className: 'stoa-eyebrow' }, 'Company'),
        h('h2', { className: 'stoa-rptitle' }, company.name),
        h('div', { className: 'stoa-rpwhere stoa-sub' }, company.domain || 'No email domain known yet')),
      h('div', { className: 'stoa-rpstats' }, stats.map(pair => h('div', { key: pair[0] }, h('span', { className: 'stoa-eyebrow' }, pair[0]), h('b', null, pair[1]))))),
    h('div', { className: 'stoa-rpgrid' },
      h('div', { className: 'stoa-rpcol' },
        h(Section, { title: 'Details' },
          h('div', { className: 'stoa-form', 'data-one': '1', style: { marginTop: 0 } },
            h(Field, { label: 'Email domain', value: domain === null ? company.domain : domain, placeholder: 'kestrel.com',
              title: 'Mail from anyone at this domain is recognised as coming from this company', onChange: event => setDomain(event.target.value) })),
          groups.length ? h('div', { style: { marginTop: 12 } }, h('small', { className: 'stoa-eyebrow' }, cap(pb.terms.groups)),
            groups.map(item => h('div', { key: item }, h('button', { type: 'button', className: 'stoa-proplink stoa-alink', onClick: () => openGroup(item) }, item)))) : null,
          h('small', { className: 'stoa-eyebrow', style: { display: 'block', marginTop: 12 } }, 'Notes'),
          h('textarea', { className: 'stoa-field stoa-notes', rows: 4, value: note === null ? company.note : note, placeholder: 'What is worth remembering about them: how they buy, who decides, when their year ends…',
            onChange: event => setNote(event.target.value) }),
          changed ? h('div', { className: 'stoa-btns' }, h(Btn, { kind: 'primary', disabled: busy, onClick: save }, 'Save'),
            h(Btn, { kind: 'quiet', disabled: busy, onClick: () => { setNote(null); setDomain(null) } }, 'Cancel')) : null)),
      h('div', { className: 'stoa-rpcol' },
        h(Section, { main: true, title: cap(pb.terms.deals) + ' here (' + (data.records || []).length + ')' },
          h(ByStage, { records: data.records || [], pb, openRecord }))),
      h('div', { className: 'stoa-rpcol' },
        h(Section, { title: 'People there' },
          (data.people || []).length ? data.people.map((person, index) => h(PersonLine, { key: index, person: { ...person, on: 'record' }, openRecord }))
            : h('div', { className: 'stoa-sub' }, 'Nobody yet.')),
        h(Section, { title: 'Lately' }, h(Lately, { entries: (data.activity || []).map(entry => ({ ...entry, deal: entry.deal || entry.record })), openRecord })))))
}

// -- a client: the company the work is for, its searches, and how it is billed when that is by the month ----
/** Whether the desk keeps money for clients billed by the month: then there is a Cash tab even where records are never billed. */
const hasClientMoney = data => Boolean(data.cash && (((data.cash.strip || {}).to_bill || { count: 0 }).count || (data.cash.invoices || []).some(item => item.client)))
  || (data.clients || []).some(row => row.terms && row.terms.kind)

async function billClient(client, body) {
  try {
    const result = await rest('/client/bill', { method: 'POST', body: { client, ...body } })
    refresh()
    return result
  } catch (error) {
    host.notifyError(error, 'Stoa could not save that')
    return null
  }
}

const monthLabel = period => { const d = new Date(period + '-15T12:00:00'); return isNaN(d) ? period : d.toLocaleDateString(undefined, { month: 'long', year: 'numeric' }) }
const termsLine = (terms, currency, client) => {
  if (!terms || !terms.kind) return ''
  const to = terms.bill_to && !sameName(terms.bill_to, client) ? ' to ' + terms.bill_to : ''
  return terms.kind === 'hourly'
    ? exact(terms.rate, currency) + ' an hour · ' + terms.hours_week + ' h a week · billed monthly' + to
    : exact(terms.rate, currency) + ' a month · billed monthly' + to
}

/** A month that has ended and is not billed: what it comes to, and the invoice recorded in one go (or the month skipped). */
function BillMonth({ row, pb, showClient, openClient }) {
  const hourly = row.kind === 'hourly'
  const [open, setOpen] = useState(false)
  const [hours, setHours] = useState(hourly ? String(row.hours) : '')
  const [amount, setAmount] = useState(String(row.amount))
  const [number, setNumber] = useState('')
  const [busy, setBusy] = useState(false)
  const total = hourly ? Math.round((Number(hours) || 0) * row.rate * 100) / 100 : Number(amount) || 0
  const record = () => {
    if (busy || !(total > 0)) return
    setBusy(true)
    billClient(row.client, { verb: 'add', period: row.period, number: number.trim(), ...(hourly ? { hours: Number(hours) } : { amount: Number(amount) }) })
      .then(result => { setBusy(false); if (result) { setOpen(false); host.notify({ kind: 'success', message: row.label + ' recorded for ' + row.client + ': ' + exact(total, pb.currency) + '.' }) } })
  }
  const skip = () => { setBusy(true); billClient(row.client, { verb: 'skip', period: row.period }).then(() => setBusy(false)) }
  const enter = event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) record(); if (event.key === 'Escape') setOpen(false) }
  return h('div', { className: 'stoa-irow' },
    h('span', { className: 'stoa-grow' },
      h('span', { className: 'stoa-name', style: { display: 'block' } },
        showClient ? h('button', { type: 'button', className: 'stoa-proplink stoa-name', onClick: () => openClient(row.client) }, row.client) : null,
        showClient ? h('span', { className: 'stoa-sub', style: { fontWeight: 400 } }, '  ·  ') : null, row.label),
      h('span', { className: 'stoa-sub', style: { display: 'block' } },
        (hourly ? row.hours + ' h × ' + exact(row.rate, pb.currency) : 'Retainer') + (row.bill_to && !sameName(row.bill_to, row.client) ? '  ·  to ' + row.bill_to : ''))),
    h('span', { className: 'stoa-num' }, exact(row.amount, pb.currency)),
    h('span', { className: 'stoa-tacts' },
      h(Btn, { kind: 'go', disabled: busy, onClick: () => setOpen(!open) }, open ? 'Close' : 'Record invoice'),
      h(Btn, { disabled: busy, title: 'Not billed for this month: time off, a pause. It can be billed later from the client.', onClick: skip }, 'Skip')),
    open ? h('div', { className: 'stoa-billform' },
      hourly ? h(Field, { label: 'Hours in ' + row.label.split(' ')[0], type: 'number', min: 0, step: 'any', autoFocus: true, value: hours, onChange: event => setHours(event.target.value), onKeyDown: enter })
        : h(Field, { label: 'Amount', type: 'number', min: 0, step: 'any', autoFocus: true, value: amount, onChange: event => setAmount(event.target.value), onKeyDown: enter }),
      h(Field, { label: 'Invoice number', value: number, placeholder: 'optional', onChange: event => setNumber(event.target.value), onKeyDown: enter }),
      h('div', { className: 'stoa-billtotal' }, h('small', { className: 'stoa-eyebrow' }, 'The invoice'), h('b', null, exact(total, pb.currency))),
      h(Btn, { kind: 'primary', disabled: busy || !(total > 0), onClick: record }, 'Record it'),
      h('span', { className: 'stoa-sub', style: { gridColumn: '1 / -1' } }, 'Stoa keeps the record; send the invoice itself the way you always do.')) : null)
}

/** How a client is billed by the month: the terms, the months waiting, and its invoices. */
function ClientBilling({ name, billing, pb, compact }) {
  const terms = billing.terms || {}
  const [editing, setEditing] = useState(false)
  const start = { kind: terms.kind || 'hourly', rate: terms.rate ? String(terms.rate) : '', hours_week: String(terms.hours_week || 40),
    bill_to: terms.bill_to || '', start: terms.start || new Date().toISOString().slice(0, 7), end: terms.end || '' }
  const [v, setV] = useState(start)
  const [busy, setBusy] = useState(false)
  const set = key => event => setV({ ...v, [key]: event.target.value })
  const save = clear => {
    setBusy(true)
    const body = clear ? { name, terms: { kind: '' } }
      : { name, terms: { kind: v.kind, rate: Number(v.rate), hours_week: v.kind === 'hourly' ? Number(v.hours_week) : undefined, bill_to: v.bill_to.trim(), start: v.start, end: v.end } }
    rest('/client', { method: 'POST', body })
      .then(() => { refresh(); setEditing(false); host.notify({ kind: 'success', message: clear ? 'Billing terms taken off ' + name + '.' : 'Billing terms saved on ' + name + '.' }) })
      .catch(error => host.notifyError(error, 'Stoa could not save those terms'))
      .then(() => setBusy(false))
  }
  const invoices = billing.invoices || []
  const totals = [billing.billed ? 'billed ' + exact(billing.billed, pb.currency) : '', billing.paid ? 'paid ' + exact(billing.paid, pb.currency) : '',
    billing.owed ? 'owed ' + exact(billing.owed, pb.currency) : ''].filter(Boolean).join(' · ')
  return h(Section, { title: 'Billing', aside: h(Btn, { kind: 'quiet', onClick: () => { setV(start); setEditing(!editing) } }, editing ? 'Close' : terms.kind ? 'Edit terms' : 'Set terms') },
    editing ? h('div', null,
      h('div', { className: 'stoa-form', 'data-one': '1', style: { marginTop: 0 } },
        h(Field, { label: 'Billed' }, h('select', { className: 'stoa-field', value: v.kind, onChange: set('kind') },
          h('option', { value: 'hourly' }, 'By the hour, every month'), h('option', { value: 'retainer' }, 'A fixed amount every month'))),
        h(Field, { label: v.kind === 'hourly' ? 'Rate per hour' : 'Amount each month', type: 'number', min: 0, step: 'any', value: v.rate, onChange: set('rate') }),
        v.kind === 'hourly' ? h(Field, { label: 'Usual hours a week', type: 'number', min: 1, max: 80, value: v.hours_week, onChange: set('hours_week') }) : null,
        h(Field, { label: 'Invoice goes to', value: v.bill_to, placeholder: name + ' (or the fund, a parent company…)', onChange: set('bill_to') }),
        h(Field, { label: 'First month billed', type: 'month', value: v.start, onChange: set('start') }),
        h(Field, { label: 'Last month (when it ends)', type: 'month', value: v.end, onChange: set('end') })),
      h('div', { className: 'stoa-btns' },
        h(Btn, { kind: 'primary', disabled: busy || !(Number(v.rate) > 0), onClick: () => save(false) }, 'Save terms'),
        terms.kind ? h(Btn, { kind: 'quiet', disabled: busy, onClick: () => save(true) }, 'Not billed by the month') : null))
      : terms.kind ? h('div', null,
        h('div', { className: 'stoa-name' }, termsLine(terms, pb.currency, name)),
        h('div', { className: 'stoa-sub' }, 'Since ' + monthLabel(terms.start) + (terms.end ? ', until ' + monthLabel(terms.end) : '')))
        : h('div', { className: 'stoa-sub' }, 'Not billed by the month. Set terms for contract hours or a retainer, and each month that ends is waiting on Today to be billed.'),
    (billing.to_bill || []).length ? h('div', { style: { marginTop: 12 } },
      h('small', { className: 'stoa-eyebrow' }, 'To bill'),
      billing.to_bill.map(row => h(BillMonth, { key: row.period, row, pb }))) : null,
    invoices.length ? h('div', { style: { marginTop: 12 } },
      h('small', { className: 'stoa-eyebrow' }, 'Invoices'),
      totals ? h('div', { className: 'stoa-sub', style: { margin: '2px 0 2px' } }, cap(totals)) : null,
      (compact ? invoices.slice(0, 6) : invoices).map(invoice => h(Invoice, { key: invoice.id, invoice, client: name, currency: pb.currency,
        name: invoice.period_label || '' }))) : null)
}

/** Every client: what is open for them, and how each is billed. */
function Clients({ state, openClient }) {
  const filter = useValue($filter)
  const pb = state.playbook
  const text = filter.text.toLowerCase()
  const rows = (state.clients || []).filter(row => !text || row.name.toLowerCase().includes(text))
  if (!rows.length) return h('p', { className: 'stoa-sub' }, text ? 'No client matches “' + filter.text + '”.' : 'No client yet. Name the client on a ' + pb.terms.group + ' (open it, then Edit), and it is listed here.')
  return h('div', { className: 'stoa-companies' },
    h('div', { className: 'stoa-crow stoa-chead', 'data-cols': '6' }, ['Client', cap(pb.terms.groups), 'Open', pb.won.label, 'Billing', 'Owed'].map(label => h('span', { key: label, className: 'stoa-eyebrow' }, label))),
    rows.map(row => h('button', { type: 'button', key: row.key, className: 'stoa-crow', 'data-cols': '6', onClick: () => openClient(row.name) },
      h('span', { className: 'stoa-name stoa-clip' }, row.name),
      h('span', { className: 'stoa-num' }, row.open_groups + (row.groups.length > row.open_groups ? ' of ' + row.groups.length : '')),
      h('span', { className: 'stoa-num' }, row.open ? row.open + (row.open_value ? ' · ' + money(row.open_value, pb.currency) : '') : '—'),
      h('span', { className: 'stoa-num' }, row.won ? String(row.won) : '—'),
      h('span', { className: 'stoa-clip stoa-sub' }, row.terms && row.terms.kind ? (row.terms.kind === 'hourly' ? exact(row.terms.rate, pb.currency) + '/h' : exact(row.terms.rate, pb.currency) + '/mo')
        + (row.to_bill ? ' · ' + row.to_bill + ' to bill' : '') : 'by placement'),
      h('span', { className: 'stoa-num', 'data-tone': row.overdue ? 'bad' : '' }, row.owed ? exact(row.owed, pb.currency) : '—'))))
}

function ClientPage({ name, state, back, backLabel, openRecord, openGroup }) {
  const query = useQuery({ queryKey: ['stoa', 'client', name], queryFn: () => rest('/client', { method: 'POST', body: { name } }), staleTime: 1500 })
  const [note, setNote] = useState(null)
  const [domain, setDomain] = useState(null)
  const [busy, setBusy] = useState(false)
  const [opening, setOpening] = useState(false)
  const [newName, setNewName] = useState('')
  const data = query.data
  if (query.error) return h('div', null, h(BackRow, { back, label: backLabel }), h('div', { className: 'stoa-box stoa-sub' }, 'No client by that name is on the desk any more.'))
  if (!data) return h('div', null, h(BackRow, { back, label: backLabel }), h('div', { className: 'stoa-sub' }, 'Loading…'))
  const pb = state.playbook, client = data.client, term = pb.terms.group
  const changed = (note !== null && note !== client.note) || (domain !== null && domain.trim().toLowerCase() !== client.domain)
  const save = () => {
    setBusy(true)
    rest('/client', { method: 'POST', body: { name: client.name, note: note === null ? client.note : note, domain: domain === null ? client.domain : domain } })
      .then(() => { refresh(); setNote(null); setDomain(null); host.notify({ kind: 'success', message: 'Saved on ' + client.name + '.' }) })
      .catch(error => host.notifyError(error, 'Stoa could not save that'))
      .then(() => setBusy(false))
  }
  const openSearch = () => {
    const title = newName.trim()
    if (!title || busy) return
    setBusy(true)
    rest('/group', { method: 'POST', body: { name: title, new: true, client: client.name } })
      .then(result => { refresh(); setNewName(''); setOpening(false); openGroup(result.group.name) })
      .catch(error => host.notifyError(error, 'Stoa could not open that'))
      .then(() => setBusy(false))
  }
  const billing = data.billing || {}
  const stats = [['Open', data.open ? data.open + (data.open_value ? ' · ' + money(data.open_value, pb.currency) : '') : '0'],
    [cap(pb.terms.groups), String(data.searches.filter(row => row.status === 'open' || row.status === 'hold').length)],
    [pb.won.label, String(data.won)], ['Owed', billing.owed ? exact(billing.owed, pb.currency) : '—']]
  return h('div', { className: 'stoa-rp' },
    h(BackRow, { back, label: backLabel }),
    h('div', { className: 'stoa-rphead' },
      h('div', { className: 'stoa-grow' },
        h('div', { className: 'stoa-eyebrow' }, 'Client'),
        h('h2', { className: 'stoa-rptitle' }, client.name),
        h('div', { className: 'stoa-rpwhere stoa-sub' }, [termsLine(billing.terms, pb.currency, client.name), client.domain].filter(Boolean).join('  ·  ') || 'Billed by placement')),
      h('div', { className: 'stoa-rpstats' }, stats.map(pair => h('div', { key: pair[0] }, h('span', { className: 'stoa-eyebrow' }, pair[0]), h('b', null, pair[1]))))),
    h('div', { className: 'stoa-rpbar', 'data-plain': '1' },
      opening ? h('div', { className: 'stoa-newgroup', style: { margin: 0 } },
        h(Field, { label: 'New ' + term + ' for ' + client.name, autoFocus: true, value: newName, placeholder: 'Its name (the role)',
          onChange: event => setNewName(event.target.value), onKeyDown: event => { if (event.key === 'Enter') openSearch(); if (event.key === 'Escape') setOpening(false) } }),
        h(Btn, { kind: 'primary', disabled: busy || !newName.trim(), onClick: openSearch }, 'Open it'),
        h(Btn, { kind: 'quiet', onClick: () => setOpening(false) }, 'Cancel'))
        : h('div', { className: 'stoa-btns', style: { marginTop: 0 } }, h(Btn, { kind: 'primary', onClick: () => setOpening(true) }, '+  New ' + term))),
    h('div', { className: 'stoa-rpgrid' },
      h('div', { className: 'stoa-rpcol' },
        h(Section, { title: 'Details' },
          h('div', { className: 'stoa-form', 'data-one': '1', style: { marginTop: 0 } },
            h(Field, { label: 'Email domain', value: domain === null ? client.domain : domain, placeholder: 'fernway.com',
              title: 'Mail from anyone at this domain is recognised as coming from this client', onChange: event => setDomain(event.target.value) })),
          h('small', { className: 'stoa-eyebrow', style: { display: 'block', marginTop: 12 } }, 'Notes'),
          h('textarea', { className: 'stoa-field stoa-notes', rows: 4, value: note === null ? client.note : note,
            placeholder: 'What to know about them: who decides, how they hire, what was agreed…', onChange: event => setNote(event.target.value) }),
          changed ? h('div', { className: 'stoa-btns' }, h(Btn, { kind: 'primary', disabled: busy, onClick: save }, 'Save'),
            h(Btn, { kind: 'quiet', disabled: busy, onClick: () => { setNote(null); setDomain(null) } }, 'Cancel')) : null)),
      h('div', { className: 'stoa-rpcol' },
        h(Section, { main: true, title: cap(pb.terms.groups) + ' (' + data.searches.length + ')' },
          data.searches.length ? h('div', { className: 'stoa-rlist' }, data.searches.map(row => h('button', { type: 'button', key: row.name, className: 'stoa-rrow',
            'data-closed': row.status === 'open' || row.status === 'hold' ? '0' : '1', onClick: () => openGroup(row.name) },
          h('span', { className: 'stoa-grow' },
            h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, row.name),
            h('span', { className: 'stoa-sub stoa-clip', style: { display: 'block' } },
              [row.status === 'open' ? '' : row.status_label, plural(row.open, 'open', 'open'), row.won ? row.won + ' ' + pb.won.label.toLowerCase() : '']
                .concat(row.facts.slice(0, 2).map(fact => fact.label + ' ' + fact.value)).filter(Boolean).join(' · '))),
          row.open_value ? h('span', { className: 'stoa-num' }, money(row.open_value, pb.currency)) : null)))
            : h('p', { className: 'stoa-sub' }, 'None yet. Open one above.')),
        h(Section, { title: cap(pb.terms.deals) + ' (' + data.records.length + ')' }, h(ByStage, { records: data.records, pb, openRecord }))),
      h('div', { className: 'stoa-rpcol' },
        h(ClientBilling, { key: 'bill:' + client.name, name: client.name, billing, pb }),
        h(Section, { title: 'On their side' },
          data.people.length ? data.people.map(person => h(PersonLine, { key: person.on + person.id, person: { ...person, record_name: person.record_name || person.group }, openRecord }))
            : h('div', { className: 'stoa-sub' }, 'Nobody yet. Add them on a ' + term + '.')),
        h(Section, { title: 'Lately' }, h(Lately, { entries: data.activity || [], openRecord })))))
}

// -- the full page -------------------------------------------------------------------------------
// The board is worked by hand as well as by talking: drag a card to another column to move it.
const DRAG = 'text/stoa-deal'
const dragging = event => Boolean(event.dataTransfer) && [...event.dataTransfer.types].includes(DRAG)

/** How long records have sat in a column, beside how long the playbook allows before a nudge is due. */
function Pace({ column }) {
  const days = column.here.map(deal => (Date.now() - Date.parse(deal.stage_at || '')) / 864e5).filter(value => value >= 0)
  if (!days.length || !column.stale_days) return h('div', { className: 'stoa-pace' }, column.stale_days ? 'quiet after ' + column.stale_days + 'd' : ' ')
  const average = Math.round(days.reduce((sum, value) => sum + value, 0) / days.length)
  return h('div', { className: 'stoa-pace',
    title: 'Records here have been in this stage ' + average + ' days on average. A record counts as quiet after ' + column.stale_days + ' days with nothing logged (your playbook sets that).' },
  'avg ' + average + 'd here · quiet after ' + column.stale_days + 'd')
}

function Board({ state, openId, setOpenId }) {
  const pb = state.playbook
  const filter = useValue($filter)
  const [over, setOver] = useState('')
  const board = useRef(null)
  // A stage picked out in the funnel: bring its column into view. Every column keeps its cards.
  useEffect(() => {
    const el = board.current
    const column = el && filter.stage ? el.querySelector('[data-spot="1"]') : null
    if (column) el.scrollTo({ left: Math.max(0, column.offsetLeft - el.offsetLeft - (el.clientWidth - column.clientWidth) / 2), behavior: 'smooth' })
  }, [filter.stage])
  const open = state.deals.filter(deal => deal.status === 'open' && matches(deal, filter))
  const won = state.deals.filter(deal => deal.status === 'won' && matches(deal, { ...filter, due: false, quiet: false }))
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
  const picked = filter.stage && known.has(filter.stage) ? filter.stage : ''       // a stage from another playbook picks out nothing
  return h('div', { className: 'stoa-board', ref: board }, columns.map((column, place) =>
    h('div', { key: column.id, className: 'stoa-col', 'data-over': over === column.id ? '1' : '0', 'data-dim': picked && column.id !== picked ? '1' : '0',
      'data-spot': picked && column.id === picked ? '1' : '0',
      onDragOver: event => { if (dragging(event) && !column.other) { event.preventDefault(); if (over !== column.id) setOver(column.id) } },
      onDragLeave: event => { if (!event.currentTarget.contains(event.relatedTarget)) setOver('') },
      onDrop: event => drop(event, column) },
      h('div', { className: 'stoa-colhead' }, h('span', { className: 'stoa-eyebrow stoa-clip' }, column.label),
        h('span', { className: 'stoa-num' }, column.here.length + (column.value ? ' · ' + money(column.value, pb.currency) : ''))),
      h('div', { className: 'stoa-weight', 'data-won': column.won ? '1' : '0' }, h('i', { style: { width: (column.won ? 100 : Math.round((100 * column.value) / most)) + '%', ...heatAt(place, columns.length) } })),
      column.won || column.other ? null : h(Pace, { column }),
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
  const tab = useValue($tab)
  // What is open on a page of its own: a record, a group or a company, in the order they were opened, so Back
  // goes to the one before. Empty: the tab's own list is on screen.
  const [stack, setStack] = useState([])
  const [adding, setAdding] = useState(false)
  const [marketId, setMarketId] = useState('')
  const savedView = useValue($view)
  const [dropping, setDropping] = useState(false)
  const [welcomed, setWelcomed] = useState(() => Boolean(storage && storage.get('welcomed', false)))
  const split = useRef(null)
  const typing = useValue($typing)
  // While the page is on screen, a click that calls for writing goes to its own dock.
  useEffect(() => { onPage = true; $onPage.set(true); return () => { onPage = false; $onPage.set(false); $last.set(null) } }, [])
  // A record picked in the pane beside the desk opens here, on a tab that has room for it.
  const want = useValue($want)
  useEffect(() => {
    if (!want) return
    if (!['today', 'pipeline', 'cash', 'activity'].includes($tab.get())) $tab.set('pipeline')
    setAdding(false)
    setStack(now => (now.length && now[now.length - 1].kind === 'record' && now[now.length - 1].id === want ? now : now.concat([{ kind: 'record', id: want }]).slice(-12)))
    $want.set('')
  }, [want])
  const top = stack.length ? stack[stack.length - 1] : null
  // A record, a group or a company opens at the top of the page.
  useEffect(() => {
    const page = split.current && split.current.closest ? split.current.closest('.stoa-page') : document.querySelector('.stoa-page')
    if (top && page) page.scrollTo({ top: 0 })
  }, [top ? top.kind + ':' + top.id : ''])
  // The form for a new record opens beside the list: bring it clear of the dock if it would sit under it.
  useEffect(() => {
    if (!adding) return undefined
    const timer = setTimeout(() => {
      const box = split.current && split.current.querySelector(':scope > .stoa-box')
      const page = split.current && split.current.closest('.stoa-page')
      if (!box || !page) return
      const frame = page.getBoundingClientRect(), rect = box.getBoundingClientRect()
      const hidden = rect.bottom - (frame.bottom - 150)
      if (hidden > 0) page.scrollBy({ top: Math.min(hidden, Math.max(0, rect.top - frame.top - 12)), behavior: 'smooth' })
    }, 260)
    return () => clearTimeout(timer)
  }, [adding, tab])
  // A desk whose records are never billed has no Cash tab: if that was the last one shown, show Today.
  const cashShown = Boolean(query.data && query.data.playbook && (query.data.playbook.billing !== 'none' || hasClientMoney(query.data)))
  useEffect(() => { if (tab === 'cash' && query.data && !cashShown) $tab.set('today') }, [tab, cashShown, Boolean(query.data)])
  if (!query.data) return h('div', { className: 'stoa stoa-page' }, h(Unavailable, { error: query.error, loading: query.isLoading }))
  const state = query.data, pb = state.playbook, totals = state.totals
  const sign = state.sign || { text: '', place: 'art' }, signed = String(typing ?? sign.text ?? '').trim()
  const day = state.today || { counts: {}, needs: 0 }
  const market = state.markets.find(item => item.id === marketId) || state.markets[0]
  const setTab = name => { $tab.set(name); setStack([]); setAdding(false) }
  const add = () => { if (tab !== 'today' && tab !== 'pipeline') $tab.set('pipeline'); setStack([]); setAdding(true) }
  const push = (kind, id) => {
    if (!id) return
    setAdding(false)
    setStack(now => (now.length && now[now.length - 1].kind === kind && now[now.length - 1].id === id ? now : now.concat([{ kind, id }]).slice(-12)))
  }
  const openRecord = id => push('record', id)
  const openGroup = name => push('group', name)
  const openCompany = name => push('company', name)
  const openClient = name => push('client', name)
  const back = () => setStack(now => now.slice(0, -1))
  const renamed = name => setStack(now => now.slice(0, -1).concat([{ kind: 'group', id: name }]))
  // Where an account or a client is the company, there is one place to look: the group's own page. No separate list of companies.
  const view = (savedView === 'companies' || savedView === 'clients') && pb.group_is_company ? 'groups' : savedView
  const groups = state.groups || []
  const pipes = state.pipelines || []
  const showPipe = id => {
    if (id === state.pipeline) return
    $pipe.set(id === 'main' ? '' : id)
    if (storage) storage.set('pipe', id === 'main' ? '' : id)
    setStack([]); setAdding(false)
    refresh()
  }
  const waiting = (day.counts && day.counts.approvals) || 0
  const welcome = Boolean(state.fresh) && !welcomed
  const skip = () => { setWelcomed(true); if (storage) storage.set('welcomed', true) }
  // Each number says what it counts when you rest on it; the ones that are lists take you there.
  const stats = [
    ['Open', totals.open, cap(pb.terms.deals) + ' still in play: not ' + pb.won.label.toLowerCase() + ', not ' + pb.lost.label.toLowerCase() + '.', 'pipeline'],
    ['Due', totals.due, 'Next steps dated today or earlier. Snoozed ' + pb.terms.deals + ' are left out. Press to see them.', 'today'],
    ['Quiet', totals.stale, 'No call, email, note or move for longer than the stage allows. Your playbook sets the days for each stage. Press to see them.', 'today'],
    ['Mapped', totals.mapped, 'Companies on your market maps.', 'market'],
    ['From the map', totals.tracked, cap(pb.terms.deals) + ' that started on a market map.', ''],
    ['Won', totals.won + (totals.won_value ? ' · ' + money(totals.won_value, pb.currency) : ''), 'Closed as ' + pb.won.label.toLowerCase() + ', all time.', '']]
  const side = adding ? h(NewRecord, { state, close: () => setAdding(false), opened: id => { setAdding(false); openRecord(id) } }) : null
  const tabLabel = { today: 'Today', pipeline: view === 'groups' ? cap(pb.terms.groups) : view === 'companies' ? 'Companies' : view === 'clients' ? 'Clients' : 'Pipeline', cash: 'Cash', activity: 'Activity' }[tab] || 'Back'
  const before = stack.length > 1 ? stack[stack.length - 2] : null
  const backLabel = !before ? tabLabel : before.kind === 'record' ? ((state.deals.find(item => item.id === before.id) || {}).name || 'Back') : before.id
  const sheet = !top ? null
    : top.kind === 'record' ? h(RecordPage, { key: 'r:' + top.id, id: top.id, state, back, backLabel, openRecord, openGroup, openCompany, openClient })
      : top.kind === 'group' ? h(GroupPage, { key: 'g:' + top.id, name: top.id, state, back, backLabel, openRecord, openClient, onRenamed: renamed })
        : top.kind === 'client' ? h(ClientPage, { key: 'cl:' + top.id, name: top.id, state, back, backLabel, openRecord, openGroup })
          : h(CompanyPage, { key: 'c:' + top.id, name: top.id, state, back, backLabel, openRecord, openGroup })
  const paged = Boolean(sheet) && ['today', 'pipeline', 'cash', 'activity'].includes(tab)
  const nothing = h('div', { className: 'stoa-sub' },
    h('p', { style: { marginTop: 0 } }, 'Nothing on the desk yet. Add a ' + pb.terms.deal + ' with the button above, tell Stoa about one below, drop a list on the page, or ask it to map a market.'),
    h(Btn, { onClick: () => setTab('setup') }, 'See how it works, or look around with sample data'))
  // A file dragged in from the computer lands on the desk, wherever on the page it is let go.
  const carrying = event => Boolean(event.dataTransfer) && [...event.dataTransfer.types].includes('Files')
  return h('div', { className: 'stoa stoa-page', 'data-drop': dropping ? '1' : '0',
    onDragOver: event => { if (carrying(event)) { event.preventDefault(); if (!dropping) setDropping(true) } },
    onDragLeave: event => { if (!event.currentTarget.contains(event.relatedTarget)) setDropping(false) },
    onDrop: event => { if (!carrying(event)) return; event.preventDefault(); setDropping(false); takeFile(event.dataTransfer.files[0]) } },
  h(Style),
  dropping ? h('div', { className: 'stoa-dropnote' }, 'Drop it on the desk: a CSV becomes a review to approve') : null,
  h('div', { className: 'stoa-wrap' },
    state.tour ? h('div', { className: 'stoa-tour', role: 'status' },
      h('span', null, h('b', null, 'Sample data'), ' · ' + state.tour.title + '. Everyone on it is made up, and your own desk is untouched.'),
      h(Btn, { onClick: () => { setStack([]); tour('leave') } }, 'Leave the tour')) : null,
    pipes.length > 1 ? h('div', { className: 'stoa-pipes', role: 'tablist', 'aria-label': 'Pipelines' }, pipes.map(item =>
      h('button', { type: 'button', key: item.id, role: 'tab', 'aria-selected': item.id === state.pipeline, className: 'stoa-pipe', 'data-on': item.id === state.pipeline ? '1' : '0',
        title: item.title + ': its own ' + 'records and its own playbook', onClick: () => showPipe(item.id) }, item.name, h('small', null, String(item.open))))) : null,
    h('div', { className: 'stoa-head', 'data-sign': welcome || !signed ? '' : sign.place === 'beside' ? 'beside' : 'art' },
      welcome ? null : h('div', { className: 'stoa-headart' },
        signed && sign.place !== 'beside' ? h(NameArt, { text: signed, side: 'right' }) : h(Art, { name: 'band' })),
      h('div', { className: 'stoa-headmain' },
        // Beside the stoa the name is always here. In place of the stoa it is here only when the page is too narrow for it there.
        signed && !welcome ? h('div', { className: 'stoa-headname', 'data-narrow': sign.place === 'beside' ? '0' : '1' }, h(NameArt, { text: signed, side: 'left' })) : null,
        h('div', { className: 'stoa-eyebrow' }, welcome ? 'Stoa' : 'Stoa · ' + pb.title),
        h('div', { className: 'stoa-figure' }, h(DotFigure, { text: money(totals.open_value, pb.currency) || String(totals.open) }), h('small', null, totals.open_value ? 'in play' : pb.terms.deals + ' open'))),
      h('div', { className: 'stoa-headstats' }, stats.map(item => h('button', { type: 'button', key: item[0], className: 'stoa-stat', title: item[2], 'data-press': item[3] ? '1' : '0',
        onClick: item[3] ? () => setTab(item[3]) : undefined }, h('span', { className: 'stoa-eyebrow' }, item[0]), h('b', null, String(item[1])))))),
    h('div', { className: 'stoa-rule' }),
    h('div', { className: 'stoa-tabsrow' },
      h('div', { className: 'stoa-tabs', role: 'tablist' }, [['today', 'Today', day.needs], ['pipeline', 'Pipeline', totals.open]]
        .concat(cashShown ? [['cash', 'Cash', state.cash ? state.cash.strip.overdue.count + (state.cash.strip.to_bill || { count: 0 }).count : 0]] : [])
        .concat([['market', 'Market map', totals.mapped], ['activity', 'Activity', 0], ['setup', 'Setup', 0]]).map(item =>
        h('button', { type: 'button', key: item[0], role: 'tab', 'aria-selected': tab === item[0], className: 'stoa-tab', 'data-on': tab === item[0] ? '1' : '0',
          onClick: () => setTab(item[0]) }, item[1], item[2] ? h('small', null, String(item[2])) : null))),
      (tab === 'today' || tab === 'pipeline') && !welcome ? h(Btn, { onClick: add }, '+  New ' + pb.terms.deal) : null),
    waiting && tab !== 'today' && tab !== 'setup' ? h('button', { type: 'button', className: 'stoa-waiting', onClick: () => setTab('today') },
      h('b', null, String(waiting)), ' waiting for your approval', h('span', { className: 'stoa-sub' }, '  ·  see Today  ›')) : null,
    (tab === 'today' || tab === 'pipeline') && welcome && !waiting ? h(Welcome, { skip }) : null,
    paged ? h('div', { ref: split }, sheet) : null,
    !paged && tab === 'today' && (!welcome || waiting) ? h('div', { className: 'stoa-split', ref: split, 'data-side': side || state.deals.length ? '1' : '0' },
      state.deals.length || waiting || (day.to_bill || []).length ? h(Today, { state, openId: '', setOpenId: openRecord, openClient }) : nothing,
      side || (state.deals.length ? h(Ahead, { state, setOpenId: openRecord }) : null)) : null,
    !paged && tab === 'pipeline' && (!welcome || waiting) ? h('div', null,
      state.deals.length && view === 'board' ? h(Funnel, { state }) : null,
      state.deals.length || groups.length ? h('div', { className: 'stoa-viewrow' },
        h('div', { className: 'stoa-grow', style: { maxWidth: 760 } }, h(Filters, { state })),
        h('div', { className: 'stoa-tabs', style: { margin: 0 }, role: 'group', 'aria-label': 'How the pipeline is shown' },
          [['board', 'Board']].concat(pb.group_is_company ? [] : [['clients', 'Clients']]).concat([['groups', cap(pb.terms.groups || 'groups')]])
            .concat(pb.group_is_company ? [] : [['companies', 'Companies']]).map(item => h('button', { type: 'button', key: item[0], className: 'stoa-tab', style: { height: 26, padding: '0 11px', fontSize: 12 },
            'data-on': view === item[0] ? '1' : '0', 'aria-pressed': view === item[0], onClick: () => $view.set(item[0]) }, item[1])))) : null,
      h('div', { className: 'stoa-split', ref: split, 'data-side': side ? '1' : '0' },
        view === 'clients' && !pb.group_is_company ? h(Clients, { state, openClient })
          : view === 'groups' && (state.deals.length || groups.length) ? h(Groups, { state, current: '', openGroup })
          : !state.deals.length ? nothing : view === 'companies' ? h(Companies, { state, current: '', openCompany })
            : h(Board, { state, openId: '', setOpenId: openRecord }), side)) : null,
    !paged && tab === 'cash' ? h('div', { className: 'stoa-split', ref: split, 'data-side': '0' },
      h(Cash, { state, openId: '', setOpenId: openRecord, openClient })) : null,
    tab === 'market' ? (market
      ? h('div', null,
        state.markets.length > 1 ? h('div', { className: 'stoa-tabs', style: { marginTop: 0 } }, state.markets.map(item =>
          h('button', { type: 'button', key: item.id, className: 'stoa-tab', 'data-on': item.id === market.id ? '1' : '0', onClick: () => setMarketId(item.id) }, item.name))) : null,
        h(Market, { market }))
      : h('p', { className: 'stoa-sub' }, 'No market map yet. Ask Stoa below: “Map the market for us.”')) : null,
    !paged && tab === 'activity' ? h('div', { style: { maxWidth: 800 } }, state.activity.length ? state.activity.map(entry => h('div', { key: entry.id, className: 'stoa-row', style: { cursor: 'default', alignItems: 'center' } },
      h('i', { className: 'stoa-dot', style: { marginTop: 0 }, 'data-tone': entry.kind === 'stage' && !entry.undone ? 'good' : '' }),
      h('span', { className: 'stoa-grow', style: entry.undone ? { opacity: 0.55 } : undefined },
        state.deals.some(item => item.id === entry.deal)
          ? h('button', { type: 'button', className: 'stoa-proplink stoa-name', onClick: () => openRecord(entry.deal) }, entry.name)
          : h('span', { className: 'stoa-name' }, entry.name), h('span', { style: entry.undone ? { textDecoration: 'line-through' } : undefined }, '  ' + entry.text),
        h('span', { className: 'stoa-sub', style: { display: 'block' } }, ago(entry.at) + ' · ' + (entry.undone ? 'taken back' : entry.kind) + (entry.by ? ' · ' + entry.by : ''))),
      entry.can_undo ? h(Btn, { kind: 'quiet', title: 'This is the last change on ' + entry.name + ': put it back as it was', onClick: () => undo(entry.deal, entry.op) },
        h('span', { style: { display: 'inline-flex', alignItems: 'center', gap: 6 } }, h(Icon, { name: 'back', size: 13 }), 'Undo')) : null,
      entry.can_restore ? h(Btn, { kind: 'quiet', title: entry.name + ' was taken off the desk, not destroyed', onClick: () => putBack(entry.deal) }, 'Put back') : null))
      : h('p', { className: 'stoa-sub' }, 'Everything that happens on the desk is logged here: who did it, when, and what changed.')) : null,
    tab === 'setup' ? h(Setup, { state, go: what => { if (what === 'add') add() } }) : null),
  h(Dock, { state }),
  h(SetupFlow))
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
    storage = ctx.storage
    try { $pipe.set(String((storage && storage.get('pipe', '')) || '')) } catch { /* no storage: the first pipeline */ }
    // Every request names the pipeline the page is showing (an opaque id such as p2, never its name).
    rest = (path, options) => {
      const pipe = $pipe.get()
      return ctx.rest(pipe && pipe !== 'main' ? path + (path.includes('?') ? '&' : '?') + 'pipeline=' + encodeURIComponent(pipe) : path, options)
    }
    try { $folds.set({ ...(storage.get('folds', {}) || {}) }) } catch { /* no storage: every group starts closed */ }
    const kept = ctx.storage.get('filter', {}) || {}      // an older version kept every filter, a picked-out stage included: take only these two
    $filter.set({ ...NO_FILTER, owner: typeof kept.owner === 'string' ? kept.owner : '', group: typeof kept.group === 'string' ? kept.group : '' })
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
