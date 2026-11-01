/**
 * e2e/harness.ts — 页面内端到端驱动（只有 ?e2e=1 才会被动态 import）
 * ------------------------------------------------------------------
 * 为什么驱动搬到页面里：老做法是 Python 后台线程反复 `window.evaluate_js` 点按钮。
 * pywebview 的 evaluate_js 是"跨线程同步 Invoke + 无超时的 semaphore.acquire"，
 * 而 js_api 的返回值又是用 evaluate_js 送回页面的（webview/util.py:264）；驱动线程
 * 和页面自己的桥调用互相等，一旦卡住连看门狗线程都跑不了（CI 里 900 秒零输出）。
 * 现在：页面自己跑完整个场景，后端只通过 `api.invoke` 干活，结果用
 * `api.e2e_report` 回传一次。Python 侧不再从别的线程碰 WebView。
 *
 * 覆盖范围与 `scripts/dev/e2e_ui.mjs` 一一对应（同一批断言，别只改一边）：
 *   渲染 / 定位整合包 / 导入更新包 / 比对与计数 / 策略胶囊（跳过重名↔完全匹配）
 *   重新扫描 / 折叠区（无需更新、已禁用）/ 更新日志抽屉 / 首选项（含「界面」）
 *   白名单读写与清洗 / 数据库位置 / 应用 + 磁盘核对 / 开发者导入-选项-导出
 *   真下载（含 5MiB 分片）/ 进度与槽位 / 关闭守卫 / 中止 / 续传
 *
 * 两个刻意的差异（和原来一样，不是缩水）：
 *   · 文件选择：真机会弹原生对话框，无人值守点不了；这里只把
 *     `api.pick_path` 换成排队返回，点"定位整合包 / STEP 2 / 位置…"依旧是真按钮。
 *   · 磁盘：页面没有文件系统，只读断言走 `e2e.fs` 探针（只读、只允许夹具目录）。
 *     导出的 .eapack 内容由 Python runner 复核（只有它读得懂）。
 */

import { whenReady } from '@/bridge'
import type { InvokeResult } from '@/bridge'
import type { DevSnapshot, FlowSnapshot } from '@/bridge/protocol'

/* ------------------------------------------------------------------ */
/* 类型                                                                */
/* ------------------------------------------------------------------ */

interface FsItem {
  path: string
  exists: boolean
  isDir?: boolean
  size?: number
  text?: string
  sha256?: string
}

interface Step {
  name: string
  ok: boolean
  detail: string
}

interface BasicFixture {
  pack: string
  update: string
  zip?: string
}

interface DownloadFixture {
  pack: string
  update: string
  abortPack: string
  abortUpdate: string
  files: { name: string; path: string; size: number; sha256: string }[]
  abortFile: { name: string; path: string; size?: number; sha256: string }
}

interface E2eConfig {
  work: string
  suite?: 'basic' | 'download' | 'all'
  budget_ms?: number
  basic?: BasicFixture
  download?: DownloadFixture
}

type BridgeApi = NonNullable<Window['pywebview']>['api']

/* ------------------------------------------------------------------ */
/* 报告状态                                                            */
/* ------------------------------------------------------------------ */

const steps: Step[] = []
const artifacts: Record<string, unknown> = {}
const consoleErrors: string[] = []

let currentAction = '（还没开始）'
let reportError = ''
let suiteName = ''
let startedAt = Date.now()
let finishing = false
let finished = false

/** 给外面的调试工具看的一点点状态（本地 Playwright 靠它快速判断"没救了"）。 */
interface E2eStatus {
  fatal: string
  steps: number
  done: boolean
}

function statusBox(): E2eStatus {
  const host = window as unknown as { __pe_e2e?: E2eStatus }
  if (!host.__pe_e2e) host.__pe_e2e = { fatal: '', steps: 0, done: false }
  return host.__pe_e2e
}

function mark(label: string): void {
  currentAction = label
}

function check(ok: boolean, name: string, detail = ''): boolean {
  steps.push({ name, ok: Boolean(ok), detail: ok ? '' : String(detail).slice(0, 800) })
  statusBox().steps = steps.length
  // 每步都往磁盘落一行：真卡死时 CI 里能看出卡在哪（不 await，坏掉也不影响流程）
  try {
    void (window as unknown as {
      pywebview?: { api?: { invoke?: (m: string, p: unknown) => Promise<unknown> } }
    }).pywebview?.api?.invoke?.('e2e.step', { name, ok: Boolean(ok) })
  } catch {
    /* 忽略：只是诊断信息 */
  }
  return Boolean(ok)
}

/** 断言 + 中断：后面的步骤依赖这个前提时用它（等价老驱动的 step）。 */
function need(ok: boolean, name: string, detail = ''): void {
  if (!check(ok, name, detail)) {
    throw new Error(`${name}${detail ? `：${detail}` : ''}`)
  }
}

/* ------------------------------------------------------------------ */
/* 基础工具                                                            */
/* ------------------------------------------------------------------ */

