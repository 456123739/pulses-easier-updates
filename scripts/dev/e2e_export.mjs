// 实战端到端：真前端 + 真 Api + 真磁盘，操作"开发者端 → 本地整合包目录 → 导出"。
// 用法（仓库根目录）：
//   node scripts/dev/e2e_export.mjs --instance /tmp/e2e-pack/E2E-Pack \
//        --out /tmp/e2e-pack/out/ui.eapack [--cfkey <key>] [--shots /tmp/shots]
import { createRequire } from 'node:module'
import { spawn } from 'node:child_process'
import { mkdirSync, existsSync, statSync } from 'node:fs'

const require = createRequire(new URL('../../web/package.json', import.meta.url))
const { chromium } = require('playwright')
import { basename, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const argv = process.argv.slice(2)
const arg = (name, def = '') => {
  const i = argv.indexOf(`--${name}`)
  return i >= 0 && argv[i + 1] ? argv[i + 1] : def
}
const INSTANCE = resolve(arg('instance'))
const OUT = resolve(arg('out', '/tmp/e2e-ui/out.eapack'))
const CFKEY = arg('cfkey', '')
const SHOTS = arg('shots', '')
const PORT = Number(arg('port', '8131'))
const PAGE_URL = `http://127.0.0.1:${PORT}/index.html`
const repo = fileURLToPath(new URL('../..', import.meta.url))

let failed = 0
const check = (ok, label, extra = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !extra ? '' : '  →  ' + extra}`)
  if (!ok) failed++
}

const env = { ...process.env, HOME: '/tmp/e2e-home' }
mkdirSync('/tmp/e2e-home', { recursive: true })
const PY = process.env.PYTHON || 'python3'
const server = spawn(PY, ['scripts/dev/bridge_server.py', '--port', String(PORT)], {
  cwd: repo, env, stdio: 'inherit',
})
const stop = () => { try { server.kill('SIGTERM') } catch { /* ignore */ } }
process.on('exit', stop)

// 等桥就绪
for (let i = 0; i < 60; i++) {
  try {
    const r = await fetch(`http://127.0.0.1:${PORT}/api/invoke`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ method: 'app.bootstrap', params: {} }),
    })
    if (r.ok) break
  } catch { /* not yet */ }
  await new Promise((r) => setTimeout(r, 300))
}

const post = (path, body) => fetch(`http://127.0.0.1:${PORT}${path}`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {}),
}).then((r) => r.json())

await post('/api/pick_queue', { paths: [INSTANCE, OUT] })

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM || undefined,
  args: ['--no-sandbox'],
})
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
const errors = []
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', (e) => errors.push(String(e)))

await page.goto(PAGE_URL, { waitUntil: 'domcontentloaded' })
await page.waitForSelector('aside', { timeout: 30000 })
await page.waitForTimeout(1200)
mkdirSync(dirname(OUT), { recursive: true })

const shot = async (name) => {
  if (!SHOTS) return
  mkdirSync(SHOTS, { recursive: true })
  await page.screenshot({ path: `${SHOTS}/${name}.png` })
}

// 1) 进开发者端
await page.getByRole('button', { name: '开发者', exact: true }).click()
await page.waitForTimeout(500)
check(await page.getByText('开始制作更新包').first().isVisible(), '开发者端可见')

// 2) 切到「本地整合包目录」并选目录
await page.getByRole('button', { name: '本地整合包目录', exact: true }).click()
await page.waitForTimeout(300)
await page.getByRole('button', { name: /选择目录|更换目录/ }).first().click()
await page.waitForTimeout(1200)
const box = await page.locator('body').innerText()
check(box.includes(basename(INSTANCE)), '实例名显示在摘要卡里', basename(INSTANCE))
check(/1\.20\.1/.test(box), 'MC 版本显示')
check(/Fabric/i.test(box), '加载器显示')
await shot('dev-instance')

// 3) 填 CurseForge Key（可选）
if (CFKEY) {
  const keyInput = page.locator('input').filter({ hasNot: page.locator('[type="checkbox"]') })
  const input = page.locator('input[placeholder*="Key"], input[placeholder*="key"]').first()
  const target = (await input.count()) ? input : keyInput.last()
  await target.fill(CFKEY)
  await page.getByRole('button', { name: '保存', exact: true }).first().click()
  await page.waitForTimeout(300)
}

// 4) 导出
await page.getByRole('button', { name: '开始制作更新包' }).click()
let done = false
for (let i = 0; i < 240; i++) {          // 最多 4 分钟
  await page.waitForTimeout(1000)
  const ok = await page.evaluate(async () => {
    const r = await window.pywebview.api.invoke('dev.state', {})
    return r?.data?.phase === 'done' || r?.data?.phase === 'error'
      ? r.data : null
  })
  if (ok) { done = ok.phase === 'done'; break }
}
check(done, '导出完成（phase=done）')
await page.waitForTimeout(400)
await shot('dev-done')

const state = await page.evaluate(async () => {
  const r = await window.pywebview.api.invoke('dev.state', {})
  return r?.data || {}
})
const produced = state.outPath || OUT       // 界面自己推的输出路径以快照为准
check(existsSync(produced), `产物落盘：${produced}`,
      existsSync(produced) ? `${statSync(produced).size} B` : '')

// 5) 让 Python 读回产物做结构断言
const { spawnSync } = await import('node:child_process')
const probe = spawnSync(PY, ['-c', `
import json, sys, zipfile
p = ${JSON.stringify(produced)}
with zipfile.ZipFile(p) as zf:
    names = zf.namelist()
    idx = json.loads(zf.read('modrinth.index.json'))
    man = json.loads(zf.read('ea_manifest.json'))
print(json.dumps({
  'files': len(idx['files']),
  'urls': [f['downloads'][0] for f in idx['files']],
  'overrides': [n for n in names if n.startswith('overrides/')],
  'source': man.get('source'),
  'deps': idx['dependencies'],
  'hasManifest': 'ea_manifest.json' in names,
}, ensure_ascii=False))
`], { encoding: 'utf-8' })
if (probe.status !== 0) {
  check(false, '产物结构可解析', probe.stderr)
} else {
  const info = JSON.parse(probe.stdout)
  check(info.hasManifest && info.source === 'instance', '产物是本地实例导出的 eapack')
  const minIndex = Number(arg('min-index', '0'))
  check(info.files >= minIndex, `index 有 ${info.files} 个网络资源（期望≥${minIndex}）`)
  check(info.urls.every((u) => u.startsWith('http')), 'index 里都是 http 直链', info.urls[0] || '')
  check(info.overrides.some((n) => n.includes('config/')), '本地文件进了 overrides')
  check(!info.overrides.some((n) => n.endsWith('options.txt')), 'options.txt 未进包')
  check(info.deps.minecraft === '1.20.1', `依赖版本：${JSON.stringify(info.deps)}`)
}

check(errors.length === 0, '无控制台错误', errors.slice(0, 2).join(' | '))

await browser.close()
stop()
console.log(failed ? `\n${failed} 项失败` : '\n全部通过')
process.exit(failed ? 1 : 0)
