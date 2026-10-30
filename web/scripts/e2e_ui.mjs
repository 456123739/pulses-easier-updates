// 真前端 + 真 Api + 真流程的端到端（浏览器里点，Python 干活）。
// 用法：node scripts/dev/e2e_ui.mjs <bridgeUrl> <workdir>
import { chromium } from 'playwright'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const url = process.argv[2] || 'http://127.0.0.1:8123/index.html'
const work = process.argv[3]
// CI 用 playwright 自己装的内核；本机可以指定 CHROMIUM 覆盖
const exe = process.env.CHROMIUM || ''

const fixture = JSON.parse(readFileSync(join(work, 'fixture.json'), 'utf8'))
let failed = 0
const check = (ok, label, extra = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !extra ? '' : '  →  ' + extra}`)
  if (!ok) failed++
}

const browser = await chromium.launch({
  ...(exe ? { executablePath: exe } : {}),
  args: ['--no-sandbox'],
})
const page = await browser.newPage({ viewport: { width: 1440, height: 860 } })
const errors = []
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', (e) => errors.push(String(e)))

// 事件泵在轮询 /api/events，永远不会 networkidle
await page.goto(url, { waitUntil: 'load' })
await page.waitForSelector('aside', { timeout: 30000 })
check(true, '界面渲染')

// 真桥：定位整合包（等价于点"选择"拿到路径后调用）
let res = await page.evaluate(
  (p) => window.pywebview.api.invoke('app.pick_pack', { path: p }),
  fixture.pack,
)
check(res?.ok === true, '定位整合包', JSON.stringify(res?.error ?? ''))
await page.waitForFunction(() => document.body.innerText.includes('E2EPack'), null, { timeout: 20000 })
check(true, '整合包信息显示')

res = await page.evaluate(
  (p) => window.pywebview.api.invoke('update.pick', { path: p }),
  fixture.update,
)
check(res?.ok === true, '导入更新包', JSON.stringify(res?.error ?? ''))
await page.waitForFunction(() => document.body.innerText.includes('E2EPack-3.0.0'), null, { timeout: 20000 })

await page.getByRole('button', { name: '开始更新' }).click()
await page.waitForFunction(() => document.body.innerText.includes('变更内容'), null, { timeout: 120000 })
check(true, '比对完成')

await page.getByRole('button', { name: '确认更新' }).click()
await page.waitForFunction(() => /更新完成|已应用/.test(document.body.innerText), null, { timeout: 180000 })
check(true, '更新应用完成')

await page.waitForTimeout(500)
await page.screenshot({ path: join(work, 'e2e.png') })

// 磁盘为准
const fs = await import('node:fs/promises')
const read = (p) => fs.readFile(join(fixture.pack, p), 'utf8')
check((await read('mods/old.jar')) === 'NEW', 'mods 已替换')
check((await read('mods/added.jar')) === 'ADDED', '新增文件已落地')
check(!(await fs.stat(join(fixture.pack, 'mods/gone.jar')).then(() => true).catch(() => false)), '本地多余文件已删除')
check((await read('config/mine.toml')) === 'mine = 1\n', '玩家自己的配置没被动')
check((await read('config/sodium-options.json')) === '{"render_distance": 8}\n', '智能剔除保护生效')

check(errors.length === 0, '无控制台错误', errors.join(' | ').slice(0, 300))
await browser.close()
console.log(failed ? `\n${failed} 项失败` : '\n全部通过')
process.exit(failed ? 1 : 0)