const sleep = (ms: number) => new Promise<void>((r) => window.setTimeout(r, ms))
const squeeze = (s: string) => s.replace(/\s+/g, ' ').trim()
const squash = (s: string) => s.replace(/\s+/g, '').trim()
/** Windows 上 write_text 会把 \n 落成 \r\n，比较前统一 */
const normText = (t?: string) => (t ?? '').replace(/\r\n/g, '\n')
const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const baseName = (p: string) => p.split(/[\\/]/).pop() ?? p
const pathKey = (p: string) => p.replace(/\\/g, '/').toLowerCase()

/** 页面里没有 node:path：按 base 的分隔符拼，Windows 上必须还是反斜杠 */
function joinPath(base: string, rel: string): string {
  const sep = base.includes('\\') && !base.includes('/') ? '\\' : '/'
  const head = base.replace(/[\\/]+$/, '')
  const tail = rel.replace(/^[\\/]+/, '').replace(/[\\/]/g, sep)
  return `${head}${sep}${tail}`
}

const bodyText = () => document.body.innerText
const allButtons = () => Array.from(document.querySelectorAll<HTMLButtonElement>('button'))
const isShown = (el: Element) => el.getClientRects().length > 0

async function waitFor<T>(
  fn: () => T | Promise<T | null | undefined | false>,
  timeout: number,
  interval = 100,
): Promise<T | null> {
  const end = Date.now() + timeout
  while (Date.now() < end) {
    let value: T | null | undefined | false = null
    try {
      value = await fn()
    } catch {
      value = null
    }
    if (value) return value
    await sleep(interval)
  }
  return null
}

async function waitText(re: RegExp, timeout = 20000): Promise<boolean> {
  return (await waitFor(() => re.test(bodyText()), timeout)) !== null
}

async function waitNoText(re: RegExp, timeout = 10000): Promise<boolean> {
  const end = Date.now() + timeout
  while (Date.now() < end) {
    if (!re.test(bodyText())) return true
    await sleep(100)
  }
  return false
}

/* ---------------- 点击 ---------------- */

function findButton(label: string, exact = false): HTMLButtonElement | null {
  const want = squeeze(label)
  for (const btn of allButtons()) {
    if (!isShown(btn)) continue
    const text = squeeze(btn.innerText || '')
    if (exact ? text === want : text.includes(want)) return btn
  }
  return null
}

async function clickText(
  label: string,
  opts: { exact?: boolean; timeout?: number } = {},
): Promise<void> {
  mark(`点击「${label}」`)
  const btn = await waitFor(() => findButton(label, opts.exact ?? false),
    opts.timeout ?? 15000, 80)
  if (!btn) throw new Error(`点不到按钮：${label}`)
  btn.click()
  await sleep(90)
}

/** 工作区拖入区（STEP 1 = 整合包，STEP 2 = 更新包） */
const clickStep = (step: string) => clickText(step, { timeout: 15000 })

function buttonLabels(): string[] {
  return allButtons().filter(isShown).map((b) => squeeze(b.innerText || ''))
}

const countRows = (name: string) => buttonLabels().filter((t) => t === name).length

async function waitRow(name: string, timeout = 20000): Promise<void> {
  mark(`等行「${name}」`)
  const hit = await waitFor(() => countRows(name) > 0, timeout, 80)
  if (!hit) throw new Error(`等不到行：${name}`)
}

async function clickRowName(name: string): Promise<void> {
  mark(`点行「${name}」`)
  const btn = allButtons().find((b) => squash(b.innerText || '') === name)
  if (!btn) throw new Error(`点不到行：${name}`)
  btn.click()
  await sleep(90)
}

async function clickRowChip(name: string, chip: string): Promise<void> {
  mark(`点胶囊「${name} / ${chip}」`)
  const rows = allButtons().filter((b) => squash(b.innerText || '') === name)
  for (const nameBtn of rows) {
    const row = nameBtn.parentElement
    if (!row) continue
    const target = Array.from(row.querySelectorAll<HTMLButtonElement>('button'))
      .find((b) => squeeze(b.innerText || '') === chip)
    if (target) {
      target.click()
      await sleep(90)
      return
    }
  }
  throw new Error(`点不到胶囊：${name} / ${chip}`)
}

/* ---------------- 抽屉 / 输入 ---------------- */

const dialogs = () => Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"]'))
const dialogCount = () => dialogs().length
const lastDialogText = () => dialogs().at(-1)?.innerText ?? ''

async function pressEsc(): Promise<void> {
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
  await sleep(250)
}

/** React 受控输入：必须走原型上的 setter，直接赋值它不认 */
function setInput(selector: string, value: string): void {
  const el = document.querySelector<HTMLTextAreaElement | HTMLInputElement>(selector)
  if (!el) throw new Error(`找不到输入框：${selector}`)
  const proto = el.tagName === 'TEXTAREA'
    ? window.HTMLTextAreaElement.prototype
    : window.HTMLInputElement.prototype
  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set
  if (!setter) throw new Error(`无法写输入框：${selector}`)
  setter.call(el, value)
  el.dispatchEvent(new Event('input', { bubbles: true }))
}

/* ---------------- 进度条 / 槽位 ---------------- */

