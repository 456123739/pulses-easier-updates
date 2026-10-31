import { chromium } from 'playwright'
const exe = process.env.CHROMIUM || '/home/admin/.cache/ms-playwright/chromium-1194/chrome-linux/chrome'
const b = await chromium.launch({ executablePath: exe, args: ['--no-sandbox'] })
const p = await b.newPage({ viewport: { width: 1440, height: 860 } })
await p.goto('http://127.0.0.1:8099/index.html', { waitUntil: 'domcontentloaded' })
await p.waitForTimeout(260)
await p.screenshot({ path: 'shots/splash.png' })
await b.close()
console.log('saved')
