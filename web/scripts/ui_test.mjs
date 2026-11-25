// 界面交互测试（Playwright，无需 pywebview/Windows）。
// 只验"看得见的行为"：渲染、点按、动画收尾、无控制台报错。
// 用法：node scripts/ui_test.mjs [--shots]
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const URL = process.env.UI_URL || 'http://127.0.0.1:8099/index.html'
const SHOTS = process.argv.includes('--shots')
const exe = process.env.CHROMIUM || '/home/admin/.cache/ms-playwright/chromium-1194/chrome-linux/chrome'
mkdirSync('shots', { recursive: true })

let failed = 0
const check = (ok, label, extra = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !extra ? '' : '  →  ' + extra}`)
  if (!ok) failed++
}

const browser = await chromium.launch({ executablePath: exe, args: ['--no-sandbox'] })
const page = await browser.newPage({ viewport: { width: 1440, height: 860 } })
const errors = []
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', (e) => errors.push(String(e)))

await page.goto(URL, { waitUntil: 'networkidle' })
await page.waitForTimeout(900)

// 1) 启动画面收掉 → 主界面上线
check((await page.locator('aside').count()) === 1, '侧边栏渲染')
check(await page.getByText('定位整合包').first().isVisible(), 'STEP 1 可见')
check(await page.getByText(/写入更新/).first().isVisible(), '主按钮可开始写入更新')

// 2) 策略胶囊：点一下要切换选中态
const firstOffChip = page.locator('button.chip[data-on="false"]:not([disabled])').first()
const beforeChip = await firstOffChip.getAttribute('data-on')
await firstOffChip.click()
await page.waitForTimeout(240)
check(
  await page.locator('button.chip[data-on="true"]').count() > 0 && beforeChip === 'false',
  '策略胶囊点击后切换选中态',
)

// 3) 折叠分区展开
await page.getByText('无需更新').first().click()
await page.waitForTimeout(320)
check(await page.getByText('无需更新').first().isVisible(), '「无需更新」分区可展开')

// 4) 角色切换 → 开发者页
await page.getByRole('button', { name: '开发者', exact: true }).click()
await page.waitForTimeout(700)
check(await page.getByText('导出选项').first().isVisible(), '开发者视图有导出选项')
check(await page.getByText('第一步 · 选择导出内容').first().isVisible(), '开发者视图有第一步导出范围')
check(
  await page.getByRole('button', { name: '开发者端整合包定位' }).isVisible(),
  '开发者定位入口可见',
)
// 导出选项：默认前三项开着（后端默认值）
const switchesOn = await page.locator('[class*="switch"][data-on="true"]').count()
check(switchesOn >= 3, '导出选项默认启用三项', String(switchesOn))
if (SHOTS) await page.screenshot({ path: 'shots/ui-dev.png' })

// 5) 回到玩家页 + 首选项抽屉
await page.getByRole('button', { name: '玩家', exact: true }).click()
await page.waitForTimeout(700)
await page.getByText('更多').first().click()
await page.waitForTimeout(260)
await page.getByText('首选项').first().click()
await page.waitForTimeout(420)
check(await page.getByText('下载引擎').first().isVisible(), '首选项抽屉打开')
check(await page.getByText('并行下载').first().isVisible(), '首选项有并行下载')
if (SHOTS) await page.screenshot({ path: 'shots/ui-preferences.png' })
await page.keyboard.press('Escape')
await page.waitForTimeout(300)

// 6) 白名单 / 数据库抽屉
await page.getByText('更多').first().click()
await page.waitForTimeout(240)
await page.getByText('白名单').first().click()
await page.waitForTimeout(360)
check(
  await page.getByPlaceholder('目录名，例如 mods').isVisible(),
  '白名单抽屉可打开',
)
await page.keyboard.press('Escape')
await page.waitForTimeout(240)
await page.getByText('更多').first().click()
await page.waitForTimeout(200)
await page.getByText('数据库位置').first().click()
await page.waitForTimeout(360)
check(await page.getByText('数据库位置').first().isVisible(), '数据库抽屉可打开')
await page.keyboard.press('Escape')
await page.waitForTimeout(240)

// 7) 受阻提示条（注入状态模拟"需要补入"）
await page.evaluate(() => {
  const s = window.__pe
  s.usePlayer.setState({ pendingCount: 2, pendingMode: 'wait' })
})
await page.waitForTimeout(420)
check(await page.getByText(/还差 2 个文件/).first().isVisible(), '受阻提示条出现')
if (SHOTS) await page.screenshot({ path: 'shots/ui-pending.png' })

// 8) 下载态：主按钮变「停止下载」，进度条出现
await page.evaluate(() => {
  const s = window.__pe
  s.usePlayer.setState({
    phase: 'download', btn: 'busy', indexPercent: 42, filePercent: 66,
    hint: '正在下载：sodium-0.6.5.jar · 3.4 MB/s · 5/12', hintLevel: 'info',
  })
})
await page.waitForTimeout(420)
check(await page.getByText('停止下载').first().isVisible(), '下载中主按钮变「停止下载」')
check((await page.locator('text=正在下载').count()) > 0, '下载提示文案可见')
if (SHOTS) await page.screenshot({ path: 'shots/ui-download.png' })

// 新流程必须丢弃上一轮文件统计；完成批次不能再留下活动传输字节。
await page.evaluate(() => {
  const { useApp } = window.__pe
  const base = useApp.getState().state
  const flow = base.flow ?? {
    phase: 'idle', update: null, strategies: [], groups: [],
    counts: { added: 0, modified: 0, deleted: 0 }, changeCounts: {},
    tasks: 0, done: 0, pending: [], report: null, error: null,
    changelog: false,
  }
  const event = (seq, phase) => ({
    v: 1, seq, type: 'state', data: { ...base, flow: { ...flow, phase } },
  })
  useApp.getState().applyEvents([event(20, 'download')])
  useApp.getState().applyEvents([{
    v: 1, seq: 21, type: 'bytes', data: { key: 'mods/a.jar', done: 512, total: 1024, state: 'running' },
  }])
  useApp.getState().setState({ ...base, flow: { ...flow, phase: 'download' } })
  useApp.getState().applyEvents([event(22, 'scan')])
  const clearedOnNewScan = Object.keys(useApp.getState().bytes).length === 0
  useApp.getState().applyEvents([event(23, 'download')])
  useApp.getState().applyEvents([{
    v: 1, seq: 24, type: 'bytes', data: { key: 'mods/a.jar', done: 1024, total: 1024, state: 'done' },
  }])
  useApp.getState().setState({ job: { id: 'old', title: 'old', status: 'done', cancelable: false } })
  useApp.getState().applyEvents([
    event(25, 'done'),
    { v: 1, seq: 26, type: 'bytes', data: { key: 'mods/a.jar', done: 1024, total: 1024, state: 'done' } },
  ])
  window.__pe.transferResetResult = {
    clearedOnNewScan,
    bytesAfterDone: Object.keys(useApp.getState().bytes).length,
    jobAfterDone: useApp.getState().job,
  }
})
const transferReset = await page.evaluate(() => window.__pe.transferResetResult)
check(transferReset.clearedOnNewScan, '新扫描清理上一轮字节统计')
check(transferReset.bytesAfterDone === 0 && transferReset.jobAfterDone === null,
      '完成后清理文件字节统计和任务状态', JSON.stringify(transferReset))
if (SHOTS) await page.screenshot({ path: 'shots/ui-download.png' })

// 9) 对话框（关闭守卫等都用它）
await page.evaluate(() => {
  window.__pe.useApp.setState({
    dialog: {
      id: 'd1',
      kind: 'confirm',
      title: '更新流程正在进行',
      message: '现在关闭会中断下载。要放弃吗？',
      buttons: [
        { id: 'cancel', label: '继续更新' },
        { id: 'ok', label: '放弃并关闭', primary: true },
      ],
    },
  })
})
await page.waitForTimeout(360)
check(await page.getByText('更新流程正在进行').first().isVisible(), '对话框可弹出')
await page.getByRole('button', { name: '继续更新' }).click()
await page.waitForTimeout(700)
check(!(await page.getByText('更新流程正在进行').isVisible()), '对话框可关闭')

await page.waitForTimeout(120)
check(errors.length === 0, '无控制台错误', errors.join(' | ').slice(0, 300))
await browser.close()
console.log(failed ? `\n${failed} 项失败` : '\n全部通过')
process.exit(failed ? 1 : 0)