function barPct(): number | null {
  const btn = document.querySelector<HTMLButtonElement>('button[title="点击查看并行槽位"]')
  if (!btn) return null
  const widths = Array.from(btn.querySelectorAll<HTMLElement>('span'))
    .map((s) => parseFloat(s.style.width || ''))
    .filter((n) => Number.isFinite(n))
  return widths.length ? Math.max(...widths) : null
}

function slotBusy(): string[] {
  return Array.from(document.querySelectorAll<HTMLElement>('[data-idle]'))
    .filter((row) => row.getAttribute('data-idle') === 'false')
    .map((row) => squeeze(row.innerText || ''))
}

function openSlots(): void {
  const bar = document.querySelector<HTMLButtonElement>('button[title="点击查看并行槽位"]')
  bar?.click()
}

async function dismissDialog(): Promise<void> {
  const btn = await waitFor(() => findButton('好', true), 3000, 100)
  if (btn) btn.click()
  await sleep(120)
}

/* ------------------------------------------------------------------ */
/* 桥                                                                  */
/* ------------------------------------------------------------------ */

const bridge = (): BridgeApi | undefined => window.pywebview?.api

function withTimeout<T>(p: Promise<T>, ms: number, message: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error(message)), ms)
    p.then(
      (v) => { window.clearTimeout(timer); resolve(v) },
      (e) => { window.clearTimeout(timer); reject(e) },
    )
  })
}

/** 桥调用一律带超时：桥那头就算卡住，这里也要能报出来 */
function bridgeInvoke(
  method: string,
  params: Record<string, unknown> = {},
  timeout = 120000,
): Promise<InvokeResult> {
  const api = bridge()
  if (!api) return Promise.reject(new Error('桌面桥接尚未就绪'))
  return withTimeout(api.invoke(method, params), timeout, `桥调用超时：${method}`)
}

async function call<T>(
  method: string,
  params: Record<string, unknown>,
  label: string,
): Promise<T | null> {
  mark(label)
  const res = await bridgeInvoke(method, params, 180000)
  if (res && res.ok === true) {
    check(true, label)
    return res.data as T
  }
  check(false, label, JSON.stringify((res as { error?: unknown } | null)?.error ?? ''))
  return null
}

async function flowSnap(): Promise<FlowSnapshot | null> {
  const res = await bridgeInvoke('flow.state', {}, 30000)
  return res && res.ok === true ? (res.data as FlowSnapshot) : null
}

interface AppSnapshot {
  pack?: { name?: string; path?: string }
  dev?: DevSnapshot
}

async function appSnap(): Promise<AppSnapshot | null> {
  const res = await bridgeInvoke('app.state', {}, 30000)
  return res && res.ok === true ? (res.data as AppSnapshot) : null
}

async function devSnap(): Promise<DevSnapshot | null> {
  return (await appSnap())?.dev ?? null
}

async function probe(
  paths: string[],
  opts: { text?: boolean; sha256?: boolean } = {},
): Promise<Record<string, FsItem>> {
  const res = await bridgeInvoke('e2e.fs', { paths, ...opts }, 60000)
  if (!res || res.ok !== true) {
    throw new Error(`e2e.fs 失败：${JSON.stringify((res as { error?: unknown } | null)?.error)}`)
  }
  return (res.data as { files: Record<string, FsItem> }).files
}

/* ---------------- 文件选择替身 ---------------- */

let picks: string[] = []

function setPicks(paths: string[]): void {
  picks = paths.slice()
}

function installPickStub(): void {
  const api = bridge()
  if (!api) return
  api.pick_path = async () => {
    const path = picks.length ? picks.shift() ?? '' : ''
    return { ok: true, data: { path } }
  }
}

/* ------------------------------------------------------------------ */
/* 报告                                                                */
/* ------------------------------------------------------------------ */

function trapErrors(): void {
  window.addEventListener('error', (e) => {
    consoleErrors.push(String(e.message || e.error || e))
  })
  window.addEventListener('unhandledrejection', (e) => {
    consoleErrors.push(`unhandledrejection: ${String((e as PromiseRejectionEvent).reason)}`)
  })
  const original = console.error.bind(console)
  console.error = (...args: unknown[]) => {
    consoleErrors.push(args.map(String).join(' '))
    original(...args)
  }
}

async function finish(): Promise<void> {
  if (finishing) return
  finishing = true
  try {
    const errors = consoleErrors.filter((e) => !/ERR_ABORTED|net::ERR/.test(e))
    check(errors.length === 0, '无控制台错误', errors.join(' | ').slice(0, 400))
    const payload = {
      ok: steps.length > 0 && !reportError && steps.every((s) => s.ok),
      steps,
      artifacts,
      error: reportError,
      suite: suiteName,
      ms: Date.now() - startedAt,
    }
    const api = bridge()
    if (api?.e2e_report) {
      try {
        const res = await withTimeout(api.e2e_report(payload), 15000,
          'e2e_report 超时')
        const accepted = res as { ok?: boolean; error?: { message?: string } } | null
        if (accepted && accepted.ok === false) {
          statusBox().fatal = `报告被拒绝：${accepted.error?.message ?? ''}`
          console.log(`[e2e] ${statusBox().fatal}`)
        }
      } catch (err) {
        // 报告可能已经落盘（Python 先写文件再关窗），别把控制台弄脏
        statusBox().fatal = `回传报告失败：${String(err)}`
        console.log(`[e2e] ${statusBox().fatal}`)
      }
    } else {
      statusBox().fatal = '没有 e2e_report 入口（未以 PULSES_E2E=1 启动？）'
      console.log(`[e2e] ${statusBox().fatal}`)
    }
  } finally {
    finished = true
    statusBox().done = true
  }
}

