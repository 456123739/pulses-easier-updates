import { useEffect } from 'react'
import { AnimatePresence } from 'framer-motion'
import { invoke, subscribe, whenReady } from '@/bridge'
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

export default function App() {
  const booting = useApp((s) => s.booting)
  const setBooting = useApp((s) => s.setBooting)
  const setState = useApp((s) => s.setState)
  const setConnected = useApp((s) => s.setConnected)
  const applyEvents = useApp((s) => s.applyEvents)
  const pushToast = useApp((s) => s.pushToast)

  useEffect(() => {
    const off = subscribe((events) => applyEvents(events))
    let cancelled = false
    ;(async () => {
      const ready = await whenReady(700)
      if (cancelled) return
      setConnected(ready)
      if (!ready) {
        // 浏览器里打开（设计稿预览）：用假数据，方便截图与联调
        setState(mockState())
        setBooting(false)
        return
      }
      try {
        const state = await invoke<AppState>('app.bootstrap')
        if (cancelled) return
        setState(state)
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
  }, [applyEvents, pushToast, setBooting, setConnected, setState])

  return (
    <>
      <AnimatePresence>{booting && <Splash key="splash" />}</AnimatePresence>
      {!booting && <Shell />}
      <Cover />
      <PreferencesSheet />
      <PendingSheet />
      <ChangelogSheet />
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
