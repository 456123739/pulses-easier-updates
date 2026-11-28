/**
 * SubScopeLayer — 「更新范围」里某一行的子项浮层（存档 / 资源包 / 光影包）
 *
 * 走 PCL 的动态子项语义：父目录整体仍可排除，但里面每一项还能单独决定
 * "是否导出"和"更新策略"（策略写进 ea_settings.recommended_strategies 的
 * `父目录/子项` 键，玩家端按路径优先级取用）。
 */
import { motion, useReducedMotion } from 'framer-motion'
import type { DevStrategy, DevSubScope } from '@/bridge/protocol'
import Icon from './Icon'
import styles from './SubScopeLayer.module.css'

type Props = {
  row: DevStrategy
  busy?: boolean
  inline?: boolean
  showStrategies?: boolean
  onToggle: (path: string, checked: boolean) => void
  onStrategy: (path: string, strategy: string) => void
  onAll?: (checked: boolean) => void
}

const fmtBytes = (bytes: number | undefined): string => {
  const value = Number(bytes) || 0
  if (value < 1024) return `${value} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let size = value / 1024
  let index = 0
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024
    index += 1
  }
  return `${size.toFixed(size >= 100 ? 0 : size >= 10 ? 1 : 2)} ${units[index] ?? 'TB'}`
}

export default function SubScopeLayer({ row, busy, inline = false, showStrategies = true, onToggle, onStrategy, onAll }: Props) {
  const reduce = useReducedMotion()
  const subs = row.subs ?? []
  const picked = subs.filter((s) => s.checked)
  const pickedBytes = picked.reduce((sum, s) => sum + (s.bytes || 0), 0)
  const totalBytes = subs.reduce((sum, s) => sum + (s.bytes || 0), 0)
  const allOn = subs.length > 0 && picked.length === subs.length

  const item = (sub: DevSubScope, index: number) => {
    const strategy = sub.strategy || ''
    const motionStyle = reduce || inline
      ? {}
      : {
          initial: { opacity: 0, x: -8, y: 4 },
          animate: { opacity: 1, x: 0, y: 0 },
          transition: { delay: 0.03 + index * 0.022, duration: 0.22, ease: [0.22, 1, 0.36, 1] as const },
        }
    return (
      <motion.div
        key={sub.path}
        className={styles.item}
        data-off={!sub.checked}
        data-scope-child={sub.path}
        {...motionStyle}
      >
        <div className={styles.itemTop}>
          <button
            type="button"
            role="switch"
            aria-checked={sub.checked}
            aria-label={`导出 ${sub.name}`}
            className={styles.check}
            data-on={sub.checked}
            disabled={busy}
            onClick={() => onToggle(sub.path, !sub.checked)}
          >
            <span className={styles.dot} />
          </button>
          <Icon name={sub.kind === 'file' ? 'file' : 'folder'} size={13} />
          <span className={styles.name} title={sub.name}>{sub.name}</span>
          <span className={styles.meta}>
            {sub.files} 个 · {fmtBytes(sub.bytes)}
          </span>
        </div>
        {showStrategies && <div className={styles.chips} role="group" aria-label={`${sub.name} 的更新策略`}>
          <button
            type="button"
            className={styles.chip}
            data-on={strategy === ''}
            title={`跟随父目录（${row.current || '完全匹配'}）`}
            disabled={busy}
            onClick={() => onStrategy(sub.path, '')}
          >
            跟随
          </button>
          {sub.strategies.map((s) => (
            <button
              key={s}
              type="button"
              className={styles.chip}
              data-on={strategy === s}
              disabled={busy}
              onClick={() => onStrategy(sub.path, s)}
            >
              {s}
            </button>
          ))}
        </div>}
      </motion.div>
    )
  }

  return (
    <div className={styles.card} data-inline={inline}>
      <header className={styles.head} data-drag-handle={inline ? undefined : 'true'} title={inline ? undefined : '按住这里可以拖动'}>
        <div className={styles.headText}>
          <span className={styles.title}>
            {!inline && <span className={styles.grip} aria-hidden>⠿</span>}
            {row.title || row.name}
          </span>
          <span className={styles.sub}>
            {subs.length} 项 · 已选 {picked.length} 项 · {fmtBytes(totalBytes)}
          </span>
        </div>
        <div className={styles.headTools}>
          <button
            type="button"
            className={styles.allBtn}
            data-on={allOn}
            aria-pressed={allOn}
            disabled={busy || subs.length === 0}
            onClick={() => onAll?.(!allOn)}
          >
            <span className={styles.allBox} aria-hidden />
            全选
          </button>
          {showStrategies && <span className={styles.inherit}>父级：{row.current || '完全匹配'}</span>}
        </div>
      </header>

      <div className={`${styles.list} scroll`}>
        {subs.length === 0 && (
          <div className={styles.empty}>这个目录里还没有可单独设置的内容。</div>
        )}
        {subs.map(item)}
      </div>

      <footer className={styles.foot}>
        <span className={styles.hint}>{inline ? '勾选要导出的子项' : '移开自动收起 · 按住标题拖动 · Esc 关闭'}</span>
        <span className={styles.bytes}>已选 {fmtBytes(pickedBytes)}</span>
      </footer>
    </div>
  )
}
