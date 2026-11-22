import { useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { invoke, pickPath } from '@/bridge'
import { useApp } from '@/store/app'
import { usePlayer } from '@/store/player'
import type { StrategyRow } from '@/store/player'
import Icon from './Icon'
import styles from './StrategyCard.module.css'

const STRATEGY_HELP: Record<string, string> = {
  '完全匹配': '以更新包为准：同名覆盖，包里没有的删除',
  '替换重名': '只覆盖本地已有同名文件，不删除其他文件',
  '跳过重名': '本地已有就保留，不覆盖',
  '智能剔除': '覆盖配置，但优化/渲染类模组的配置保留本地版本',
  '保留键位': '覆盖文件，但保留本地 key_* 键位行',
}

interface Props {
  rows?: StrategyRow[]
  onToggle?: (name: string) => void
  onStrategy?: (name: string, value: string) => void
  compact?: boolean
  /** 把「提交草稿」交给父级，让底部唯一主按钮一次性提交+应用 */
  registerCommit?: (fn: () => Promise<void>) => void
}

export default function StrategyCard({ rows, onToggle, onStrategy, compact, registerCommit }: Props) {
  const all = usePlayer((s) => s.strategies)
  const toggleRow = usePlayer((s) => s.toggleRow)
  const data = rows ?? all
  const [showDisabled, setShowDisabled] = useState(false)
  const [showUnchanged, setShowUnchanged] = useState(false)
  const [unifyOpen, setUnifyOpen] = useState(false)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const connected = useApp((s) => s.connected)
  const pushToast = useApp((s) => s.pushToast)

  const exportConfig = async () => {
    if (!connected) return
    try {
      const path = await pickPath('save', '策略配置.json')
      if (path) await invoke('update.config.export', { path })
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '导出失败', 'error')
    }
  }

  const importConfig = async () => {
    if (!connected) return
    try {
      const path = await pickPath('file', '')
      if (path) await invoke('update.config.import', { path })
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '导入失败', 'error')
    }
  }

  const unify = (value: string) => {
    setUnifyOpen(false)
    setDraft((prev) => {
      const next = { ...prev }
      for (const r of enabled) next[r.name] = value
      return next
    })
  }

  const signature = data.map((r) => `${r.name}:${r.current}`).join('|')
  useEffect(() => {
    setDraft({})
  }, [signature])

  const enabled = useMemo(() => data.filter((r) => r.checked && (r.changed ?? 0) > 0), [data])
  const unifyOptions = useMemo(() => {
    if (!enabled.length) return ['完全匹配', '替换重名', '跳过重名']
    const first = enabled[0]?.strategies ?? []
    return first.filter((x) => enabled.every((r) => r.strategies.includes(x)))
  }, [enabled])
  const unchanged = useMemo(() => data.filter((r) => r.checked && (r.changed ?? 0) === 0), [data])
  const disabled = useMemo(() => data.filter((r) => !r.checked), [data])

  const toggle = (name: string) => (onToggle ? onToggle(name) : toggleRow(name))
  const pick = (name: string, v: string) => {
    if (onStrategy) { onStrategy(name, v); return }
    setDraft((prev) => ({ ...prev, [name]: v }))
  }

  const dirtyNames = Object.keys(draft).filter((name) => {
    const row = data.find((r) => r.name === name)
    return row ? row.current !== draft[name] : false
  })
  const commit = async () => {
    if (!connected || dirtyNames.length === 0) return
    try {
      await invoke('update.strategies.set', { values: draft })
      setDraft({})
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '保存策略失败', 'error')
    }
  }

  const commitRef = useRef(commit)
  commitRef.current = commit
  useEffect(() => {
    registerCommit?.(() => commitRef.current())
  }, [registerCommit])

  return (
    <div className={`card ${styles.card}`}>
      <div className={styles.head}>
        <span className="t-title">更新策略</span>
        <span className="t-small">悬停策略可看说明；改完点底部主按钮一次提交</span>
        <div className={styles.headBtns}>
          <button className={styles.mini} onClick={() => void importConfig()}>
            导入配置
          </button>
          <button className={styles.mini} onClick={() => void exportConfig()}>
            导出配置
          </button>
        </div>
      </div>

      <div className={`scroll ${styles.rows}`}>
        {enabled.map((r) => (
          <Row key={r.name} row={r} onToggle={toggle} onStrategy={pick} current={draft[r.name]} />
        ))}

        {unchanged.length > 0 && (
          <Section
            label={`无需更新（${unchanged.length}）`}
            open={showUnchanged}
            onToggle={() => setShowUnchanged((v) => !v)}
          >
            {unchanged.map((r) => (
              <Row key={r.name} row={r} onToggle={toggle} onStrategy={pick} current={draft[r.name]} dim />
            ))}
          </Section>
        )}

        {disabled.length > 0 && (
          <Section
            label={`已禁用（${disabled.length}）`}
            open={showDisabled}
            onToggle={() => setShowDisabled((v) => !v)}
          >
            {disabled.map((r) => (
              <Row key={r.name} row={r} onToggle={toggle} onStrategy={undefined} dim />
            ))}
          </Section>
        )}

        {enabled.length === 0 && unchanged.length === 0 && disabled.length === 0 && (
          <div className={styles.empty}>比对完成后这里会列出可更新的目录与文件</div>
        )}
      </div>

      {!compact && (
        <div className={styles.foot}>
          <div style={{ position: 'relative' }}>
            <button
              className="btn"
              style={{ height: 32, padding: '0 16px' }}
              onClick={() => setUnifyOpen((v) => !v)}
              disabled={enabled.length === 0}
            >
              统一策略
            </button>
            <AnimatePresence>
              {unifyOpen && (
                <motion.div
                  initial={{ opacity: 0, y: 6 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: 4 }}
                  transition={{ duration: 0.16, ease: [0.25, 0.8, 0.35, 1] }}
                  className={`card ${styles.unifyMenu}`}
                >
                  {unifyOptions.map((s) => (
                    <button key={s} className={styles.unifyItem} onClick={() => void unify(s)}>
                      {s}
                    </button>
                  ))}
                </motion.div>
              )}
            </AnimatePresence>
          </div>
          <span className={styles.count}>
            {enabled.length} 项参与更新
            {unchanged.length > 0 ? ` · ${unchanged.length} 项无需更新` : ''}
            {disabled.length > 0 ? ` · ${disabled.length} 项已禁用` : ''}
            {dirtyNames.length > 0
              ? ` · ${dirtyNames.length} 项待应用（点底部主按钮一次提交）`
              : ' · 策略已生效'}
          </span>
        </div>
      )}
    </div>
  )
}

