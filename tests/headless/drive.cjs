const { chromium } = require('playwright')
;(async () => {
  const b = await chromium.launch(process.env.CHROME ? { executablePath: process.env.CHROME } : {})
  const p = await b.newPage({ viewport: { width: 460, height: 900 }, deviceScaleFactor: 2 })
  const errors = []
  p.on('pageerror', e => errors.push(String(e)))
  p.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
  const port = process.env.PORT
  await p.goto(`http://127.0.0.1:${port}/`)
  await p.waitForSelector('.pl-card', { timeout: 15000 })
  await p.waitForSelector('.pl-frame', { timeout: 15000 })
  await p.screenshot({ path: `${process.env.SHOTS}/pane-faster.png` })
  const card = await p.textContent('.pl-card')
  console.log('CARD', card)
  console.log('HOT', await p.textContent('.pl-top-hot'))
  // zoom into the hot function
  await p.click('.pl-frame.pl-hot')
  await p.screenshot({ path: `${process.env.SHOTS}/pane-zoom.png` })
  // switch to the rejected target
  const targets = await p.evaluate(() => fetch('/api/summary').then(r => r.json()))
  const wrong = targets.targets.find(t => t.label === 'signals.py')
  await p.evaluate(id => window.__select(id), wrong.id)
  await p.waitForSelector('.pl-card.pl-bad', { timeout: 15000 })
  await p.screenshot({ path: `${process.env.SHOTS}/pane-rejected.png` })
  console.log('CARD2', await p.textContent('.pl-card'))
  console.log('TARGETS', targets.targets.map(t => `${t.label}:${t.latest.verdict}:${t.latest.ratio}`).join(' | '))
  console.log('ERRORS', JSON.stringify(errors))
  await b.close()
})()
