// 给开发者页灌一份演示状态后截图（纯前端，不连 Python）
import { chromium } from 'playwright'
const exe = process.env.CHROMIUM || '/home/admin/.cache/ms-playwright/chromium-1194/chrome-linux/chrome'
const browser = await chromium.launch({ executablePath: exe, args: ['--no-sandbox'] })
const page = await browser.newPage({ viewport: { width: 1440, height: 860 } })
await page.goto('http://127.0.0.1:8099/index.html', { waitUntil: 'networkidle' })
await page.waitForTimeout(900)
await page.evaluate(() => {
  const s = window.__pe
  const st = s.useApp.getState().state
  s.useApp.setState({
    state: {
      ...st,
      role: 'developer',
      dev: {
        phase: 'idle',
        source: { path: 'D:/Packs/MyPack.zip', name: 'MyPack-2.3.zip' },
        name: 'MyPack',
        version: '2.3.0',
        files: 1284,
        strategies: [
          { name: 'mods', current: '完全匹配', excluded: false, isFile: false },
          { name: 'config', current: '完全匹配', excluded: false, isFile: false },
          { name: 'tacz', current: '完全匹配', excluded: false, isFile: false },
          { name: 'defaultconfigs', current: '替换重名', excluded: false, isFile: false },
          { name: 'resourcepacks', current: '完全匹配', excluded: true, isFile: false },
          { name: 'options.txt', current: '完全匹配', excluded: false, isFile: true },
        ],
        changelog: '## 2.3.0\n\n- 新增 Create 与 Farmer\'s Delight\n- 修复启动器在 4K 下的模糊\n- 优化包体：移除了重复的库文件\n',
        options: { include_hashes: true, tamper_proof: true, precompute_overrides: true, min_size: false },
        outPath: '',
        error: null,
        progress: {},
      },
    },
  })
})
await page.waitForTimeout(600)
await page.screenshot({ path: 'shots/dev-filled.png' })
await browser.close()
console.log('saved shots/dev-filled.png')
