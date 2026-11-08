// Minimal stand-in for @hermes/plugin-sdk: real React + React Query, simple widgets.
import React, { useSyncExternalStore } from 'react'
import { QueryClient, useQuery as uq } from '@tanstack/react-query'
export const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
export const useQuery = o => uq(o, queryClient)
export function atom(v) { let val = v; const subs = new Set(); return { get: () => val, set: n => { val = n; subs.forEach(f => f()) }, subscribe: f => { subs.add(f); return () => subs.delete(f) } } }
export const useValue = a => useSyncExternalStore(a.subscribe, a.get)
window.__log = []
export const host = {
  state: { focusedSessionId: atom('sess-main'), activeSessionId: atom('sess-main') },
  notify: n => window.__log.push(['notify', n]),
  composer: { insertText: async (sid, text) => { window.__log.push(['insertText', text]); return true } },
  openWorkspace: (id, o) => { window.__tile = o; window.dispatchEvent(new Event('tile')); return () => {} },
  paneVisibility: () => ({ get: () => true }), revealPane: () => {}
}
const ICON = { 'pass-filled': '✔', error: '✖', warning: '⚠', milestone: '◆', 'arrow-right': '→', dashboard: '⏱', dash: '–', question: '?' }
export const Codicon = ({ name, size = 14 }) => <span style={{ fontSize: size, lineHeight: 1 }}>{ICON[name] || '•'}</span>
export const Button = ({ children, variant, size, ...r }) => <button {...r} style={{ font: 'inherit', fontSize: 12, padding: '3px 9px', borderRadius: 4, cursor: 'pointer', border: variant === 'ghost' ? '1px solid transparent' : '1px solid var(--ui-stroke-primary)', background: variant ? 'transparent' : 'var(--ui-accent)', color: 'var(--ui-text-primary)', ...(r.style || {}) }}>{children}</button>
export const Switch = ({ checked, onCheckedChange }) => <button onClick={() => onCheckedChange(!checked)} style={{ width: 30, height: 16, borderRadius: 8, border: 0, background: checked ? 'var(--ui-accent)' : 'var(--ui-stroke-primary)', position: 'relative' }}><span style={{ position: 'absolute', top: 2, left: checked ? 16 : 2, width: 12, height: 12, borderRadius: 6, background: '#fff' }} /></button>
let SEL = null
export const Select = ({ value, onValueChange, children }) => { SEL = value; window.__select = onValueChange; return <div>{children}</div> }
export const SelectTrigger = ({ children }) => <div style={{ flex: 1, border: '1px solid var(--ui-stroke-secondary)', borderRadius: 4, padding: '4px 8px', background: 'var(--ui-bg-secondary)' }}>{children}</div>
export const SelectValue = () => <span id="selval">{window.__labels?.[SEL] || SEL} ▾</span>
export const SelectContent = ({ children }) => <div style={{ display: 'none' }}>{children}</div>
export const SelectItem = ({ value, children }) => { (window.__labels ||= {})[value] = children; return null }
export const ConfirmDialog = () => null
export const PALETTE_AREA = 'palette'
export const STATUSBAR_AREAS = { right: 'sb.right' }
