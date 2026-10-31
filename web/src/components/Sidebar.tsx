import { AnimatePresence, motion } from 'framer-motion'
import { useState } from 'react'
import wordmark from '@/assets/wordmark.png'
import { invoke, pickPath } from '@/bridge'
import { useApp } from '@/store/app'
import { useUi } from '@/store/ui'
import DropZone from './DropZone'
import RoleSwitch from './RoleSwitch'
import styles from './Sidebar.module.css'

export default function Sidebar() {
  const state = useApp((s) => s.state)
  const connected = useApp((s) => s.connected)
  const setState = useApp((s) => s.setState)
  const pushToast = useApp((s) => s.pushToast)
  const [moreOpen, setMoreOpen] = useState(false)
  const openSheet = useUi((s) => s.openSheet)

  const openPack = async (path: string) => {
    if (!connected) return
    try {
      const res = await invoke<{ state: { version: string } }>('app.pick_pack', { path })
      const next = (res as unknown as { state: typeof state }).state
      if (next) setState(next)
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '定位失败', 'error')
    }
  }

  const onChoose = async () => {
    if (!connected) return
    try {
      const path = await pickPath('file', '')
      if (path) await openPack(path)
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '选择失败', 'error')
    }
  }

  const clearRecent = async () => {
    if (!connected || !state) return
    try {
      await invoke('app.recent.clear')
      setState({ ...state, recent: [] })
    } catch {
      /* 忽略 */
    }
  }

  return (
    <aside className={styles.sidebar}>
      <div className={styles.brand}>
        <img className={styles.wordmark} src={wordmark} alt="Pulses Easier" draggable={false} />
        <div className={styles.maker}>Produced by Pulses0 Studio</div>
      </div>

      <DropZone
        step="STEP 1"
        title={state?.role === 'developer' ? '导入整合包' : '定位整合包'}
        subtitle={
          state?.role === 'developer'
            ? '启动器导出的 .zip / .mrpack'
            : state?.pack
              ? state.pack.name
              : '拖入整合包内任意文件'
        }
        hint="或点击选择"
        height={104}
        variant="sidebar"
        onPick={onChoose}
      />

      <div className={`card ${styles.recentCard}`}>
        <div className={styles.cardHead}>
          <span className={styles.cardTitle}>最近打开</span>
          <button className={styles.linkBtn} onClick={clearRecent}>清空</button>
        </div>
        <div className={styles.recentList}>
          {state?.recent?.length ? (
            state.recent.map((r) => (
              <motion.button
                key={r.path}
                className={styles.recentRow}
                whileHover={{ x: 2 }}
                whileTap={{ scale: 0.99 }}
                title={r.path}
                onClick={() => void openPack(r.path)}
              >
                {r.name}
              </motion.button>
            ))
          ) : (
            <div className={styles.empty}>暂无记录</div>
          )}
        </div>
      </div>

      <div className={`card ${styles.moreCard}`}>
        <button className={styles.moreHead} onClick={() => setMoreOpen((v) => !v)}>
          <motion.span animate={{ rotate: moreOpen ? 90 : 0 }} transition={{ type: 'spring', stiffness: 420, damping: 30 }}>
            ▶
          </motion.span>
          <span>更多</span>
        </button>
        <AnimatePresence initial={false}>
          {moreOpen && (
            <motion.div
              className={styles.moreBody}
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ type: 'spring', stiffness: 320, damping: 32 }}
            >
              <button className={styles.moreItem} onClick={() => openSheet('db')}>
                数据库位置
              </button>
              <button className={styles.moreItem} onClick={() => openSheet('preferences')}>
                首选项
              </button>
              <button className={styles.moreItem} onClick={() => openSheet('whitelist')}>
                白名单
              </button>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      <div className={styles.spacer} />

      <RoleSwitch />
      <div className={styles.version}>v{state?.version ?? '—'}</div>
    </aside>
  )
}
