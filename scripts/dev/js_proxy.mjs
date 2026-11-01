// js_proxy.mjs — 给"真机驱动"做一个本地替身
//
// `app/ui_web/e2e_driver.py` 只用 `window.evaluate_js` 碰页面。本机没有
// WebView2，就用 Playwright 的 Chromium 顶替页面：把 evaluate_js 收到
// HTTP 上（POST /eval），驱动代码一行不改就能在本地跑一遍。
//
// 它不替代真机验证，只保证"驱动里的断言与点击逻辑"本身是对的。
//
// 用法：node scripts/dev/js_proxy.mjs <bridgeUrl> <port>
import { createRequire } from 'node:module'
import { createServer } from 'node:http'

const require = createRequire(new URL('../../web/package.json', import.meta.url))
const { chromium } = require('playwright')

const url = process.argv[2]
const port = Number(process.argv[3] || 0)

const browser = await chromium.launch({ args: ['--no-sandbox'] })
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
await page.goto(url, { waitUntil: 'load' })
await page.waitForSelector('aside', { timeout: 30000 })

const server = createServer((req, res) => {
  let body = ''
  req.on('data', (chunk) => { body += chunk })
  req.on('end', async () => {
    const reply = (payload) => {
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' })
      res.end(JSON.stringify(payload))
    }
    if (req.url.startsWith('/close')) {
      reply({ ok: true })
      setTimeout(() => process.exit(0), 50)
      return
    }
    if (!req.url.startsWith('/eval')) {
      res.writeHead(404)
      res.end('{}')
      return
    }
    try {
      const { code } = JSON.parse(body || '{}')
      const value = await page.evaluate(code)
      reply({ ok: true, value: value === undefined ? null : value })
    } catch (err) {
      reply({ ok: false, error: String(err) })
    }
  })
})

server.listen(port, '127.0.0.1', () => {
  console.log(`js proxy on http://127.0.0.1:${server.address().port}`)
})