/** 整体保险丝：任何一步卡住都要有报告，绝不允许"等到外面杀进程" */
function armHardStop(ms: number): void {
  window.setTimeout(() => {
    if (finished) return
    reportError = `harness 整体超时（${Math.round(ms / 1000)}s），最后一步：${currentAction}`
    void finish()
  }, ms)
}

/* ------------------------------------------------------------------ */
/* 场景一：基本流程（对应 e2e_ui.mjs basicSuite）                        */
/* ------------------------------------------------------------------ */

async function basicSuite(fx: BasicFixture, work: string): Promise<void> {
  const pack = fx.pack
  const update = fx.update

  // ---- 0. 渲染 ----
  mark('等界面渲染')
  const shell = await waitFor(() => document.querySelector('aside'), 30000)
  need(!!shell, '界面渲染')

  // ---- 1. 定位整合包（侧栏"选择"→ 桥的 pick_path）----
  setPicks([pack])
  await clickText('定位整合包')
  need(await waitText(new RegExp(escapeRe(baseName(pack)))), '定位整合包', pack)
  const appState = await appSnap()
  const packAfter = appState?.pack
  // 后端会把路径落成真正的实例根：只要求"认到了这个包"（名字对、路径含目录名）
  check(packAfter?.name === 'E2EPack' && pathKey(packAfter?.path ?? '').includes('e2epack'),
    '整合包信息显示', `${JSON.stringify(packAfter)} expected=${pack}`)

  // ---- 2. 导入更新包 ----
  setPicks([update])
  await clickStep('STEP 2')
  need(await waitText(/更新包：E2EPack-3\.0\.0\.eapack/), '导入更新包')
  let snap = await flowSnap()
  check(snap?.update?.name === 'E2EPack-3.0.0.eapack' && snap?.update?.changelog === true,
    '更新包信息（含更新日志）', JSON.stringify(snap?.update))

  // ---- 3. 开始更新（比对）----
  await clickText('开始更新', { exact: true })
  need(await waitText(/变更内容/, 120000), '比对完成')
  snap = await flowSnap()
  check(snap?.counts?.added === 1 && snap?.counts?.modified === 2
    && snap?.counts?.deleted === 1, '比对计数（+1 ~2 -1）', JSON.stringify(snap?.counts))
  check(snap?.changeCounts?.mods === 3, 'mods 待更新 3 项',
    JSON.stringify(snap?.changeCounts))
  // 比对阶段不许动磁盘
  const oldJar = joinPath(pack, 'mods/old.jar')
  const beforeScan = await probe([oldJar], { text: true })
  check(normText(beforeScan[oldJar]?.text) === 'OLD', '比对阶段磁盘未变', oldJar)

  // ---- A. 策略交互：完全匹配 → 跳过重名 → 重新扫描 → 改回 ----
  await waitRow('mods')
  await clickRowChip('mods', '跳过重名')
  snap = await flowSnap()
  const modsRow = (s: FlowSnapshot | null) => (s?.strategies ?? []).find((r) => r.name === 'mods')
  const modsGroup = (s: FlowSnapshot | null) => (s?.groups ?? []).find((g) => g.top === 'mods')
  check(modsRow(snap)?.current === '跳过重名', '策略已切到「跳过重名」',
    JSON.stringify(modsRow(snap)))
  need(await waitText(/跳过\s*2/), '跳过计数出现在变更列表')
  check(modsGroup(snap)?.skipped === 2, 'mods 跳过 2 项（快照）',
    JSON.stringify(modsGroup(snap)?.skipped))
  check((modsGroup(snap)?.items ?? []).filter((i) => i.skipped).length === 2,
    '被跳过的条目逐条标记（old.jar / gone.jar）')

  // 重新扫描：策略要保留，跳过数不能丢
  const before = (bodyText().match(/比对完成/g) ?? []).length
  await call('pack.scan', {}, '重新扫描（改策略后）')
  need(!!(await waitFor(() => (bodyText().match(/比对完成/g) ?? []).length > before, 60000)),
    '重新扫描完成（提示刷新）')
  snap = await flowSnap()
  check(modsRow(snap)?.current === '跳过重名', '重新扫描后策略保留',
    JSON.stringify(modsRow(snap)))
  check(modsGroup(snap)?.skipped === 2, '重新扫描后跳过数保留',
    JSON.stringify(modsGroup(snap)?.skipped))
  check(snap?.counts?.added === 1 && snap?.counts?.deleted === 1, '重新扫描后差异计数不变',
    JSON.stringify(snap?.counts))

  // 改回「完全匹配」：跳过应归零
  await clickRowChip('mods', '完全匹配')
  snap = await flowSnap()
  check(modsRow(snap)?.current === '完全匹配', '策略已改回「完全匹配」',
    JSON.stringify(modsRow(snap)))
  check(modsGroup(snap)?.skipped === 0, '跳过计数归零',
    JSON.stringify(modsGroup(snap)?.skipped))
  need(await waitNoText(/跳过\s*2/), '「跳过 2」标记消失')

  // ---- B. 折叠区：无需更新 / 已禁用 ----
  need(await waitText(/无需更新（1）/), '出现「无需更新」分组')
  check(countRows('resourcepacks') === 0, '折叠时看不到内容')
  await clickText('无需更新')
  need(!!(await waitFor(() => countRows('resourcepacks') === 1, 10000)),
    '展开「无需更新」后内容可见')
  await clickText('无需更新')
  need(!!(await waitFor(() => countRows('resourcepacks') === 0, 10000)),
    '收起后内容再次隐藏')

  // 取消勾选 → 行落进「已禁用」
  await clickText('无需更新')
  await waitFor(() => countRows('resourcepacks') === 1, 10000)
  await clickRowName('resourcepacks')
  need(await waitText(/已禁用（1）/), '取消勾选后出现「已禁用」分组')
  check(countRows('resourcepacks') === 0, '「已禁用」默认折叠')
  await clickText('已禁用')
  need(!!(await waitFor(() => countRows('resourcepacks') === 1, 10000)),
    '展开「已禁用」后内容可见')
  await clickRowName('resourcepacks')
  need(await waitNoText(/已禁用（1）/), '重新勾选后「已禁用」分组消失')
  need(!!(await waitFor(() => countRows('resourcepacks') === 1, 10000)),
    '行回到「无需更新」且仍可见')
  await clickText('无需更新')
  await waitFor(() => countRows('resourcepacks') === 0, 10000)

  // ---- C. 更新日志抽屉 ----
  await clickText('查看更新日志', { exact: true })
  need(!!(await waitFor(() => dialogCount() > 0, 15000)), '更新日志抽屉已打开')
  const logText = await waitFor(() => {
    const text = lastDialogText()
    return /3\.0\.0/.test(text) ? text : null
  }, 15000)
  check(!!logText, '更新日志含版本号 3.0.0', (logText ?? lastDialogText()).slice(0, 80))
  check(/e2e/.test(logText ?? ''), '更新日志正文已渲染')
  await pressEsc()
  need(!!(await waitFor(() => dialogCount() === 0, 10000)), 'Esc 关闭更新日志')

  // ---- D. 首选项（含「界面」一栏）----
  await clickText('更多')
  await clickText('首选项', { exact: true })
  need(!!(await waitFor(() => dialogCount() > 0, 15000)), '首选项抽屉已打开')
  const prefText = lastDialogText()
  check(/界面/.test(prefText), '首选项有「界面」一栏',
    prefText.replace(/\n/g, ' | ').slice(0, 120))
  for (const label of ['自动', '旧界面', '新界面']) {
    check(prefText.includes(label), `「界面」有「${label}」选项`)
  }
  // 真点一下：要落到配置里（不是只改了个本地 state）
  await clickText('新界面', { exact: true })
  let settings = await call<{ ui_mode?: string }>('app.settings.get', {}, '读取设置')
  check(settings?.ui_mode === 'web', '选「新界面」已保存', JSON.stringify(settings?.ui_mode))
  await clickText('自动', { exact: true })
  settings = await call<{ ui_mode?: string }>('app.settings.get', {}, '读取设置')
  check(settings?.ui_mode === 'auto', '改回「自动」已保存', JSON.stringify(settings?.ui_mode))
  await pressEsc()
  need(!!(await waitFor(() => dialogCount() === 0, 10000)), 'Esc 关闭首选项')

  // ---- E. 白名单：写入 / 回读 / 清洗 / 还原 ----
  const wlBefore = (await call<{ whitelist?: string[] }>(
    'app.whitelist.get', {}, '读取白名单'))?.whitelist ?? []
  const wlSet = await call<{ whitelist?: string[] }>(
    'app.whitelist.set', { items: ['mods', 'config'] }, '写白名单 ["mods","config"]')
  check(JSON.stringify(wlSet?.whitelist) === JSON.stringify(['mods', 'config']),
    '白名单写入结果正确', JSON.stringify(wlSet))
  const wlRead = (await call<{ whitelist?: string[] }>(
    'app.whitelist.get', {}, '回读白名单'))?.whitelist
  check(JSON.stringify(wlRead) === JSON.stringify(['mods', 'config']), '白名单回读一致',
    JSON.stringify(wlRead))
  const wlDirty = await call<{ whitelist?: string[] }>(
    'app.whitelist.set', { items: ['../x', '', 'mods'] }, '写非法白名单')
  check(JSON.stringify(wlDirty?.whitelist) === JSON.stringify(['mods']),
    '非法项被清洗（../x、空串）', JSON.stringify(wlDirty))
  const wlAfterDirty = (await call<{ whitelist?: string[] }>(
    'app.whitelist.get', {}, '回读白名单'))?.whitelist
  check(JSON.stringify(wlAfterDirty) === JSON.stringify(['mods']), '清洗后的值真的落盘',
    JSON.stringify(wlAfterDirty))
  await call('app.whitelist.set', { items: wlBefore }, '还原白名单')
  const wlRestored = (await call<{ whitelist?: string[] }>(
    'app.whitelist.get', {}, '回读白名单'))?.whitelist
  check(JSON.stringify(wlRestored) === JSON.stringify(wlBefore), '白名单已还原',
    JSON.stringify(wlBefore))

  // ---- F. 数据库位置：读 / 改到临时目录 / 还原 ----
  const dbBefore = (await call<{ path?: string }>('app.db.get', {}, '读取数据库位置'))?.path ?? ''
  const dbProbe = await probe([dbBefore])
  check(!!dbBefore && dbProbe[dbBefore]?.isDir === true, '数据库路径存在', dbBefore)
  const dbAlt = joinPath(work, 'db-alt')
  const dbSet = await call<{ path?: string }>('app.db.set', { path: dbAlt }, '切到临时数据库')
  check(dbSet?.path === dbAlt, '切库返回新路径', JSON.stringify(dbSet))
  check((await call<{ path?: string }>('app.db.get', {}, '回读数据库位置'))?.path === dbAlt,
    '数据库位置回读一致')
  // 库的形态是 config.json + projects/（不是单个 .db 文件）
  const dbCfg = joinPath(dbAlt, 'config.json')
  const dbProjects = joinPath(dbAlt, 'projects')
  const built = await probe([dbCfg, dbProjects])
  check(built[dbCfg]?.exists === true && built[dbProjects]?.isDir === true,
    '临时数据库已建库', dbAlt)
  await call('app.db.set', { path: dbBefore }, '还原数据库位置')
  check((await call<{ path?: string }>('app.db.get', {}, '回读数据库位置'))?.path === dbBefore,
    '数据库位置已还原', dbBefore)

  // ---- 应用 + 磁盘为准（界面说完成不算）----
  await clickText('确认更新', { exact: true })
  need(await waitText(/更新完成|已应用/, 240000), '更新应用完成')
  await sleep(400)
  const report = (await flowSnap())?.report
  check(report?.failed === 0, '复核 0 失败', JSON.stringify(report))

  const added = joinPath(pack, 'mods/added.jar')
  const gone = joinPath(pack, 'mods/gone.jar')
  const mine = joinPath(pack, 'config/mine.toml')
  const sodium = joinPath(pack, 'config/sodium-options.json')
  const keep = joinPath(pack, 'resourcepacks/keep.zip')
  const after = await probe([oldJar, added, gone, mine, sodium, keep], { text: true })
  check(normText(after[oldJar]?.text) === 'NEW', 'mods 已替换')
  check(normText(after[added]?.text) === 'ADDED', '新增文件已落地')
  check(after[gone]?.exists === false, '本地多余文件已删除')
  check(normText(after[mine]?.text) === 'mine = 1\n', '玩家自己的配置没被动')
  check(normText(after[sodium]?.text) === '{"render_distance": 8}\n', '智能剔除保护生效')
  check(normText(after[keep]?.text) === 'PK-KEEP', '无需更新的文件没被重写')

  // ---- G. 开发者端：导入 → 选项 → 导出 ----
  if (!fx.zip) {
    check(false, '开发者夹具齐全（zip）', JSON.stringify(fx))
    return
  }
  await clickText('开发者', { exact: true })
  need(await waitText(/开始制作更新包/), '切到开发者视图')
  setPicks([fx.zip])
  await clickText('拖入整合包')
  need(await waitText(/更新范围/), '开发者导入整合包')
  let dev = await devSnap()
  check((dev?.strategies ?? []).length >= 2, '开发者列出更新范围',
    JSON.stringify((dev?.strategies ?? []).map((r) => r.name)))
  // 排除 config（点目录名）—— 快照到了不代表行已经画出来
  await waitRow('config')
  await clickRowName('config')
  dev = await devSnap()
  check((dev?.strategies ?? []).find((r) => r.name === 'config')?.excluded === true,
    '目录可被排除（config）')
  // 改策略
  await waitRow('mods')
  await clickRowChip('mods', '替换重名')
  dev = await devSnap()
  check((dev?.strategies ?? []).find((r) => r.name === 'mods')?.current === '替换重名',
    '开发者策略可改（mods → 替换重名）')
  // 导出选项
  await clickText('压缩到最小体积')
  dev = await devSnap()
  check(dev?.options?.min_size === true, '导出选项可切换（min_size）')
  // 更新日志（视图里有 500ms 防抖）
  const changelog = '# 导出夹具\n\n- 开发者端\n'
  setInput('textarea', changelog)
  await sleep(1200)
  dev = await devSnap()
  check(dev?.changelog === changelog, '更新日志写回后端', JSON.stringify(dev?.changelog))
  // 输出位置（走 pick_path 的 save 分支）+ 开始制作
  const outFile = joinPath(work, 'dev-out/E2EExport.eapack')
  setPicks([outFile])
  await clickText('位置…', { exact: true })
  const filled = await waitFor(() => {
    const el = document.querySelector<HTMLInputElement>('input[placeholder="输出文件名"]')
    return el && el.value === 'E2EExport.eapack' ? el.value : null
  }, 10000)
  check(!!filled, '输出文件名已回填', outFile)
  await clickText('开始制作更新包', { exact: true })
  need(await waitText(/已生成：/, 180000), '开发者导出完成（完成态可见）')
  const exportedProbe = await probe([outFile])
  check(exportedProbe[outFile]?.exists === true && (exportedProbe[outFile]?.size ?? 0) > 500,
    '导出的 .eapack 存在', outFile)
  artifacts.exported = outFile
  artifacts.exportChangelog = changelog
  dev = await devSnap()
  check(dev?.phase === 'done' && pathKey(dev?.outPath ?? '') === pathKey(outFile),
    '开发者完成态（快照）',
    JSON.stringify({ phase: dev?.phase, out: dev?.outPath }))
}

