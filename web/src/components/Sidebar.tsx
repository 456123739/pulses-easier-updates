import { AnimatePresence, motion } from 'framer-motion'
import { useState } from 'react'
import wordmark from '@/assets/wordmark.png'
import { invoke, pickPath } from '@/bridge'
import type { AppState } from '@/bridge/protocol'
import { useApp } from '@/store/app'
import { useUi } from '@/store/ui'
import RoleSwitch from './RoleSwitch'
import styles from './Sidebar.module.css'

export default function Sidebar() {
  const role = useApp((s) => s.state?.role ?? 'player')
  const devName = useApp((s) => s.state?.dev?.instance?.name
    ?? s.state?.dev?.source?.name)
  const packName = useApp((s) => s.state?.pack?.name)
  const allRecent = useApp((s) => s.state?.recent ?? [])
  const recent = role === 'developer'
    ? allRecent
    : allRecent.filter((r) => !/\.(zip|mrpack)$/i.test(r.path))
  const version = useApp((s) => s.state?.version ?? '—')
  const connected = useApp((s) => s.connected)
  const setState = useApp((s) => s.setState)
  const pushToast = useApp((s) => s.pushToast)
  const [moreOpen, setMoreOpen] = useState(false)
  const openSheet = useUi((s) => s.openSheet)

  const openPack = async (path: string) => {
    if (!connected) return
    try {
      const res = await invoke<{ state: { version: string } }>('app.pick_pack', { path })
      const next = (res as unknown as { state?: AppState }).state
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
    if (!connected || !recent.length) return
    if (!window.confirm('清空最近打开记录？不会删除任何整合包。')) return
    try {
      await invoke('app.recent.clear')
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

      <div className={styles.currentPack}>
        <span className="t-small">当前整合包</span>
        <button className={styles.packLink} onClick={() => void onChoose()} title={role === 'developer' ? devName : packName}>
          {role === 'developer' ? devName ?? '未选择' : packName ?? '未选择'}
        </button>
        <span className={styles.packHint}>点击更换 · 也可在右侧直接拖入</span>
      </div>

      <div className={`card ${styles.recentCard}`}>
        <div className={styles.cardHead}>
          <span className={styles.cardTitle}>最近打开</span>
          <button className={styles.linkBtn} onClick={clearRecent}>清空记录</button>
        </div>
        <div className={styles.recentList}>
          {recent.length ? (
            recent.map((r) => (
              <motion.button
                key={r.path}
                className={styles.recentRow}
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
      <div className={styles.version}>v{version}</div>
    </aside>
  )
}
