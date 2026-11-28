// Startup/role animation boundaries against the real build; no GUI server required.
// Usage: node scripts/motion_test.mjs (optional CHROMIUM override).
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, extname, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const dist = resolve(dirname(fileURLToPath(import.meta.url)), '../dist')
const origin = 'http://motion.test'
const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' }
const browser = await chromium.launch({
  ...(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {}),
  args: ['--no-sandbox'],
})
const errors = []
let checks = 0
const check = (value, label) => { assert.ok(value, label); checks++; console.log(`PASS  ${label}`) }

async function open({ reduced = false, motionOff = false } = {}) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 860 }, reducedMotion: reduced ? 'reduce' : 'no-preference' })
  await context.route(`${origin}/**`, async (route) => {
    const pathname = decodeURIComponent(new URL(route.request().url()).pathname)
    const file = resolve(dist, `.${pathname === '/' ? '/index.html' : pathname}`)
    assert.ok(file.startsWith(`${dist}${sep}`), 'asset stays inside dist')
    await route.fulfill({ body: readFileSync(file), contentType: types[extname(file)] || 'application/octet-stream' })
  })
  await context.addInitScript(({ motionOff }) => {
    const state = { version: 'test', role: 'player', pack: null, recent: [], settings: { window_anim: motionOff ? 0 : 1 }, busy: false, job: null }
    const probe = window.__motion = { expands: [], roles: [], failRole: false }
    window.pywebview = { platform: 'test', api: { invoke(method, params) {
      if (method === 'app.bootstrap') return Promise.resolve({ ok: true, data: state })
      if (method === 'window.expand') {
        probe.expands.push(params)
        return new Promise((resolve) => { probe.finishExpand = () => resolve({ ok: true, data: { expanding: false } }) })
      }
      if (method === 'app.role.set') {
        const cover = document.querySelector('[data-role-cover]')
        probe.roles.push({ role: params.role, opacity: cover ? Number(getComputedStyle(cover).opacity) : null, time: performance.now() })
        if (probe.failRole) return Promise.resolve({ ok: false, error: { code: 'BUSY', message: '暂时不能切换角色' } })
        // Authoritative state can arrive before the bridge reply; it must stay hidden.
        const next = { ...window.__pe.useApp.getState().state, role: params.role }
        window.__PE.push([{ v: 1, seq: probe.roles.length, type: 'state', data: next }])
        return new Promise((resolve) => { probe.finishRole = () => resolve({ ok: true, data: { role: params.role } }) })
      }
      return Promise.resolve({ ok: true, data: {} })
    } } }
  }, { motionOff })
  const page = await context.newPage()
  page.on('pageerror', (error) => errors.push(String(error)))
  page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()) })
  await page.goto(`${origin}/index.html`)
  await page.waitForFunction(() => window.__motion?.expands.length === 1)
  return { context, page }
}

try {
  const { context, page } = await open()
  await page.waitForTimeout(760) // Longer than the former fixed 620ms reveal timer.
  check(await page.locator('aside').count() === 0 && await page.evaluate(() => window.__pe.useApp.getState().booting), 'startup keeps heavy Shell unmounted until native completion')
  const logo = await page.getByAltText('Pulses Easier', { exact: true }).evaluate((img) => ({ width: img.width, height: img.height, opacity: getComputedStyle(img).opacity, filter: getComputedStyle(img).filter }))
  check(logo.width === 240 && logo.height === 240 && Number(logo.opacity) === 0 && logo.filter === 'none', 'expansion has fixed-size faded logo with no animated blur')
  check(await page.evaluate(() => window.__motion.expands[0].animate === true), 'normal startup requests native animation')
  await page.evaluate(() => window.__motion.finishExpand())
  await page.waitForSelector('aside')
  await page.waitForFunction(() => !document.querySelector('[class*="Splash_wrap"]'))
  check(await page.getByRole('button', { name: '最大化', exact: true }).isVisible(), 'desktop title bar appears after expansion')

  await page.evaluate(() => { window.__motion.thumb = document.querySelector('[data-role] > span') })
  await page.getByRole('button', { name: '开发者', exact: true }).click()
  await page.waitForFunction(() => window.__motion.roles.length === 1)
  check(await page.evaluate(() => window.__motion.roles[0].opacity === 1), 'role bridge and authoritative page mount start only behind opaque cover')
  await page.evaluate(() => {
    for (let i = 0; i < 6; i++) Array.from(document.querySelectorAll('button')).find((b) => b.textContent === '玩家')?.click()
  })
  await page.waitForTimeout(760)
  check(await page.evaluate(() => window.__motion.roles.length === 1 && window.__pe.useUi.getState().covering && Number(getComputedStyle(document.querySelector('[data-role-cover]')).opacity) === 1), 'slow role reply stays covered and duplicate requests are ignored')
  await page.evaluate(() => window.__motion.finishRole())
  await page.waitForFunction(() => !document.querySelector('[data-role-cover]'))
  check(await page.getByRole('button', { name: '开发者', exact: true }).getAttribute('data-active') === 'true', 'successful role settles on developer page')
  const thumb = await page.evaluate(() => {
    const seg = document.querySelector('[data-role]')
    const span = seg.querySelector(':scope > span')
    const active = seg.querySelector('[data-active="true"]')
    const a = span.getBoundingClientRect(), b = active.getBoundingClientRect()
    return { same: window.__motion.thumb === span, property: getComputedStyle(span).transitionProperty, dx: Math.abs(a.x - b.x), dw: Math.abs(a.width - b.width) }
  })
  check(thumb.same && thumb.property === 'transform' && thumb.dx < 1 && thumb.dw < 1, 'role thumb remains one element and moves by transform to the correct geometry')

  await page.evaluate(() => { window.__motion.failRole = true })
  await page.getByRole('button', { name: '玩家', exact: true }).click()
  await page.waitForFunction(() => window.__motion.roles.length === 2 && !document.querySelector('[data-role-cover]'))
  check(await page.getByRole('button', { name: '开发者', exact: true }).getAttribute('data-active') === 'true', 'rejected role change reveals original page and clears cover')
  await context.close()

  for (const options of [{ reduced: true }, { motionOff: true }]) {
    const { context, page } = await open(options)
    const label = options.reduced ? 'system reduced motion' : 'window animation preference off'
    check(await page.evaluate(() => window.__motion.expands[0].animate === false), `${label} skips native size tween`)
    await page.evaluate(() => window.__motion.finishExpand())
    await page.waitForSelector('aside')
    await page.waitForFunction(() => !document.querySelector('[class*="Splash_wrap"]'))
    await page.getByRole('button', { name: '开发者', exact: true }).click()
    await page.waitForFunction(() => window.__motion.roles.length === 1)
    await page.evaluate(() => window.__motion.finishRole())
    await page.waitForFunction(() => !document.querySelector('[data-role-cover]'))
    const duration = await page.locator('[data-role] > span').evaluate((span) => getComputedStyle(span).transitionDuration)
    check(duration.split(',').every((value) => parseFloat(value) <= 0.001), `${label} leaves no role thumb motion or cover behind`)
    await context.close()
  }
  check(errors.length === 0, `no browser console errors (${errors.join(' | ')})`)
  console.log(`\n${checks} motion checks passed`)
} finally {
  await browser.close()
}