/* ------------------------------------------------------------------ */
/* 场景二：真下载（对应 e2e_ui.mjs downloadSuite）                       */
/* ------------------------------------------------------------------ */

async function downloadSuite(dl: DownloadFixture): Promise<void> {
  // 上一段把身份切成了开发者，先切回玩家（同一条流水线里跑两套）
  await clickText('玩家', { exact: true })
  need(await waitText(/更新策略/), '切回玩家视图')
  await call('update.clear', {}, '清空上一轮比对')
  setPicks([dl.pack])
  await clickText('定位整合包')
  need(await waitText(new RegExp(escapeRe(baseName(dl.pack)))), '定位整合包（真下载）')
  setPicks([dl.update])
  await clickStep('STEP 2')
  need(await waitText(/E2EPackDL-a\.eapack/), '导入更新包（带 URL 文件）')
  await clickText('开始更新', { exact: true })
  need(await waitText(/变更内容/, 120000), '比对完成（真下载）')
  await waitText(/e2e-big\.jar/, 30000)
  const snap = await flowSnap()
  check(snap?.counts?.added === 4, '比对出 4 个新增文件', JSON.stringify(snap?.counts))
  check((snap?.groups ?? []).some((g) => g.items.some((i) => i.path.endsWith('e2e-big.jar'))),
    '要下载的大文件出现在变更列表')

  // ---- H：确认更新 → 边下边采样 ----
  // 同一页里跑过基本流程，"更新完成"可能还挂在提示条上：只认**新增的**完成提示。
  const doneBefore = (bodyText().match(/更新完成/g) ?? []).length
  await clickText('确认更新', { exact: true })
  const seen = {
    barMid: 0,
    slots: new Set<string>(),
    hints: new Set<string>(),
    guard: null as { canClose?: boolean; blocked?: boolean } | null,
    done: false,
  }
  const t0 = Date.now()
  let opened = false
  let guardChecked = false
  while (Date.now() - t0 < 180000) {
    const pct = barPct()
    if (pct !== null && pct > 0 && pct < 100) seen.barMid++
    if (!opened && pct !== null) {
      opened = true
      openSlots()
    }
    if (opened) {
      for (const item of slotBusy()) seen.slots.add(item)
      // 确认真的在下载之后，再问"能不能关窗"：下载点关闭必须被拦住
      if (!guardChecked && (seen.barMid > 0 || seen.slots.size > 0)) {
        guardChecked = true
        seen.guard = await call('e2e.can_close', {}, '下载中询问关闭守卫')
        if (seen.guard?.blocked) await dismissDialog()
      }
    }
    const body = bodyText()
    const hit = body.match(/下载 \d+\/\d+|正在下载/)
    if (hit) seen.hints.add(hit[0])
    if ((body.match(/更新完成/g) ?? []).length > doneBefore) {
      seen.done = true
      break
    }
    await sleep(60)
  }
  check(seen.done, '下载 + 应用完成')
  check(seen.barMid > 0, '进度条出现过下载中的进度', `采样 ${seen.barMid} 次`)
  check(seen.slots.size > 0, '槽位面板出现过活动槽位',
    JSON.stringify([...seen.slots].slice(0, 3)))
  check(seen.hints.size > 0, '提示文案有下载进度', JSON.stringify([...seen.hints]))
  check(seen.guard?.blocked === true, '下载中关闭守卫拦截（can_close=False）',
    JSON.stringify(seen.guard))

  const report = (await flowSnap())?.report
  check(report?.failed === 0, '复核 0 失败', JSON.stringify(report))
  for (const f of dl.files) {
    const target = joinPath(dl.pack, f.path)
    const got = await probe([target], { sha256: true })
    check(got[target]?.exists === true && got[target]?.sha256 === f.sha256,
      `下载并落地：${f.name}（${f.size} B）`, target)
  }
  const inpack = joinPath(dl.pack, 'mods/e2e-inpack.jar')
  const inpackProbe = await probe([inpack], { text: true })
  check(normText(inpackProbe[inpack]?.text) === 'INPACK', '包内文件照常落地', inpack)
  const dlCfg = joinPath(dl.pack, 'config/e2e-dl.toml')
  check((await probe([dlCfg]))[dlCfg]?.exists === true, '配置随包落地', dlCfg)
  const guardAfter = await call<{ blocked?: boolean }>('e2e.can_close', {},
    '空闲时询问关闭守卫')
  check(guardAfter?.blocked === false, '空闲时关闭守卫放行', JSON.stringify(guardAfter))

  // ---- H2：下载中中止 → 状态不卡死 → 续传完成 ----
  await call('update.clear', {}, '清空上一轮比对')
  setPicks([dl.abortPack])
  await clickText('定位整合包')
  need(await waitText(new RegExp(escapeRe(baseName(dl.abortPack)))), '定位第二个整合包')
  setPicks([dl.abortUpdate])
  await clickStep('STEP 2')
  need(await waitText(/E2EPackDL-b\.eapack/), '导入续传用更新包')
  await clickText('开始更新', { exact: true })
  need(await waitText(/变更内容/, 120000), '比对完成（续传包）')
  // 上一轮的变更树可能还没被替换掉：等这一轮独有的条目出现
  need(await waitText(/e2e-abort-big\.jar/, 30000), '变更列表已换成续传包内容')
  await clickText('确认更新', { exact: true })
  // 下载态的唯一可靠信号：主按钮变成「取消下载」（界面必须能取消）
  need(!!(await waitFor(() => buttonLabels().includes('取消下载'), 20000)),
    '进入下载态（主按钮变成「取消下载」）')
  await clickText('取消下载', { exact: true })
  need(await waitText(/已请求中止/, 10000), '中止请求已送到后端')
  const stopped = await waitFor(async () => {
    const phase = (await flowSnap())?.phase
    return phase === 'idle' || phase === 'done' ? phase : null
  }, 40000)
  check(stopped === 'idle', '中止后状态回到空闲（不卡死）', JSON.stringify(stopped))
  const afterAbort = await flowSnap()
  check((afterAbort?.pending ?? []).length > 0, '中止后进入"待补入"',
    JSON.stringify(afterAbort?.pending))
  need(await waitText(/还差 \d+ 个文件/, 20000), '界面提示还差文件')
  const guardAborted = await call<{ blocked?: boolean }>('e2e.can_close', {},
    '中止后询问关闭守卫')
  check(guardAborted?.blocked === false, '中止后可以正常关窗（守卫放行）',
    JSON.stringify(guardAborted))

  // 续传：重新比对 → 自动继续下载 → 应用
  const resumeBefore = (bodyText().match(/更新完成/g) ?? []).length
  await call('update.resume', {}, '继续上次的更新')
  need(!!(await waitFor(
    () => (bodyText().match(/更新完成/g) ?? []).length > resumeBefore, 240000, 200)),
    '续传后更新完成')
  const abortTarget = joinPath(dl.abortPack, dl.abortFile.path)
  const aborted = await probe([abortTarget], { sha256: true })
  check(aborted[abortTarget]?.exists === true
    && aborted[abortTarget]?.sha256 === dl.abortFile.sha256,
    '续传后文件字节与源一致', abortTarget)
  const report2 = (await flowSnap())?.report
  check(report2?.failed === 0, '续传后复核 0 失败', JSON.stringify(report2))
  artifacts.downloaded = dl.files.map((f) => joinPath(dl.pack, f.path))
  artifacts.aborted = abortTarget
}

