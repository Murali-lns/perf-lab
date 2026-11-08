// Perf Lab: proof for speed-ups, inside Hermes Desktop.
// Shows what the plugin itself measured: before/after times, every run as a dot,
// whether the output stayed identical, a flame graph of where the time goes, and history.
// Plain ESM loaded uncompiled: UI is jsx()/jsxs() calls, no JSX syntax.
import {
  host,
  atom,
  useValue,
  useQuery,
  queryClient,
  Button,
  Codicon,
  Switch,
  ConfirmDialog,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  PALETTE_AREA,
  STATUSBAR_AREAS
} from '@hermes/plugin-sdk'
import { useMemo, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'perf-lab'
const PANE_ID = `${ID}:pane`
const TILE_PANE = `plugin-workspace:${ID}`
let CTX = null
let closeTile = null
let disposing = false

const $selected = atom(null)
const $open = atom(false)

const api = (path, opts) => CTX.rest(path, { timeoutMs: 20000, ...(opts || {}) })

const VERDICT = {
  faster: { icon: 'pass-filled', tone: 'good', title: 'Faster, same output' },
  slower: { icon: 'warning', tone: 'warn', title: 'Slower than before' },
  no_change: { icon: 'dash', tone: 'muted', title: 'No real change' },
  inconclusive: { icon: 'question', tone: 'warn', title: 'Not proven: timings too noisy' },
  changed_output: { icon: 'error', tone: 'bad', title: 'Rejected: the output changed' },
  failed: { icon: 'error', tone: 'bad', title: 'The command failed' },
  unstable_output: { icon: 'warning', tone: 'warn', title: 'Output changes between runs' },
  no_comparison: { icon: 'warning', tone: 'warn', title: 'Not comparable with the baseline' },
  baseline: { icon: 'milestone', tone: 'muted', title: 'Baseline recorded' }
}

const fmt = v => (v == null ? '?' : v >= 1 ? `${v.toFixed(2)} s` : `${Math.round(v * 1000)} ms`)
const ago = t => {
  if (!t) return ''
  const s = Math.max(0, Date.now() / 1000 - t)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)} min ago`
  if (s < 86400) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86400)} d ago`
}
const shortFrame = name => {
  const m = /^(.*) \((.*?)(?::(\d+))?\)$/.exec(name || '')
  if (!m) return { fn: name, where: '' }
  const file = m[2].split('/').pop()
  return { fn: m[1], where: m[3] ? `${file}:${m[3]}` : file }
}

// ------------------------------------------------------------------ open / close

const hasTile = () => typeof host.openWorkspace === 'function'

function openLab() {
  if (!hasTile()) {
    if (typeof host.revealPane === 'function') host.revealPane(PANE_ID)
    return
  }
  closeTile = host.openWorkspace(ID, {
    title: 'Perf Lab',
    dock: { pane: 'workspace', pos: 'right' },
    minWidth: '22rem',
    render: () => jsx(Lab, {}),
    onClose: () => {
      closeTile = null
      $open.set(false)
      if (!disposing) CTX?.storage.set('open', false)
    }
  })
  $open.set(true)
  CTX.storage.set('open', true)
}

function toggleLab() {
  if (!hasTile() || !closeTile) return openLab()
  const visible = typeof host.paneVisibility === 'function' ? host.paneVisibility(TILE_PANE).get() : true
  if (!visible) return host.revealPane?.(TILE_PANE)
  closeTile()
}

// ------------------------------------------------------------------ data

function useSummary() {
  return useQuery({ queryKey: [ID, 'summary'], queryFn: () => api('/summary'), refetchInterval: 4000, retry: false })
}

function useDetail(id) {
  return useQuery({
    queryKey: [ID, 'target', id],
    queryFn: () => api(`/target?id=${encodeURIComponent(id)}`),
    enabled: !!id,
    refetchInterval: 4000,
    retry: false
  })
}

// ------------------------------------------------------------------ pieces

function Section({ title, right, children }) {
  return jsxs('section', {
    className: 'pl-sec',
    children: [
      jsxs('div', { className: 'pl-sec-h', children: [jsx('span', { children: title }), right || null] }),
      children
    ]
  })
}

