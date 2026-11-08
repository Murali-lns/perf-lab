import React from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import plugin from './plugin.js'
import { queryClient, atom } from './sdk.jsx'
const store = { open: true }
const ctx = {
  storage: { get: (k, d) => (k in store ? store[k] : d), set: (k, v) => { store[k] = v } },
  registerMany: () => {}, onDispose: () => {}, os: { writeClipboard() {} },
  rest: async (path, o = {}) => { const r = await fetch('/api' + path, { method: o.method || 'GET', headers: { 'content-type': 'application/json' }, body: o.body ? JSON.stringify(o.body) : undefined }); const j = await r.json(); if (!r.ok) throw new Error(j.detail); return j }
}
plugin.register(ctx)
function Root() { const [o, setO] = React.useState(window.__tile); React.useEffect(() => { const f = () => setO(window.__tile); window.addEventListener('tile', f); return () => window.removeEventListener('tile', f) }, []); return <QueryClientProvider client={queryClient}><div style={{ width: 460, height: '100vh', position: 'relative', borderLeft: '1px solid var(--ui-stroke-secondary)' }}>{o ? o.render() : null}</div></QueryClientProvider> }
createRoot(document.getElementById('root')).render(<Root />)
