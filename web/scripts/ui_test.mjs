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
check(await page.getByText('确认更新').first().isVisible(), '主按钮为「确认更新」')

// 2) 策略胶囊：点一下要切换选中态
const rowStrategy = () =>
  page.evaluate(() =>
    window.__pe.usePlayer.getState().strategies.find((s) => s.name === 'config')?.current,
  )
const beforeStrategy = await rowStrategy()
await page.locator('button.chip[data-on="false"]:not([disabled])').first().click()
await page.waitForTimeout(240)
const afterStrategy = await rowStrategy()
check(
  beforeStrategy !== afterStrategy,
  '策略胶囊点击后写入界面状态',
  `${beforeStrategy} → ${afterStrategy}`,
)

// 3) 折叠分区展开
await page.getByText('无需更新').first().click()
await page.waitForTimeout(320)
check(await page.getByText('无需更新').first().isVisible(), '「无需更新」分区可展开')

// 4) 角色切换 → 开发者页
await page.getByRole('button', { name: '开发者', exact: true }).click()
await page.waitForTimeout(700)
check(await page.getByText('导出选项').first().isVisible(), '开发者视图有导出选项')
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

// 6) 受阻提示条（注入状态模拟"需要补入"）
await page.evaluate(() => {
  const s = window.__pe
  s.usePlayer.setState({ pendingCount: 2, pendingMode: 'wait' })
})
await page.waitForTimeout(420)
check(await page.getByText(/还差 2 个文件/).first().isVisible(), '受阻提示条出现')
if (SHOTS) await page.screenshot({ path: 'shots/ui-pending.png' })

// 7) 下载态：主按钮变「取消下载」，进度条出现
await page.evaluate(() => {
  const s = window.__pe
  s.usePlayer.setState({
    phase: 'download', btn: 'busy', indexPercent: 42, filePercent: 66,
    hint: '正在下载：sodium-0.6.5.jar · 3.4 MB/s · 5/12', hintLevel: 'info',
  })
})
await page.waitForTimeout(420)
check(await page.getByText('取消下载').first().isVisible(), '下载中主按钮变「取消下载」')
check((await page.locator('text=正在下载').count()) > 0, '下载提示文案可见')
if (SHOTS) await page.screenshot({ path: 'shots/ui-download.png' })

await page.waitForTimeout(120)
check(errors.length === 0, '无控制台错误', errors.join(' | ').slice(0, 300))
await browser.close()
console.log(failed ? `\n${failed} 项失败` : '\n全部通过')
process.exit(failed ? 1 : 0)