function VerdictCard({ run }) {
  if (!run) {
    return jsx('div', { className: 'pl-card pl-muted', children: 'No measurement yet.' })
  }
  const v = VERDICT[run.verdict] || VERDICT.no_change
  const ratio = run.ratio && ['faster', 'slower', 'inconclusive'].includes(run.verdict) ? `${run.ratio}×` : null
  return jsxs('div', {
    className: `pl-card pl-${v.tone}`,
    children: [
      jsxs('div', {
        className: 'pl-card-top',
        children: [
          jsx(Codicon, { name: v.icon, size: 18 }),
          jsx('strong', { children: v.title }),
          ratio ? jsx('span', { className: 'pl-ratio', children: run.verdict === 'faster' ? `${ratio} faster` : run.verdict === 'inconclusive' ? `${ratio}?` : `${ratio} speed` }) : null
        ]
      }),
      jsxs('div', {
        className: 'pl-nums',
        children: [
          jsxs('div', { children: [jsx('label', { children: 'Before' }), jsx('b', { children: fmt(run.before_s ?? run.base_median ?? run.median) })] }),
          jsx(Codicon, { name: 'arrow-right', size: 14 }),
          jsxs('div', { children: [jsx('label', { children: run.verdict === 'baseline' ? 'Baseline' : 'After' }), jsx('b', { children: fmt(run.after_s ?? run.median) })] }),
          jsxs('div', {
            className: 'pl-out',
            children: [
              jsx('label', { children: 'Output' }),
              jsx('b', {
                children: run.failed ? 'failed' : run.unstable ? 'varies' : run.verdict === 'changed_output' ? 'changed ✗' : run.verdict === 'baseline' ? 'saved' : 'identical ✓'
              })
            ]
          })
        ]
      }),
      jsx('p', { className: 'pl-reason', children: run.reason }),
      run.diff?.length
        ? jsxs('div', {
            className: 'pl-diff',
            children: [
              jsx('div', { className: 'pl-diff-h', children: 'First differences in the output' }),
              ...run.diff.map(d =>
                jsxs('div', {
                  className: 'pl-diff-row',
                  children: [
                    jsx('span', { className: 'pl-ln', children: `line ${d.line}` }),
                    jsxs('div', {
                      children: [
                        jsx('div', { className: 'pl-minus', children: `− ${d.before}` }),
                        jsx('div', { className: 'pl-plus', children: `+ ${d.after}` })
                      ]
                    })
                  ]
                }, d.line)
              )
            ]
          })
        : null,
      run.failed && run.stderr ? jsx('pre', { className: 'pl-err', children: run.stderr }) : null
    ]
  })
}

// Every timed run as a dot: baseline runs on one row, the latest runs on the other.
// When the two clouds do not overlap, the speed-up is not noise.
function RunDots({ base, run }) {
  if (!run || !run.times?.length) return null
  const rows = [run.verdict !== 'baseline' && base ? { label: 'Before', times: base.times || [] } : null, { label: run.verdict === 'baseline' ? 'Baseline' : 'After', times: run.times }].filter(Boolean)
  const all = rows.flatMap(r => r.times)
  const max = Math.max(...all) * 1.08 || 1
  const W = 300
  return jsxs('div', {
    className: 'pl-dots',
    children: [
      ...rows.map((r, i) =>
        jsxs('div', {
          className: 'pl-dots-row',
          children: [
            jsx('span', { children: r.label }),
            jsx('svg', {
              viewBox: `0 0 ${W} 18`,
              preserveAspectRatio: 'none',
              children: [
                jsx('line', { x1: 0, x2: W, y1: 9, y2: 9, className: 'pl-axis' }, 'axis'),
                ...r.times.map((t, j) => jsx('circle', { cx: (t / max) * W, cy: 9, r: 4, className: i === rows.length - 1 && rows.length > 1 ? 'pl-dot-new' : 'pl-dot' }, j))
              ]
            })
          ]
        }, r.label)
      ),
      jsxs('div', { className: 'pl-scale', children: [jsx('span', { children: '0' }), jsx('span', { children: fmt(max) })] })
    ]
  })
}

