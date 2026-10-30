// 用 Playwright 给 dist 截图（不需要 Windows / pywebview）。
// 用法：node scripts/shot.mjs <url> <out.png> [width] [height]
import { chromium } from 'playwright'

const url = process.argv[2] || 'http://127.0.0.1:8099/index.html'
const out = process.argv[3] || 'shots/shot.png'
const width = Number(process.argv[4] || 1440)
const height = Number(process.argv[5] || 860)

const exe = process.env.CHROMIUM || '/home/admin/.cache/ms-playwright/chromium-1194/chrome-linux/chrome'
const browser = await chromium.launch({ executablePath: exe, args: ['--no-sandbox', '--force-device-scale-factor=1'] })
const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 })
const errors = []
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', (e) => errors.push(String(e)))
await page.goto(url, { waitUntil: 'networkidle' })
await page.waitForTimeout(600)
await page.screenshot({ path: out })
await browser.close()
if (errors.length) {
  console.error('console errors:\n' + errors.join('\n'))
  process.exit(1)
}
console.log('saved', out)
