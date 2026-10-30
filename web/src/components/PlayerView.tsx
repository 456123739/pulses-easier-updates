import { useEffect, useState } from 'react'
import { invoke } from '@/bridge'
import { AnimatePresence, motion } from 'framer-motion'
import { pickPath } from '@/bridge'
import { useApp } from '@/store/app'
import { usePlayer } from '@/store/player'
import { useUi } from '@/store/ui'
import DropZone from './DropZone'
import StrategyCard from './StrategyCard'
import ChangeTree from './ChangeTree'
import ProgressBar from './ProgressBar'
import LogPanel from './LogPanel'
import MainButton from './MainButton'
import SlotPanel from './SlotPanel'
import PendingBanner from './PendingBanner'
import styles from './PlayerView.module.css'

export default function PlayerView() {
  const connected = useApp((s) => s.connected)
  const flow = useApp((s) => s.state?.flow)
  const applyFlow = usePlayer((s) => s.applyFlow)
  const pushToast = useApp((s) => s.pushToast)
  const openSheet = useUi((s) => s.openSheet)
  const progress = useApp((s) => s.progress)

  // 后端快照是唯一事实来源：连上桥就覆盖本地演示态
  useEffect(() => {
    if (flow) applyFlow(flow)
  }, [flow, applyFlow])
  const pack = useApp((s) => s.state?.pack)
  const updatePack = usePlayer((s) => s.updatePack)
  const hint = usePlayer((s) => s.hint)
  const hintLevel = usePlayer((s) => s.hintLevel)
  const phase = usePlayer((s) => s.phase)
  const groups = usePlayer((s) => s.groups)
  const indexPercent = usePlayer((s) => s.indexPercent)
  const slotsOpen = usePlayer((s) => s.slotsOpen)
  const pendingCount = usePlayer((s) => s.pendingCount)
  const pendingMode = usePlayer((s) => s.pendingMode)
  const placeholder = usePlayer((s) => s.placeholder)
  const toggleSlots = usePlayer((s) => s.toggleSlots)
  const setHint = usePlayer((s) => s.setHint)
  const [showTree, setShowTree] = useState(true)

  const onPickUpdate = async () => {
    if (!connected) return
    try {
      const path = await pickPath('file', '')
      if (!path) return
      await invoke('update.pick', { path })
      setHint(`已导入更新包：${path.split(/[\\/]/).pop()}`, 'info')
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '导入失败', 'error')
    }
  }

  const onClearUpdate = async () => {
    if (!connected) return
    try {
      await invoke('update.clear')
    } catch {
      /* 忽略 */
    }
  }

  const setStrategy = async (name: string, value: string) => {
    if (!connected) return
    try {
      await invoke('update.strategy.set', { name, value })
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '策略保存失败', 'error')
    }
  }

  const toggleChecked = async (name: string) => {
    if (!connected) return
    const row = flow?.strategies.find((r) => r.name === name)
    try {
      await invoke('update.strategy.set', { name, checked: !(row?.checked ?? true) })
    } catch {
      /* 忽略 */
    }
  }

  const hasDiff = showTree && groups.some((g) => g.items.length > 0)
  // 后端进度文案优先：玩家看到的"正在下载/正在应用/更新完成"都来自这里
  const liveHint =
    phase !== 'idle' || (progress?.percent ?? 0) >= 1
      ? progress?.message || hint
      : hint

  return (
    <div className={styles.page}>
      <section className={styles.left}>
        <div className={styles.dropWrap}>
          <DropZone
            step="STEP 2"
            title={updatePack ? `更新包：${updatePack.name}` : '拖入更新包'}
            subtitle={updatePack ? undefined : '支持 .zip / .eapack'}
            hint={updatePack ? undefined : '或点击选择'}
            height={112}
            highlight={!updatePack}
            onPick={onPickUpdate}
          />
          <div className={styles.clearRow}>
            <button
              className="btn btn-ghost"
              style={{ height: 28, fontSize: 11.5 }}
              onClick={onClearUpdate}
              disabled={!updatePack}
            >
              清空
            </button>
          </div>
        </div>

        <StrategyCard
          onStrategy={connected ? setStrategy : undefined}
          onToggle={connected ? toggleChecked : undefined}
        />

        <MainButton onScanToggle={() => setShowTree((v) => !v)} />
      </section>

      <section className={styles.right}>
        <div className={styles.treeArea}>
          <AnimatePresence mode="wait" initial={false}>
            {pendingCount > 0 && pendingMode ? (
              <motion.div
                key="pending"
                className={styles.pendingSlot}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -6 }}
                transition={{ type: 'spring', stiffness: 320, damping: 30 }}
              >
                <PendingBanner count={pendingCount} mode={pendingMode} />
              </motion.div>
            ) : null}
            {hasDiff ? (
              <motion.div
                key="tree"
                className={styles.treeSlot}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -8 }}
                transition={{ type: 'spring', stiffness: 320, damping: 30 }}
              >
                <ChangeTree />
              </motion.div>
            ) : (
              <motion.div
                key="placeholder"
                className={`card ${styles.placeholder}`}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
              >
                <span>{placeholder}</span>
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        <AnimatePresence initial={false}>
          {indexPercent !== null && (
            <motion.div
              className={styles.progressRow}
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
            >
              <ProgressBar onClick={toggleSlots} />
            </motion.div>
          )}
        </AnimatePresence>

        {liveHint && (
          <div className={styles.hint} data-level={hintLevel}>
            {liveHint}
          </div>
        )}

        <AnimatePresence initial={false}>
          {slotsOpen && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ type: 'spring', stiffness: 300, damping: 32 }}
              style={{ overflow: 'hidden' }}
            >
              <SlotPanel onClose={toggleSlots} />
            </motion.div>
          )}
        </AnimatePresence>

        <LogPanel />

        <div className={styles.footRow}>
          <button
            className="btn"
            style={{ height: 36, padding: '0 18px' }}
            onClick={() => openSheet('changelog')}
          >
            查看更新日志
          </button>
          <span className={styles.packHint}>
            {pack ? `${pack.name} · ${pack.fileCount ?? 0} 个模组` : '尚未定位整合包'}
            {phase !== 'idle' ? ` · ${phase}` : ''}
          </span>
        </div>
      </section>
    </div>
  )
}