function History({ history }) {
  if (!history || history.length < 2) return null
  const W = 300
  const H = 46
  const max = Math.max(...history.map(h => h.median)) * 1.1
  const pts = history.map((h, i) => [(i / (history.length - 1)) * W, H - (h.median / max) * (H - 6) - 3])
  return jsxs('div', {
    className: 'pl-hist',
    children: [
      jsx('svg', {
        viewBox: `0 0 ${W} ${H}`,
        preserveAspectRatio: 'none',
        children: [
          jsx('polyline', { points: pts.map(p => p.join(',')).join(' '), className: 'pl-line' }, 'l'),
          ...pts.map((p, i) => jsx('circle', { cx: p[0], cy: p[1], r: 3, className: history[i].verdict === 'changed_output' || history[i].verdict === 'failed' ? 'pl-dot-bad' : 'pl-dot' }, i))
        ]
      }),
      jsxs('div', { className: 'pl-scale', children: [jsx('span', { children: fmt(history[0].median) }), jsx('span', { children: `${history.length} measurements` }), jsx('span', { children: fmt(history[history.length - 1].median) })] })
    ]
  })
}

// Flame graph as an icicle: the whole program on top, what each function calls below it.
// Width = share of time. Click a bar to zoom into it; click the top bar to zoom out.
function Flame({ profile }) {
  const [focus, setFocus] = useState(null)
  const hot = profile?.hotspot?.name
  const root = useMemo(() => {
    if (!profile?.tree) return null
    if (!focus) return profile.tree
    const find = n => (n.name === focus ? n : n.children.reduce((a, c) => a || find(c), null))
    return find(profile.tree) || profile.tree
  }, [profile, focus])
  if (!root) return jsx('div', { className: 'pl-muted pl-pad', children: 'No profile yet. Ask Hermes to run perf_profile.' })

  const rows = []
  const walk = (node, depth, left, width) => {
    if (depth > 18 || width < 1.5) return
    ;(rows[depth] ||= []).push({ node, left, width })
    let x = left
    for (const c of node.children) {
      const w = (c.value / node.value) * width
      walk(c, depth + 1, x, w)
      x += w
    }
  }
  walk(root, 0, 0, 100)
  const total = profile.tree.value || 1
  return jsxs('div', {
    className: 'pl-flame',
    children: rows.map((row, d) =>
      jsx('div', {
        className: 'pl-frow',
        children: row.map(({ node, left, width }, i) => {
          const f = shortFrame(node.name)
          const pct = ((node.value / total) * 100).toFixed(1)
          const isHot = node.name === hot
          return jsx('button', {
            className: `pl-frame${isHot ? ' pl-hot' : ''}${d === 0 ? ' pl-froot' : ''}`,
            style: { left: `${left}%`, width: `${width}%` },
            title: `${node.name}\n${pct}% of samples${node.self ? `, ${((node.self / total) * 100).toFixed(1)}% in its own lines` : ''}`,
            onClick: () => setFocus(d === 0 ? null : node.name),
            'aria-label': `${f.fn}: ${pct}% of time. Click to zoom`,
            children: width > 7 ? jsx('span', { children: d === 0 ? (focus ? '↑ all' : 'all') : `${f.fn} ${pct}%` }) : null
          }, `${d}-${i}`)
        })
      }, d)
    )
  })
}

function TopList({ profile }) {
  if (!profile?.top?.length) return null
  return jsx('div', {
    className: 'pl-top',
    children: profile.top.slice(0, 5).map(t => {
      const f = shortFrame(t.name)
      return jsxs('div', {
        className: `pl-top-row${t.name === profile.hotspot?.name ? ' pl-top-hot' : ''}`,
        title: t.name,
        children: [
          jsx('div', { className: 'pl-bar', style: { width: `${Math.max(2, t.self_pct)}%` } }),
          jsx('span', { className: 'pl-fn', children: f.fn }),
          jsx('span', { className: 'pl-where', children: f.where }),
          jsx('span', { className: 'pl-pct', children: `${t.self_pct}%` })
        ]
      }, t.name)
    })
  })
}

function askRemeasure(target) {
  const text = `Re-measure with Perf Lab: perf_bench(command=${JSON.stringify(target.command)}, cwd=${JSON.stringify(target.cwd)})`
  const sid = host.state.focusedSessionId?.get?.() || host.state.activeSessionId?.get?.() || null
  Promise.resolve(host.composer?.insertText?.(sid, text)).then(ok => {
    if (!ok) {
      CTX.os?.writeClipboard?.(text)
      host.notify({ kind: 'info', message: 'Copied the re-measure request; paste it into a chat.' })
    }
  })
}