/* ------------------------------------------------------------------ */
/* 入口                                                                */
/* ------------------------------------------------------------------ */

export async function start(): Promise<void> {
  trapErrors()
  statusBox()
  startedAt = Date.now()
  // 第一件事就往磁盘报一行：哪怕后面卡死，也能确认"驱动确实起来了"
  try {
    void bridgeInvoke('e2e.step', { name: 'harness 启动' }, 5000)
  } catch {
    /* 桥还没好就算了，后面 whenReady 会处理 */
  }
  try {
    mark('等桌面桥就绪')
    const ready = await whenReady(20000)
    if (!ready || !bridge()) {
      throw new Error('没有桌面桥接：?e2e=1 只能在 pywebview 或 bridge_server 里跑')
    }
    installPickStub()
    mark('读取 e2e 夹具配置')
    const res = await bridgeInvoke('e2e.config', {}, 30000)
    if (!res || res.ok !== true) {
      throw new Error(
        `e2e.config 失败：${JSON.stringify((res as { error?: unknown } | null)?.error)}`)
    }
    const cfg = res.data as E2eConfig
    suiteName = cfg.suite ?? 'all'
    armHardStop(cfg.budget_ms ?? 720000)

    const wantBasic = suiteName !== 'download' && !!cfg.basic?.pack && !!cfg.basic?.update
    const wantDownload = suiteName !== 'basic' && !!cfg.download?.pack
      && !!cfg.download?.update
    if (!wantBasic && !wantDownload) {
      throw new Error(`夹具不完整（suite=${suiteName}）：${JSON.stringify(cfg)}`)
    }
    if (wantBasic) await basicSuite(cfg.basic as BasicFixture, cfg.work)
    if (wantDownload) await downloadSuite(cfg.download as DownloadFixture)
  } catch (err) {
    reportError = err instanceof Error ? err.message : String(err)
  } finally {
    await finish()
  }
}
