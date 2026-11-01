// 真前端 + 真 Api + 真流程的端到端（浏览器里点，Python 干活）。
//
// 两个套件（同一个文件，避免两份逻辑漂移）：
//   basic    渲染 → 定位 → 导入 → 策略 → 折叠区 → 更新日志 → 首选项
//            → 白名单 → 数据库 → 确认更新 → 磁盘核对 → 开发者（导入/选项/导出）
//   download 真 HTTP 下载（含文件内分片）→ 进度条/槽位 → 关闭守卫
//            → 中止 → 续传 → 磁盘逐字节核对
//
// 用法：node scripts/dev/e2e_ui.mjs <bridgeUrl> <workdir> [basic|download]
// 依赖从 web/node_modules 解析（本脚本不在 web/ 下，直接 import 'playwright' 找不到）。
import { createRequire } from 'node:module'
import { existsSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { join } from 'node:path'

const require = createRequire(new URL('../../web/package.json', import.meta.url))
const { chromium } = require('playwright')

const url = process.argv[2] || 'http://127.0.0.1:8123/index.html'
const work = process.argv[3] || '.'
const suite = process.argv[4] || 'basic'
const fixture = JSON.parse(readFileSync(join(work, 'fixture.json'), 'utf8'))
// CI 用 playwright 自己装的内核；本机可以指定 CHROMIUM 覆盖
const exe = process.env.CHROMIUM || ''

const artifacts = {}
let failed = 0
const check = (ok, label, extra = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !extra ? '' : '  →  ' + extra}`)
  if (!ok) failed++
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const browser = await chromium.launch({
  ...(exe ? { executablePath: exe } : {}),
  args: ['--no-sandbox'],
})
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
const errors = []
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', (e) => errors.push(String(e)))

// ----------------------------------------------------------------------
// 小工具
// ----------------------------------------------------------------------
const bodyText = () => page.evaluate(() => document.body.innerText)

async function waitBody(re, timeout = 20000) {
  const end = Date.now() + timeout
  while (Date.now() < end) {
    if (re.test(await bodyText())) return true
    await sleep(100)
  }
  return false
}

async function waitNoBody(re, timeout = 10000) {
  const end = Date.now() + timeout
  while (Date.now() < end) {
    if (!re.test(await bodyText())) return true
    await sleep(100)
  }
  return false
}

async function waitFor(fn, timeout = 20000, interval = 100) {
  const end = Date.now() + timeout
  while (Date.now() < end) {
    let v = null
    try { v = await fn() } catch { v = null }
    if (v) return v
    await sleep(interval)
  }
  return null
}

/** 点工作区的拖入区（STEP 1 = 整合包，STEP 2 = 更新包） */
async function clickStep(step) {
  const ok = await page.evaluate((s) => {
    const btn = Array.from(document.querySelectorAll('button'))
      .find((b) => (b.innerText || '').replace(/\s+/g, ' ').includes(s) &&
        b.getClientRects().length)
    if (!btn) return false
    btn.click()
    return true
  }, step)
  if (!ok) throw new Error(`点不到 ${step} 拖入区`)
  await sleep(150)
}

async function clickText(label, { exact = false, timeout = 15000 } = {}) {
  const loc = exact
    ? page.getByRole('button', { name: label, exact: true })
    : page.getByRole('button', { name: label })
  await loc.first().waitFor({ state: 'visible', timeout })
  await loc.first().click({ timeout })
  await sleep(60)
}

const invoke = (method, params = {}) =>
  page.evaluate(([m, p]) => window.pywebview.api.invoke(m, p), [method, params])

async function call(method, params, label) {
  const res = await invoke(method, params)
  check(res?.ok === true, label, JSON.stringify(res?.error ?? ''))
  return res?.data
}

const setPicks = (paths) =>
  page.evaluate((p) => fetch('/api/pick_queue', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paths: p }),
  }).then((r) => r.json()), paths)

const closeGuard = () =>
  page.evaluate(() => fetch('/api/close_guard', { method: 'POST' })
    .then((r) => r.json()))

/** 当前所有可见按钮的可读文案（行名按钮就是精确的行名） */
const buttonLabels = () => page.evaluate(() => Array.from(document.querySelectorAll('button'))
  .filter((b) => b.offsetParent !== null || b.getClientRects().length > 0)
  .map((b) => (b.innerText || '').replace(/\s+/g, ' ').trim()))

const countRows = async (name) => (await buttonLabels()).filter((t) => t === name).length

/** 等某一行真的画出来（快照到了不等于 DOM 已经渲染） */
async function waitRow(name, timeout = 20000) {
  if (!await waitFor(async () => (await countRows(name)) > 0, timeout)) {
    throw new Error(`等不到行：${name}`)
  }
}

/** 点某一行（策略表/开发者表）的名称按钮 */
async function clickRowName(name) {
  const ok = await page.evaluate((n) => {
    const btn = Array.from(document.querySelectorAll('button'))
      .find((b) => (b.innerText || '').replace(/\s+/g, '').trim() === n)
    if (!btn) return false
    btn.click()
    return true
  }, name)
  if (!ok) throw new Error(`点不到行：${name}`)
  await sleep(150)
}

/** 点某一行里的策略胶囊（文案完全匹配，避免点到别行/别处） */
async function clickRowChip(name, chip) {
  const ok = await page.evaluate(([n, c]) => {
    const rows = Array.from(document.querySelectorAll('button'))
      .filter((b) => (b.innerText || '').replace(/\s+/g, '').trim() === n)
    for (const nameBtn of rows) {
      const row = nameBtn.parentElement
      if (!row) continue
      const target = Array.from(row.querySelectorAll('button'))
        .find((b) => (b.innerText || '').trim() === c)
      if (target) { target.click(); return true }
    }
    return false
  }, [name, chip])
  if (!ok) throw new Error(`点不到胶囊：${name} / ${chip}`)
  await sleep(150)
}

const flowState = async () => (await invoke('flow.state'))?.data
const devState = async () => {
  const res = await invoke('app.state')
  return res?.data?.dev
}

const sha256 = (path) => {
  const h = createHash('sha256')
  h.update(readFileSync(path))
  return h.digest('hex')
}

// ----------------------------------------------------------------------
// 基本流程套件
// ----------------------------------------------------------------------
async function basicSuite() {
  // ---- 0. 渲染 ----
  await page.waitForSelector('aside', { timeout: 30000 })
  check(true, '界面渲染')

  // ---- 1. 定位整合包（侧栏"选择"→ 桥的 pick_path）----
  await setPicks([fixture.pack])
  await clickText('定位整合包')
  check(await waitBody(new RegExp(fixture.pack.split(/[\\/]/).pop())),
        '定位整合包', fixture.pack)
  const packAfter = (await invoke('app.state'))?.data?.pack
  // Windows 上前后端拿到的路径分隔符可能一个是 /、一个是 \，比之前先归一
  const norm = (v) => String(v ?? '').replace(/\\/g, '/').toLowerCase()
  check(norm(packAfter?.path) === norm(fixture.pack),
        '整合包信息显示', JSON.stringify(packAfter))

  // ---- 2. 导入更新包（工作区拖入区 → pick_path）----
  await setPicks([fixture.update])
  await clickStep('STEP 2')
  check(await waitBody(/更新包：E2EPack-3\.0\.0\.eapack/), '导入更新包')
  const imported = await flowState()
  check(imported?.update?.name === 'E2EPack-3.0.0.eapack' &&
        imported?.update?.changelog === true, '更新包信息（含更新日志）',
        JSON.stringify(imported?.update))

  // ---- 3. 开始更新（比对）----
  await clickText('开始更新', { exact: true })
  check(await waitBody(/变更内容/, 120000), '比对完成')
  let snap = await flowState()
  check(snap?.counts?.added === 1 && snap?.counts?.modified === 2 &&
        snap?.counts?.deleted === 1, '比对计数（+1 ~2 -1）',
        JSON.stringify(snap?.counts))
  check(snap?.changeCounts?.mods === 3, 'mods 待更新 3 项',
        JSON.stringify(snap?.changeCounts))
  // 比对阶段不许动磁盘
  check(readFileSync(join(fixture.pack, 'mods/old.jar'), 'utf8') === 'OLD',
        '比对阶段磁盘未变')

  // ---- A. 策略交互：完全匹配 → 跳过重名 → 重新扫描 → 改回 ----
  await waitRow('mods')
  await clickRowChip('mods', '跳过重名')
  snap = await flowState()
  const modsRow = () => (snap?.strategies ?? []).find((r) => r.name === 'mods')
  check(modsRow()?.current === '跳过重名', '策略已切到「跳过重名」',
        JSON.stringify(modsRow()))
  check(await waitBody(new RegExp('跳过\\s*2')), '跳过计数出现在变更列表')
  const modsGroup = (s) => (s?.groups ?? []).find((g) => g.top === 'mods')
  check(modsGroup(snap)?.skipped === 2, 'mods 跳过 2 项（快照）',
        JSON.stringify(modsGroup(snap)?.skipped))
  check((modsGroup(snap)?.items ?? []).filter((i) => i.skipped).length === 2,
        '被跳过的条目逐条标记（old.jar / gone.jar）')

  // 重新扫描：策略要保留，跳过数不能丢
  const before = (await bodyText()).match(/比对完成/g)?.length ?? 0
  await call('pack.scan', {}, '重新扫描（改策略后）')
  const rescanDone = await waitFor(async () => {
    const body = await bodyText()
    return (body.match(/比对完成/g)?.length ?? 0) > before
  }, 60000)
  check(!!rescanDone, '重新扫描完成（提示刷新）')
  snap = await flowState()
  check(modsRow()?.current === '跳过重名', '重新扫描后策略保留',
        JSON.stringify(modsRow()))
  check(modsGroup(snap)?.skipped === 2, '重新扫描后跳过数保留',
        JSON.stringify(modsGroup(snap)?.skipped))
  check(snap?.counts?.added === 1 && snap?.counts?.deleted === 1,
        '重新扫描后差异计数不变', JSON.stringify(snap?.counts))

  // 改回「完全匹配」：跳过应归零（deleted/ modified 重新参与更新）
  await clickRowChip('mods', '完全匹配')
  snap = await flowState()
  check(modsRow()?.current === '完全匹配', '策略已改回「完全匹配」',
        JSON.stringify(modsRow()))
  check(modsGroup(snap)?.skipped === 0, '跳过计数归零',
        JSON.stringify(modsGroup(snap)?.skipped))
  check(await waitNoBody(new RegExp('跳过\\s*2')), '「跳过 2」标记消失')

  // ---- B. 折叠区：无需更新 / 已禁用 ----
  check(await waitBody(/无需更新（1）/), '出现「无需更新」分组')
  check(await countRows('resourcepacks') === 0, '折叠时看不到内容')
  await clickText('无需更新')
  check(await waitFor(async () => (await countRows('resourcepacks')) === 1),
        '展开「无需更新」后内容可见')
  await clickText('无需更新')
  check(await waitFor(async () => (await countRows('resourcepacks')) === 0),
        '收起后内容再次隐藏')

  // 取消勾选 → 行落进「已禁用」
  await clickText('无需更新')
  await waitFor(async () => (await countRows('resourcepacks')) === 1)
  await clickRowName('resourcepacks')
  check(await waitBody(/已禁用（1）/), '取消勾选后出现「已禁用」分组')
  check(await countRows('resourcepacks') === 0, '「已禁用」默认折叠')
  await clickText('已禁用')
  check(await waitFor(async () => (await countRows('resourcepacks')) === 1),
        '展开「已禁用」后内容可见')
  await clickRowName('resourcepacks')
  check(await waitNoBody(/已禁用（1）/), '重新勾选后「已禁用」分组消失')
  check(await waitFor(async () => (await countRows('resourcepacks')) === 1),
        '行回到「无需更新」且仍可见')
  await clickText('无需更新')
  await waitFor(async () => (await countRows('resourcepacks')) === 0)

  // ---- C. 更新日志抽屉 ----
  await clickText('查看更新日志', { exact: true })
  const sheet = page.locator('[role="dialog"]').last()
  await sheet.waitFor({ state: 'visible', timeout: 15000 })
  const sheetText = await sheet.innerText()
  check(/3\.0\.0/.test(sheetText), '更新日志含版本号 3.0.0', sheetText.slice(0, 80))
  check(/e2e/.test(sheetText), '更新日志正文已渲染')
  await page.keyboard.press('Escape')
  check(await waitFor(async () => !(await sheet.isVisible().catch(() => false))),
        'Esc 关闭更新日志', '')

  // ---- D. 首选项（含「界面」一栏）----
  await clickText('更多')
  await clickText('首选项', { exact: true })
  const pref = page.locator('[role="dialog"]').last()
  await pref.waitFor({ state: 'visible', timeout: 15000 })
  const prefText = await pref.innerText()
  check(/界面/.test(prefText), '首选项有「界面」一栏', prefText.replace(/\n/g, ' | ').slice(0, 120))
  for (const label of ['自动', '旧界面', '新界面']) {
    check(prefText.includes(label), `「界面」有「${label}」选项`)
  }
  // 真点一下：要落到配置里（不是只改了个本地 state）
  await clickText('新界面', { exact: true })
  let settings = await call('app.settings.get', {}, '读取设置')
  check(settings?.ui_mode === 'web', '选「新界面」已保存', JSON.stringify(settings?.ui_mode))
  await clickText('自动', { exact: true })
  settings = await call('app.settings.get', {}, '读取设置')
  check(settings?.ui_mode === 'auto', '改回「自动」已保存', JSON.stringify(settings?.ui_mode))
  await page.keyboard.press('Escape')
  check(await waitFor(async () => !(await pref.isVisible().catch(() => false))),
        'Esc 关闭首选项')

  // ---- E. 白名单：写入 / 回读 / 清洗 / 还原 ----
  const wlBefore = (await call('app.whitelist.get', {}, '读取白名单'))?.whitelist
  const wlSet = await call('app.whitelist.set', { items: ['mods', 'config'] },
                           '写白名单 ["mods","config"]')
  check(JSON.stringify(wlSet?.whitelist) === JSON.stringify(['mods', 'config']),
        '白名单写入结果正确', JSON.stringify(wlSet))
  const wlRead = (await call('app.whitelist.get', {}, '回读白名单'))?.whitelist
  check(JSON.stringify(wlRead) === JSON.stringify(['mods', 'config']),
        '白名单回读一致', JSON.stringify(wlRead))
  const wlDirty = await call('app.whitelist.set',
                             { items: ['../x', '', 'mods'] }, '写非法白名单')
  check(JSON.stringify(wlDirty?.whitelist) === JSON.stringify(['mods']),
        '非法项被清洗（../x、空串）', JSON.stringify(wlDirty))
  const wlAfterDirty = (await call('app.whitelist.get', {}, '回读白名单'))?.whitelist
  check(JSON.stringify(wlAfterDirty) === JSON.stringify(['mods']),
        '清洗后的值真的落盘', JSON.stringify(wlAfterDirty))
  await call('app.whitelist.set', { items: wlBefore ?? [] }, '还原白名单')
  check(JSON.stringify((await call('app.whitelist.get', {}, '回读白名单'))?.whitelist)
        === JSON.stringify(wlBefore), '白名单已还原', JSON.stringify(wlBefore))

  // ---- F. 数据库位置：读 / 改到临时目录 / 还原 ----
  const dbBefore = (await call('app.db.get', {}, '读取数据库位置'))?.path
  check(!!dbBefore && existsSync(dbBefore), '数据库路径存在', dbBefore)
  const dbAlt = join(work, 'db-alt')
  const dbSet = await call('app.db.set', { path: dbAlt }, '切到临时数据库')
  check(dbSet?.path === dbAlt, '切库返回新路径', JSON.stringify(dbSet))
  check((await call('app.db.get', {}, '回读数据库位置'))?.path === dbAlt,
        '数据库位置回读一致')
  // 库的形态是 config.json + projects/（不是单个 .db 文件）
  check(existsSync(join(dbAlt, 'config.json')) && existsSync(join(dbAlt, 'projects')),
        '临时数据库已建库', dbAlt)
  await call('app.db.set', { path: dbBefore }, '还原数据库位置')
  check((await call('app.db.get', {}, '回读数据库位置'))?.path === dbBefore,
        '数据库位置已还原', dbBefore)

  // ---- 应用 + 磁盘为准（界面说完成不算）----
  await clickText('确认更新', { exact: true })
  check(await waitBody(/更新完成|已应用/, 180000), '更新应用完成')
  await sleep(400)
  const report = (await flowState())?.report
  check(report?.failed === 0, '复核 0 失败', JSON.stringify(report))
  const read = (p) => readFileSync(join(fixture.pack, p), 'utf8').replace(/\r\n/g, '\n')
  check(read('mods/old.jar') === 'NEW', 'mods 已替换')
  check(read('mods/added.jar') === 'ADDED', '新增文件已落地')
  check(!existsSync(join(fixture.pack, 'mods/gone.jar')), '本地多余文件已删除')
  check(read('config/mine.toml') === 'mine = 1\n', '玩家自己的配置没被动')
  check(read('config/sodium-options.json') === '{"render_distance": 8}\n',
        '智能剔除保护生效')
  check(read('resourcepacks/keep.zip') === 'PK-KEEP', '无需更新的文件没被重写')
  await page.screenshot({ path: join(work, 'e2e.png') })

  // ---- G. 开发者端：导入 → 选项 → 导出 ----
  await clickText('开发者', { exact: true })
  check(await waitBody(/开始制作更新包/), '切到开发者视图')
  await setPicks([fixture.zip])
  await clickText('拖入整合包')
  check(await waitBody(/更新范围/), '开发者导入整合包')
  let dev = await devState()
  check((dev?.strategies ?? []).length >= 2, '开发者列出更新范围',
        JSON.stringify((dev?.strategies ?? []).map((r) => r.name)))
  // 排除 config（点目录名）—— 快照到了不代表行已经画出来
  await waitRow('config')
  await clickRowName('config')
  dev = await devState()
  check((dev?.strategies ?? []).find((r) => r.name === 'config')?.excluded === true,
        '目录可被排除（config）')
  // 改策略
  await waitRow('mods')
  await clickRowChip('mods', '替换重名')
  dev = await devState()
  check((dev?.strategies ?? []).find((r) => r.name === 'mods')?.current === '替换重名',
        '开发者策略可改（mods → 替换重名）')
  // 导出选项
  await clickText('压缩到最小体积')
  dev = await devState()
  check(dev?.options?.min_size === true, '导出选项可切换（min_size）')
  // 更新日志
  const changelog = '# 导出夹具\n\n- 开发者端\n'
  await page.locator('textarea').first().fill(changelog)
  await sleep(1200)                       // 视图里有 500ms 防抖
  dev = await devState()
  check(dev?.changelog === changelog, '更新日志写回后端', JSON.stringify(dev?.changelog))
  // 输出位置（走 pick_path 的 save 分支）+ 开始制作
  const outFile = join(work, 'dev-out', 'E2EExport.eapack')
  await setPicks([outFile])
  await clickText('位置…', { exact: true })
  check(await waitFor(async () => {
    const v = await page.locator('input[placeholder="输出文件名"]').inputValue()
    return v === 'E2EExport.eapack'
  }), '输出文件名已回填', outFile)
  await clickText('开始制作更新包', { exact: true })
  check(await waitBody(/已生成：/, 120000), '开发者导出完成（完成态可见）')
  check(existsSync(outFile) && statSync(outFile).size > 500,
        '导出的 .eapack 存在', outFile)
  artifacts.exported = outFile
  artifacts.exportChangelog = changelog
  dev = await devState()
  check(dev?.phase === 'done' && dev?.outPath === outFile,
        '开发者完成态（快照）', JSON.stringify({ phase: dev?.phase, out: dev?.outPath }))

}

// ----------------------------------------------------------------------
// 真下载套件
// ----------------------------------------------------------------------
const barPct = () => page.evaluate(() => {
  const btn = document.querySelector('button[title="点击查看并行槽位"]')
  if (!btn) return null
  const widths = Array.from(btn.querySelectorAll('span'))
    .map((s) => parseFloat(s.style.width || ''))
    .filter((n) => Number.isFinite(n))
  return widths.length ? Math.max(...widths) : null
})

const slotBusy = () => page.evaluate(() => Array.from(
  document.querySelectorAll('[data-idle]'))
  .filter((r) => r.getAttribute('data-idle') === 'false')
  .map((r) => (r.innerText || '').replace(/\s+/g, ' ').trim()))

async function openSlots() {
  const bar = page.locator('button[title="点击查看并行槽位"]')
  if (await bar.count()) {
    await bar.first().click({ timeout: 5000 }).catch(() => {})
    await sleep(120)
  }
}

async function dismissDialogIfAny(timeout = 3000) {
  const ok = page.getByRole('button', { name: '好', exact: true })
  await ok.first().waitFor({ state: 'visible', timeout }).catch(() => {})
  if (await ok.count()) {
    await ok.first().click({ timeout: 5000 }).catch(() => {})
    await sleep(120)
  }
}

async function downloadSuite() {
  await page.waitForSelector('aside', { timeout: 30000 })
  check(true, '界面渲染')

  await setPicks([fixture.pack])
  await clickText('定位整合包')
  check(await waitBody(new RegExp(fixture.pack.split(/[\\/]/).pop())), '定位整合包')

  await setPicks([fixture.update])
  await clickStep('STEP 2')
  check(await waitBody(/E2EPackDL-a\.eapack/), '导入更新包（带 URL 文件）')

  await clickText('开始更新', { exact: true })
  check(await waitBody(/变更内容/, 120000), '比对完成')
  await waitBody(/e2e-big\.jar/, 30000)      // 等新条目真的画出来
  const snap = await flowState()
  check(snap?.counts?.added === 4, '比对出 4 个新增文件',
        JSON.stringify(snap?.counts))
  check((snap?.groups ?? []).some((g) => g.items.some((i) => i.path.endsWith('e2e-big.jar'))),
        '要下载的大文件出现在变更列表')

  // ---- H：确认更新 → 边下边采样 ----
  await clickText('确认更新', { exact: true })
  const seen = { barMid: 0, slots: new Set(), hints: new Set(), guard: null, done: false }
  const t0 = Date.now()
  let opened = false
  let guardChecked = false
  while (Date.now() - t0 < 180000) {
    const pct = await barPct()
    if (pct !== null && pct > 0 && pct < 100) seen.barMid++
    if (!opened && pct !== null) {
      opened = true
      await openSlots()
    }
    if (opened) {
      const busy = await slotBusy()
      busy.forEach((s) => seen.slots.add(s))
      // 确认真的在下载之后，再问"能不能关窗"：下载点关闭必须被拦住
      if (!guardChecked && (seen.barMid > 0 || busy.length > 0)) {
        guardChecked = true
        const guard = await closeGuard()
        seen.guard = guard?.data
        if (seen.guard?.blocked) await dismissDialogIfAny()
      }
    }
    const body = await bodyText()
    const hit = body.match(/下载 \d+\/\d+|正在下载/)
    if (hit) seen.hints.add(hit[0])
    if (/更新完成|已应用/.test(body)) { seen.done = true; break }
    await sleep(60)
  }
  check(seen.done, '下载 + 应用完成')
  check(seen.barMid > 0, '进度条出现过下载中的进度', `采样 ${seen.barMid} 次`)
  check(seen.slots.size > 0, '槽位面板出现过活动槽位',
        JSON.stringify([...seen.slots].slice(0, 3)))
  check(seen.hints.size > 0, '提示文案有下载进度', JSON.stringify([...seen.hints]))
  check(seen.guard?.blocked === true, '下载中关闭守卫拦截（can_close=False）',
        JSON.stringify(seen.guard))

  const report = (await flowState())?.report
  check(report?.failed === 0, '复核 0 失败', JSON.stringify(report))
  for (const f of fixture.files) {
    const target = join(fixture.pack, f.path)
    check(existsSync(target) && sha256(target) === f.sha256,
          `下载并落地：${f.name}（${f.size} B）`, target)
  }
  check(readFileSync(join(fixture.pack, 'mods/e2e-inpack.jar'), 'utf8') === 'INPACK',
        '包内文件照常落地')
  check(existsSync(join(fixture.pack, 'config/e2e-dl.toml')), '配置随包落地')
  const guardAfter = await closeGuard()
  check(guardAfter?.data?.blocked === false, '空闲时关闭守卫放行',
        JSON.stringify(guardAfter?.data))
  await page.screenshot({ path: join(work, 'download.png') })

  // ---- H2：下载中中止 → 状态不卡死 → 续传完成 ----
  await call('update.clear', {}, '清空上一轮比对')
  await setPicks([fixture.abortPack])
  await clickText('定位整合包')
  check(await waitBody(new RegExp(fixture.abortPack.split(/[\\/]/).pop())),
        '定位第二个整合包')
  await setPicks([fixture.abortUpdate])
  await clickStep('STEP 2')
  check(await waitBody(/E2EPackDL-b\.eapack/), '导入续传用更新包')
  await clickText('开始更新', { exact: true })
  check(await waitBody(/变更内容/, 120000), '比对完成（续传包）')
  // 上一轮的变更树可能还没被替换掉：等这一轮独有的条目出现
  check(await waitBody(/e2e-abort-big\.jar/, 30000), '变更列表已换成续传包内容')
  await clickText('确认更新', { exact: true })
  // 下载态的唯一可靠信号：主按钮变成「取消下载」（界面必须能取消）
  const started = await waitFor(async () =>
    (await buttonLabels()).includes('取消下载'), 20000)
  check(!!started, '进入下载态（主按钮变成「取消下载」）')
  await clickText('取消下载', { exact: true })
  check(await waitBody(/已请求中止/, 10000), '中止请求已送到后端')
  const stopped = await waitFor(async () => {
    const st = (await invoke('flow.state'))?.data?.phase
    return st === 'idle' || st === 'done' ? st : null
  }, 40000)
  check(stopped === 'idle', '中止后状态回到空闲（不卡死）', JSON.stringify(stopped))
  const afterAbort = await flowState()
  check((afterAbort?.pending ?? []).length > 0, '中止后进入"待补入"',
        JSON.stringify(afterAbort))
  check(await waitBody(/还差 \d+ 个文件/), '界面提示还差文件')
  const guardAborted = await closeGuard()
  check(guardAborted?.data?.blocked === false, '中止后可以正常关窗（守卫放行）',
        JSON.stringify(guardAborted?.data))

  // 续传：重新比对 → 自动继续下载 → 应用
  const doneBefore = ((await bodyText()).match(/更新完成/g) ?? []).length
  await call('update.resume', {}, '继续上次的更新')
  check(await waitFor(async () =>
    ((await bodyText()).match(/更新完成/g) ?? []).length > doneBefore, 180000),
        '续传后更新完成')
  const abortFile = join(fixture.abortPack, fixture.abortFile.path)
  check(existsSync(abortFile) && sha256(abortFile) === fixture.abortFile.sha256,
        '续传后文件字节与源一致', abortFile)
  const report2 = (await flowState())?.report
  check(report2?.failed === 0, '续传后复核 0 失败', JSON.stringify(report2))
  artifacts.downloaded = fixture.files.map((f) => join(fixture.pack, f.path))
  artifacts.aborted = abortFile
}

try {
  await page.goto(url, { waitUntil: 'load' })
  if (suite === 'download') await downloadSuite()
  else await basicSuite()
} catch (err) {
  check(false, `套件（${suite}）异常中断`, String(err).slice(0, 400))
  try { await page.screenshot({ path: join(work, `fail-${suite}.png`) }) } catch { /* 忽略 */ }
}

writeFileSync(join(work, 'artifacts.json'),
              JSON.stringify(artifacts, null, 2), 'utf-8')
check(errors.filter((e) => !/ERR_ABORTED|net::ERR/.test(e)).length === 0,
      '无控制台错误', errors.join(' | ').slice(0, 400))
await browser.close()
console.log(failed ? `\n${failed} 项失败` : '\n全部通过')
process.exit(failed ? 1 : 0)