function Section({
  label,
  open,
  onToggle,
  children,
}: {
  label: string
  open: boolean
  onToggle: () => void
  children: React.ReactNode
}) {
  return (
    <div className={styles.section}>
      <button className={styles.sectionHead} onClick={onToggle} data-open={open}>
        <motion.span
          className={styles.arrow}
          animate={{ rotate: open ? 90 : 0 }}
          transition={{ type: 'spring', stiffness: 420, damping: 30 }}
        >
          ▶
        </motion.span>
        <span>{label}</span>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ type: 'spring', stiffness: 320, damping: 32 }}
            style={{ overflow: 'hidden' }}
          >
            {children}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

function Row({
  row,
  onToggle,
  onStrategy,
  current,
  dim,
}: {
  row: StrategyRow
  onToggle: (name: string) => void
  onStrategy?: (name: string, value: string) => void
  current?: string
  dim?: boolean
}) {
  return (
    <div className={styles.row} data-dim={dim}>
      <button
        className={styles.nameBtn}
        data-on={row.checked}
        data-locked={row.locked}
        onClick={() => onToggle(row.name)}
        title={row.display}
        disabled={row.locked}
      >
        <span className={styles.nameText}>
          <Icon name={row.isFile ? 'file' : 'folder'} size={13} className={styles.rowIcon} />
          {row.display.replace(/^[^ ]+ /, '')}
        </span>
      </button>
      {onStrategy && <span className={styles.leader} aria-hidden />}
      {onStrategy && (
        <div className={styles.chips}>
          {row.strategies.map((s) => (
            <button
              key={s}
              className="chip"
              title={STRATEGY_HELP[s] ?? s}
              data-on={(current ?? row.current) === s}
              disabled={row.locked}
              onClick={() => onStrategy(row.name, s)}
            >
              {s}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
