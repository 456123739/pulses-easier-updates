// harness_run.mjs — 用 Playwright 打开 `?e2e=1` 的页面，让页面内驱动自己跑完
// ----------------------------------------------------------------------
// 和真机（pywebview + WebView2）走的是同一份驱动代码 `web/src/e2e/harness.ts`，
// 这里只负责"开页面 / 等报告 / 把 steps 打出来 / 截图"，不参与任何点击与断言。
//
// 用法：node scripts/dev/harness_run.mjs <url> <report.json> <workdir> [timeoutSec]
// 报告由 bridge_server 里的 `api.e2e_report` 写入（真机上是同一个方法）。
import { createRequire } from 'node:module'
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

const require = createRequire(new URL('../../web/package.json', import.meta.url))
const { chromium } = require('playwright')

const url = process.argv[2] || 'http://127.0.0.1:8123/index.html?e2e=1'
const reportPath = process.argv[3] || 'e2e-report.json'
const work = process.argv[4] || '.'
const timeout = Number(process.argv[5] || 900) * 1000
// CI 用 playwright 自己装的内核；本机可以指定 CHROMIUM 覆盖
const exe = process.env.CHROMIUM || ''

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const errors = []

const browser = await chromium.launch({
  ...(exe ? { executablePath: exe } : {}),
  args: ['--no-sandbox'],
})
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
page.on('console', (m) => {
  const text = m.text()
  if (m.type() === 'error') errors.push(text)
  if (text.startsWith('[e2e]')) console.log(text)
})
page.on('pageerror', (e) => errors.push(String(e)))

let report = null
let fatal = ''
let progress = false
const started = Date.now()
page.setDefaultTimeout(8000)
try {
  await page.goto(url, { waitUntil: 'load' })
  let nextProbe = Date.now() + 3000
  while (Date.now() - started < timeout) {
    if (existsSync(reportPath)) {
      try {
        report = JSON.parse(readFileSync(reportPath, 'utf8'))
      } catch {
        report = null          // 还在写
      }
      if (report) break
    }
    // 页面自己说"报告送不出去"（没有 e2e_report 入口 / 回传超时）就别等满超时
    if (!fatal && Date.now() >= nextProbe) {
      nextProbe = Date.now() + 5000
      try {
        const status = await page.evaluate(() => window.__pe_e2e ?? null)
        if (status?.fatal) fatal = String(status.fatal)
        else if (status && status.steps) {
          progress = true
          process.stdout.write(`\r已跑 ${status.steps} 条断言…`)
        }
      } catch {
        /* 页面卡住/还没起来：继续等 */
      }
    }
    if (fatal) break
    await sleep(400)
  }
} catch (err) {
  console.log(`FAIL  页面打开失败：${String(err).slice(0, 400)}`)
}

try {
  await page.screenshot({ path: join(work, 'harness.png') })
} catch {
  /* 截图失败不影响结论 */
}

const elapsed = Math.round((Date.now() - started) / 1000)
if (progress) console.log('')
if (!report && fatal) {
  console.log(`FAIL  页面内驱动无法回传报告：${fatal}（${elapsed}s）`)
  if (errors.length) console.log(`控制台错误：${errors.join(' | ').slice(0, 600)}`)
  await browser.close()
  process.exit(1)
}
if (!report) {
  console.log(`FAIL  等不到报告（${elapsed}s）：${reportPath}`)
  if (errors.length) console.log(`控制台错误：${errors.join(' | ').slice(0, 600)}`)
  await browser.close()
  process.exit(1)
}

for (const step of report.steps ?? []) {
  console.log(`${step.ok ? 'PASS' : 'FAIL'}  ${step.name}`
    + (step.ok || !step.detail ? '' : `  →  ${step.detail}`))
}
if (!report.ok) {
  console.log(`FAIL  页面内驱动结论 ok=false：${report.error || ''}`)
}
if (errors.length) {
  console.log(`WARN  控制台错误：${errors.join(' | ').slice(0, 600)}`)
}
console.log(`\n页面内驱动：${(report.steps ?? []).length} 条断言，`
  + `${report.ok ? '全部通过' : '有失败'}（${elapsed}s，页面自报 ${report.ms ?? 0} ms）`)

await browser.close()
process.exit(report.ok ? 0 : 1)
