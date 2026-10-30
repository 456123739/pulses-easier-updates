import { useMemo, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { usePlayer } from '@/store/player'
import type { StrategyRow } from '@/store/player'
import Icon from './Icon'
import styles from './StrategyCard.module.css'

interface Props {
  rows?: StrategyRow[]
  onToggle?: (name: string) => void
  onStrategy?: (name: string, value: string) => void
  compact?: boolean
}

export default function StrategyCard({ rows, onToggle, onStrategy, compact }: Props) {
  const all = usePlayer((s) => s.strategies)
  const toggleRow = usePlayer((s) => s.toggleRow)
  const setStrategy = usePlayer((s) => s.setStrategy)
  const data = rows ?? all
  const [showDisabled, setShowDisabled] = useState(false)
  const [showUnchanged, setShowUnchanged] = useState(false)

  const enabled = useMemo(() => data.filter((r) => r.checked && (r.changed ?? 0) > 0), [data])
  const unchanged = useMemo(() => data.filter((r) => r.checked && (r.changed ?? 0) === 0), [data])
  const disabled = useMemo(() => data.filter((r) => !r.checked), [data])

  const toggle = (name: string) => (onToggle ? onToggle(name) : toggleRow(name))
  const pick = (name: string, v: string) => (onStrategy ? onStrategy(name, v) : setStrategy(name, v))

  return (
    <div className={`card ${styles.card}`}>
      <div className={styles.head}>
        <span className="t-title">更新策略</span>
        <div className={styles.headBtns}>
          <button className={styles.mini}>导入配置</button>
          <button className={styles.mini}>导出配置</button>
        </div>
      </div>

      <div className={`scroll ${styles.rows}`}>
        {enabled.map((r) => (
          <Row key={r.name} row={r} onToggle={toggle} onStrategy={pick} />
        ))}

        {unchanged.length > 0 && (
          <Section
            label={`无需更新（${unchanged.length}）`}
            open={showUnchanged}
            onToggle={() => setShowUnchanged((v) => !v)}
          >
            {unchanged.map((r) => (
              <Row key={r.name} row={r} onToggle={toggle} onStrategy={pick} dim />
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
          <button className="btn" style={{ height: 32, padding: '0 16px' }}>
            统一策略
          </button>
          <span className={styles.count}>
            {enabled.length} 项参与更新 · 跳过 {data.filter((r) => r.changed).length ? '' : ''}
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
  dim,
}: {
  row: StrategyRow
  onToggle: (name: string) => void
  onStrategy?: (name: string, value: string) => void
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
      {onStrategy && (
        <div className={styles.chips}>
          {row.strategies.map((s) => (
            <button
              key={s}
              className="chip"
              data-on={row.current === s}
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