// ------------------------------------------------------------------ main view

function Lab() {
  const summary = useSummary()
  const selected = useValue($selected)
  const targets = summary.data?.targets || []
  const id = selected && targets.some(t => t.id === selected) ? selected : targets[0]?.id
  const detail = useDetail(id)
  const [forget, setForget] = useState(false)
  const settings = summary.data?.settings || { gate: true }

  if (summary.isError) {
    return jsxs('div', {
      className: 'pl pl-pad',
      children: [
        jsx('style', { children: CSS }),
        jsx('p', { children: 'Perf Lab backend is not loaded yet.' }),
        jsx('p', { className: 'pl-muted', children: 'Enable the plugin with `hermes plugins enable perf-lab`, then restart the Hermes gateway once.' }),
        typeof host.restartGateway === 'function' ? jsx(Button, { size: 'sm', onClick: () => host.restartGateway(), children: 'Restart gateway' }) : null
      ]
    })
  }

  const d = detail.data
  const runs = d?.runs || []
  const run = runs[runs.length - 1]
  const base = runs.find(r => r.id === d?.target?.baseline)
  const tgt = targets.find(t => t.id === id)

  return jsxs('div', {
    className: 'pl',
    children: [
      jsx('style', { children: CSS }),
      jsxs('div', {
        className: 'pl-head',
        children: [
          jsx(Codicon, { name: 'dashboard', size: 15 }),
          jsx('strong', { children: 'Perf Lab' }),
          jsx('span', { className: 'pl-grow' }),
          jsxs('label', {
            className: 'pl-gate',
            title: 'When on, chats about speed must measure before editing and re-measure before finishing.',
            children: [
              jsx('span', { children: 'Gatekeeper' }),
              jsx(Switch, {
                checked: !!settings.gate,
                onCheckedChange: v => api('/settings', { method: 'POST', body: { gate: !!v } }).then(() => queryClient.invalidateQueries({ queryKey: [ID] }))
              })
            ]
          })
        ]
      }),
      targets.length === 0
        ? jsxs('div', {
            className: 'pl-empty',
            children: [
              jsx(Codicon, { name: 'dashboard', size: 28 }),
              jsx('p', { children: 'Nothing measured yet.' }),
              jsx('p', { className: 'pl-muted', children: 'Ask Hermes to make a script faster. Perf Lab will time it before and after, check the output did not change, and show the proof here.' })
            ]
          })
        : jsxs('div', {
            className: 'pl-body',
            children: [
              jsxs('div', {
                className: 'pl-pick',
                children: [
                  jsxs(Select, {
                    value: id,
                    onValueChange: v => $selected.set(v),
                    children: [
                      jsx(SelectTrigger, { className: 'pl-select', children: jsx(SelectValue, {}) }),
                      jsx(SelectContent, {
                        children: targets.map(t => jsx(SelectItem, { value: t.id, children: `${t.label} · ${ago(t.updated)}` }, t.id))
                      })
                    ]
                  }),
                  tgt?.stale ? jsx('span', { className: 'pl-stale', title: 'The code changed after the last measurement', children: 'out of date' }) : null
                ]
              }),
              jsx(VerdictCard, { run }),
              jsx(RunDots, { base, run }),
              jsx(Section, {
                title: 'Where the time goes',
                right: d?.profile ? jsx('span', { className: 'pl-muted', children: `${d.profile.engine} · ${d.profile.samples} samples · ${ago(d.profile.at)}` }) : null,
                children: jsxs('div', { children: [jsx(Flame, { profile: d?.profile }), jsx(TopList, { profile: d?.profile })] })
              }),
              tgt?.history?.length > 1 ? jsx(Section, { title: 'History (typical time per measurement)', children: jsx(History, { history: tgt.history }) }) : null,
              jsxs('div', {
                className: 'pl-foot',
                children: [
                  jsx('code', { title: tgt?.cwd, children: tgt?.command }),
                  jsxs('div', {
                    className: 'pl-actions',
                    children: [
                      jsx(Button, { size: 'sm', variant: 'secondary', onClick: () => tgt && askRemeasure(tgt), children: 'Ask Hermes to re-measure' }),
                      jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setForget(true), children: 'Forget' })
                    ]
                  })
                ]
              }),
              jsx(ConfirmDialog, {
                open: forget,
                onClose: () => setForget(false),
                title: `Forget ${tgt?.label || 'this script'}?`,
                description: 'Deletes its measurements and profiles from Perf Lab. Your code is not touched.',
                confirmLabel: 'Forget',
                destructive: true,
                onConfirm: () =>
                  api('/forget', { method: 'POST', body: { id } }).then(() => {
                    $selected.set(null)
                    queryClient.invalidateQueries({ queryKey: [ID] })
                  })
              })
            ]
          })
    ]
  })
}

