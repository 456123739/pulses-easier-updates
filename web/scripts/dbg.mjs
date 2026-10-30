import { chromium } from 'playwright'
const exe = '/home/admin/.cache/ms-playwright/chromium-1194/chrome-linux/chrome'
const b = await chromium.launch({ executablePath: exe, args: ['--no-sandbox'] })
const p = await b.newPage({ viewport: { width: 1440, height: 860 } })
p.on('console', m => console.log('console:', m.type(), m.text()))
p.on('pageerror', e => console.log('pageerror:', String(e)))
await p.goto('http://127.0.0.1:8099/index.html', { waitUntil: 'networkidle' })
await p.waitForTimeout(2500)
const info = await p.evaluate(() => {
  const out = []
  const walk = (el, depth) => {
    if (depth > 3) return
    const r = el.getBoundingClientRect()
    out.push(`${'  '.repeat(depth)}<${el.tagName.toLowerCase()} class="${el.className}"> ${Math.round(r.x)},${Math.round(r.y)} ${Math.round(r.width)}x${Math.round(r.height)} pos=${getComputedStyle(el).position}`)
    for (const c of el.children) walk(c, depth + 1)
  }
  walk(document.getElementById('root'), 0)
  return out.join('\n')
})
console.log(info)
await p.screenshot({ path: 'shots/dbg.png' })
await b.close()
