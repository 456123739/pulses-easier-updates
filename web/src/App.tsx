import { Suspense, lazy, useEffect, useState } from 'react'
import { AnimatePresence } from 'framer-motion'
import { invoke, subscribe, whenReady } from '@/bridge'
import { expandWindow, isDesktop } from '@/window'
import type { AppState } from '@/bridge/protocol'
import { useApp } from '@/store/app'
import Splash from '@/components/Splash'
import Shell from '@/components/Shell'
import DialogHost from '@/components/DialogHost'
import PreferencesSheet from '@/components/PreferencesSheet'
import Cover from '@/components/Cover'
import PendingSheet from '@/components/PendingSheet'
import ChangelogSheet from '@/components/ChangelogSheet'
import WhitelistSheet from '@/components/WhitelistSheet'
import DbSheet from '@/components/DbSheet'
import ToastHost from '@/components/ToastHost'
import { useUi } from '@/store/ui'

// 编辑器（Tiptap + markdown-it）单拉一个 chunk：不开就不下载、不解析
const MarkdownEditorSheet = lazy(() => import('@/components/MarkdownEditorSheet'))

export default function App() {
  const booting = useApp((s) => s.booting)
  const setBooting = useApp((s) => s.setBooting)
  const setState = useApp((s) => s.setState)
  const setConnected = useApp((s) => s.setConnected)
  const applyEvents = useApp((s) => s.applyEvents)
  const pushToast = useApp((s) => s.pushToast)
  const sheet = useUi((s) => s.sheet)
  const [editorReady, setEditorReady] = useState(false)
  const [expanding, setExpanding] = useState(false)
  const [bridgeError, setBridgeError] = useState(false)
  const [bootAttempt, setBootAttempt] = useState(0)
  const desktop = isDesktop()

  // 第一次点开才加载编辑器；之后保活，关闭动画不丢
  useEffect(() => {
    if (sheet === 'changelog-editor') setEditorReady(true)
  }, [sheet])

  useEffect(() => {
    const off = subscribe((events) => applyEvents(events))
    let cancelled = false
    ;(async () => {
      const ready = await whenReady(desktop ? 15000 : 700)
      if (cancelled) return
      setConnected(ready)
      if (!ready) {
        if (desktop) {
          setBridgeError(true)
          setBooting(false)
        } else {
          // 浏览器里打开（设计稿预览）：用假数据，方便截图与联调
          setState(mockState())
          setBooting(false)
        }
        return
      }
      setBridgeError(false)
      try {
        const state = await invoke<AppState>('app.bootstrap')
        if (cancelled) return
        setState(state)
        if (isDesktop()) {
          // 先把 logo 模糊淡出成主界面底色（固定 240px，不随窗口拉伸），
          // 再展开纯色窗口；中间不挂载 Shell，避免每帧重排整个界面。
          setExpanding(true)
          await new Promise((resolve) => setTimeout(resolve, 180))
          await expandWindow()
          await new Promise((resolve) => setTimeout(resolve, 620))
        }
      } catch (err) {
        pushToast(err instanceof Error ? err.message : '启动失败', 'error')
      } finally {
        if (!cancelled) setBooting(false)
      }
    })()
    return () => {
      cancelled = true
      off()
    }
  }, [applyEvents, bootAttempt, desktop, pushToast, setBooting, setConnected, setState])

  return (
    <>
      <AnimatePresence>{booting && <Splash key="splash" expanding={expanding} />}</AnimatePresence>
      {!booting && !bridgeError && <Shell />}
      {!booting && bridgeError && (
        <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', padding: 32, background: '#111214', color: '#f2f3f4' }}>
          <div style={{ maxWidth: 440, textAlign: 'center' }}>
            <h1 style={{ margin: '0 0 12px', fontSize: 20 }}>桌面桥接未就绪</h1>
            <p style={{ margin: '0 0 20px', color: '#a9adb3', lineHeight: 1.6 }}>应用界面已打开，但后台服务没有响应。可以重试连接，不会进入演示数据模式。</p>
            <button className="btn" onClick={() => { setBridgeError(false); setBooting(true); setBootAttempt((n) => n + 1) }}>重试连接</button>
          </div>
        </div>
      )}
      <Cover />
      <PreferencesSheet />
      <PendingSheet />
      <ChangelogSheet />
      {editorReady && (
        <Suspense fallback={null}>
          <MarkdownEditorSheet />
        </Suspense>
      )}
      <WhitelistSheet />
      <DbSheet />
      <DialogHost />
      <ToastHost />
    </>
  )
}

function mockState(): AppState {
  return {
    version: '0.9.2',
    role: 'player',
    pack: null,
    recent: [
      { name: '1.20.1-Fabric', path: 'D:\\Games\\Minecraft\\PCL\\.minecraft\\versions\\1.20.1-Fabric' },
    ],
    settings: {},
    busy: false,
    job: null,
  }
}
