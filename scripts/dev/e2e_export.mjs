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
check(await page.getByText('第一步 · 选择导出内容').first().isVisible(), '开发者端可见（第一步）')

// 2) STEP 1 现在是统一入口：直接选目录（实例目录 / 导出包都走这里）
await page.getByRole('button', { name: /选择目录/ }).first().click()
// 等后台扫描结束（phase 回 idle 且有实例），别拿"正在识别"的界面做断言
let devReady = null
for (let i = 0; i < 150; i++) {
  await page.waitForTimeout(200)
  devReady = await page.evaluate(async () => {
    const r = await window.pywebview.api.invoke('dev.state', {})
    return r?.data ?? null
  })
  if (devReady?.phase === 'idle' && devReady?.instance) break
}
check(Boolean(devReady?.instance), '实例已识别（phase=idle）')
await page.waitForTimeout(300)
const box = await page.locator('body').innerText()
check(box.includes(basename(INSTANCE)), '实例名显示在摘要卡里', basename(INSTANCE))
check(/1\.20\.1/.test(box), 'MC 版本显示')
check(/Fabric/i.test(box), '加载器显示')
await shot('dev-instance')

// 动态子项首次默认展开；折叠与父级选择相互独立。
for (const folder of ['saves', 'resourcepacks', 'shaderpacks']) {
  const group = page.locator(`[data-scope-row="${folder}"]`)
  const heading = group.getByRole('button', { name: folder, exact: true })
  check(await group.getByRole('checkbox', { name: `导出 ${folder}`, exact: true }).getAttribute('aria-checked') === 'true',
    `${folder} 默认勾选`)
  check(await heading.getAttribute('aria-expanded') === 'true' && await group.locator('[data-scope-child]').count() > 0,
    `${folder} 子项默认展开`)
  await heading.click()
  check(await group.locator('[data-scope-child]').count() === 0, `${folder} 主条目可折叠`)
  await heading.click()
  check(await group.locator('[data-scope-child]').count() > 0 &&
    await group.getByRole('checkbox', { name: `导出 ${folder}`, exact: true }).getAttribute('aria-checked') === 'true',
    `${folder} 再展开保留父级勾选`)
}
await page.locator('[data-scope-row="saves"]').getByRole('switch', { name: '导出 WorldA', exact: true }).click()
await page.waitForFunction(() => {
  const dev = window.__pe.useApp.getState().state?.dev
  return dev?.strategies.find((r) => r.name === 'saves')?.subs?.find((s) => s.name === 'WorldA')?.checked === false
})

// 故障回归：最后一次输入后立刻保存，不能依赖 400ms 自动保存定时器。
await page.getByRole('button', { name: '编写更新日志', exact: true }).click()
const editorDialog = page.getByRole('dialog', { name: '更新日志', exact: true })
await editorDialog.locator('[contenteditable="true"]').fill('即时保存回归：导出说明不会丢失')
await editorDialog.getByRole('button', { name: '保存并关闭', exact: true }).click()
await editorDialog.waitFor({ state: 'hidden' })
check((await page.getByRole('button', { name: '编写更新日志', exact: true }).innerText()).includes('即时保存回归'),
  '立即保存后摘要同步')
await page.getByRole('button', { name: '编写更新日志', exact: true }).click()
check((await editorDialog.locator('[contenteditable="true"]').innerText()).includes('即时保存回归'),
  '重新打开保留日志')
await editorDialog.getByRole('button', { name: 'Markdown', exact: true }).click()
const failSave = async (route) => {
  const request = route.request().postDataJSON()
  if (request?.method === 'dev.changelog.set') {
    await route.fulfill({ json: { ok: false, error: { code: 'TEST_SAVE_FAILURE', message: '测试保存失败' } } })
  } else await route.fallback()
}
await page.route('**/api/invoke', failSave)
await editorDialog.locator('textarea').fill('# 保存失败也保留草稿\n')
await editorDialog.getByRole('button', { name: '保存并关闭', exact: true }).click()
await editorDialog.getByRole('status').filter({ hasText: '测试保存失败' }).waitFor()
check(await editorDialog.isVisible() && (await editorDialog.locator('textarea').inputValue()).includes('保留草稿'),
  '保存失败明确提示并保留编辑器与草稿')
await page.unroute('**/api/invoke', failSave)
await editorDialog.locator('textarea').fill('# 即时保存回归\n\n- 导出说明不会丢失\n')
await page.keyboard.press('Escape')
await editorDialog.waitFor({ state: 'hidden' })
check((await page.evaluate(async () => (await window.pywebview.api.invoke('dev.state', {}))?.data?.changelog)).includes('即时保存回归'),
  'Markdown 输入后 Esc 关闭也保存')

// 3) 填 CurseForge Key（可选）
if (CFKEY) {
  const keyInput = page.locator('input').filter({ hasNot: page.locator('[type="checkbox"]') })
  const input = page.locator('input[placeholder*="Key"], input[placeholder*="key"]').first()
  const target = (await input.count()) ? input : keyInput.last()
  await target.fill(CFKEY)
  await page.getByRole('button', { name: '保存', exact: true }).first().click()
  await page.waitForTimeout(300)
}

// 4) 两步导出：先确认范围（默认全选），再开始制作
await page.getByRole('button', { name: /下一步：设置策略/ }).click()
for (let i = 0; i < 150; i++) {
  await page.waitForTimeout(200)
  const st = await page.evaluate(async () => {
    const r = await window.pywebview.api.invoke('dev.state', {})
    return r?.data ?? null
  })
  if (st?.phase === 'idle') break
}
check(await page.getByText('第二步 · 更新策略与确认').first().isVisible(), '第二步策略可见')
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
import json, sys
from app.core.container import open_container
from app.core.eapack import read_changelog
p = ${JSON.stringify(produced)}
with open_container(p) as reader:
    names = list(reader.names)
    idx = json.loads(reader.read('modrinth.index.json'))
    man = json.loads(reader.read('ea_manifest.json'))
print(json.dumps({
  'files': len(idx['files']),
  'urls': [f['downloads'][0] for f in idx['files']],
  'overrides': [n for n in names if n.startswith('overrides/')],
  'source': man.get('source'),
  'deps': idx['dependencies'],
  'hasManifest': 'ea_manifest.json' in names,
  'changelog': read_changelog(p),
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
  check(info.overrides.some((n) => n.endsWith('options.txt')), 'options.txt 随包（PCL 默认）')
  check(info.deps.minecraft === '1.20.1', `依赖版本：${JSON.stringify(info.deps)}`)
  check(info.changelog.includes('即时保存回归') && info.changelog.includes('导出说明不会丢失'),
    '即时保存的更新日志原样进入导出包')
  check(!info.overrides.some((n) => n.startsWith('overrides/saves/WorldA/')) &&
    info.overrides.includes('overrides/saves/WorldB/level.dat') &&
    info.overrides.includes('overrides/saves/WorldB/region/r.0.0.mca'),
    '默认存档已导出，取消的 WorldA 未进入包')
  check(info.overrides.includes('overrides/resourcepacks/fixture.zip') &&
    info.overrides.includes('overrides/shaderpacks/fixture.zip'),
    '默认资源包与光影子项进入导出包')
}

check(errors.length === 0, '无控制台错误', errors.slice(0, 2).join(' | '))

await browser.close()
stop()
console.log(failed ? `\n${failed} 项失败` : '\n全部通过')
process.exit(failed ? 1 : 0)