function Chip() {
  const summary = useSummary()
  const run = summary.data?.targets?.[0]?.latest
  const v = run ? VERDICT[run.verdict] : null
  return jsx(Button, {
    variant: 'ghost',
    size: 'sm',
    title: 'Perf Lab',
    onClick: () => toggleLab(),
    style: { gap: 4, fontSize: 11 },
    children: [
      jsx(Codicon, { name: v ? v.icon : 'dashboard', size: 13 }, 'i'),
      run && run.verdict === 'faster' ? jsx('span', { children: `${run.ratio}×` }, 't') : null
    ]
  })
}

// ------------------------------------------------------------------ styles
const CSS = `
.pl{display:flex;flex-direction:column;height:100%;min-height:0;font-size:12.5px;color:var(--ui-text-primary)}
.pl *{box-sizing:border-box}
.pl-pad{padding:14px}
.pl-head{display:flex;gap:8px;align-items:center;padding:8px 10px;border-bottom:1px solid var(--ui-stroke-secondary)}
.pl-grow{flex:1}
.pl-gate{display:flex;gap:6px;align-items:center;color:var(--ui-text-secondary);font-size:11.5px;cursor:pointer}
.pl-body{flex:1;min-height:0;overflow:auto;padding:10px;display:flex;flex-direction:column;gap:10px}
.pl-empty{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;padding:24px;text-align:center;color:var(--ui-text-secondary)}
.pl-muted{color:var(--ui-text-tertiary)}
.pl-pick{display:flex;gap:8px;align-items:center}
.pl-select{flex:1;min-width:0}
.pl-stale{font-size:11px;padding:2px 6px;border-radius:4px;border:1px solid var(--ui-warning, var(--ui-accent));color:var(--ui-warning, var(--ui-accent))}
.pl-card{border:1px solid var(--ui-stroke-secondary);border-left-width:4px;border-radius:6px;padding:10px 12px;display:flex;flex-direction:column;gap:8px}
.pl-good{border-left-color:var(--ui-green, var(--ui-success, var(--ui-accent)))}
.pl-good .pl-card-top{color:var(--ui-green, var(--ui-success, var(--ui-accent)))}
.pl-bad{border-left-color:var(--ui-red, var(--ui-danger))}
.pl-bad .pl-card-top{color:var(--ui-red, var(--ui-danger))}
.pl-warn{border-left-color:var(--ui-warning, var(--ui-accent))}
.pl-warn .pl-card-top{color:var(--ui-warning, var(--ui-accent))}
.pl-card-top{display:flex;gap:8px;align-items:center;font-size:13.5px}
.pl-ratio{margin-left:auto;font-size:20px;font-weight:700}
.pl-nums{display:flex;gap:14px;align-items:flex-end;color:var(--ui-text-primary)}
.pl-nums label{display:block;font-size:10.5px;text-transform:uppercase;letter-spacing:.04em;color:var(--ui-text-tertiary)}
.pl-nums b{font-size:17px}
.pl-out{margin-left:auto;text-align:right}
.pl-reason{margin:0;color:var(--ui-text-secondary);font-size:11.5px}
.pl-diff{border-top:1px solid var(--ui-stroke-tertiary);padding-top:6px;font-family:var(--font-mono, monospace);font-size:11px}
.pl-diff-h{font-family:inherit;color:var(--ui-text-secondary);margin-bottom:4px}
.pl-diff-row{display:flex;gap:8px;margin:3px 0}
.pl-ln{color:var(--ui-text-tertiary);min-width:52px}
.pl-minus{color:var(--ui-red, var(--ui-danger))}
.pl-plus{color:var(--ui-green, var(--ui-success, var(--ui-accent)))}
.pl-err{margin:0;max-height:120px;overflow:auto;font-size:11px;white-space:pre-wrap;color:var(--ui-text-secondary)}
.pl-dots{display:flex;flex-direction:column;gap:2px;padding:0 2px}
.pl-dots-row{display:flex;gap:8px;align-items:center}
.pl-dots-row span{width:54px;font-size:11px;color:var(--ui-text-tertiary)}
.pl-dots-row svg{flex:1;height:18px}
.pl-axis{stroke:var(--ui-stroke-tertiary);stroke-width:1}
.pl-dot{fill:var(--ui-text-tertiary)}
.pl-dot-new{fill:var(--ui-accent)}
.pl-dot-bad{fill:var(--ui-red, var(--ui-danger))}
.pl-scale{display:flex;justify-content:space-between;padding-left:62px;font-size:10px;color:var(--ui-text-quaternary)}
.pl-hist .pl-scale{padding-left:0}
.pl-sec{display:flex;flex-direction:column;gap:6px}
.pl-sec-h{display:flex;justify-content:space-between;gap:8px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--ui-text-secondary)}
.pl-sec-h .pl-muted{text-transform:none;letter-spacing:0}
.pl-flame{display:flex;flex-direction:column;gap:1px}
.pl-frow{position:relative;height:20px}
.pl-frame{position:absolute;top:0;height:20px;border:0;border-radius:2px;padding:0 4px;overflow:hidden;cursor:pointer;text-align:left;font:inherit;font-size:10.5px;color:var(--ui-text-primary);background:color-mix(in srgb, var(--ui-accent) 22%, transparent);box-shadow:inset -1px 0 0 var(--ui-bg-editor, transparent)}
.pl-frame:hover{background:color-mix(in srgb, var(--ui-accent) 38%, transparent)}
.pl-froot{background:color-mix(in srgb, var(--ui-text-tertiary) 18%, transparent)}
.pl-hot{background:color-mix(in srgb, var(--ui-red, var(--ui-danger)) 45%, transparent);font-weight:600}
.pl-hot:hover{background:color-mix(in srgb, var(--ui-red, var(--ui-danger)) 60%, transparent)}
.pl-frame span{white-space:nowrap}
.pl-top{display:flex;flex-direction:column;gap:2px;margin-top:6px}
.pl-top-row{position:relative;display:flex;gap:8px;align-items:center;padding:3px 6px;border-radius:3px;overflow:hidden}
.pl-bar{position:absolute;left:0;top:0;bottom:0;background:color-mix(in srgb, var(--ui-accent) 14%, transparent)}
.pl-top-hot .pl-bar{background:color-mix(in srgb, var(--ui-red, var(--ui-danger)) 22%, transparent)}
.pl-fn{position:relative;font-family:var(--font-mono, monospace);font-size:11.5px}
.pl-where{position:relative;flex:1;color:var(--ui-text-tertiary);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pl-pct{position:relative;font-variant-numeric:tabular-nums}
.pl-hist svg{width:100%;height:46px}
.pl-line{fill:none;stroke:var(--ui-accent);stroke-width:1.5}
.pl-foot{display:flex;flex-direction:column;gap:6px;border-top:1px solid var(--ui-stroke-tertiary);padding-top:8px}
.pl-foot code{font-size:11px;color:var(--ui-text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pl-actions{display:flex;gap:6px}
`

export default {
  id: ID,
  name: 'Perf Lab',
  register(ctx) {
    CTX = ctx
    disposing = false
    const items = [
      {
        id: 'open',
        area: PALETTE_AREA,
        data: { id: 'open', label: 'Perf Lab: Open / close', keywords: ['performance', 'benchmark', 'profile', 'speed', 'flame'], run: () => toggleLab() }
      },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 55, render: () => jsx(Chip, {}) }
    ]
    if (!hasTile()) {
      items.unshift({ id: 'pane', area: 'panes', title: 'Perf Lab', data: { placement: 'right', width: '440px' }, render: () => jsx(Lab, {}) })
    }
    ctx.registerMany(items)
    ctx.onDispose?.(() => {
      disposing = true
      closeTile?.()
    })
    if (hasTile() && ctx.storage.get('open', false)) openLab()
  }
}
