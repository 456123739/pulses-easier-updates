import { motion } from 'framer-motion'
import { useState } from 'react'
import styles from './DropZone.module.css'

interface Props {
  step?: string
  title: string
  subtitle?: string
  hint?: string
  height?: number
  highlight?: boolean
  locked?: boolean
  variant?: 'sidebar' | 'work' | 'pending'
  /** 无障碍名字，同时给端到端一个稳定的锚点（例如「开发者端整合包定位」） */
  ariaLabel?: string
  /** 原生拖放到页面后由 pywebview 传给宿主，用于区分玩家 STEP 1/STEP 2。 */
  dropTarget?: string
  onPick?: () => void
}

/** 拖入区：玻璃卡 + 青色内发光；hover/拖入时发光增强（iOS26 的"受击反馈"）。 */
export default function DropZone({
  step,
  title,
  subtitle,
  hint = '或点击选择',
  height = 112,
  highlight = true,
  locked = false,
  variant = 'work',
  ariaLabel,
  dropTarget,
  onPick,
}: Props) {
  const [hover, setHover] = useState(false)
  const [dragging, setDragging] = useState(false)
  const active = (hover || dragging) && !locked

  return (
    <motion.button
      type="button"
      className={`${styles.zone} ${styles[variant]}`}
      data-active={active}
      data-highlight={highlight && !locked}
      data-locked={locked}
      data-drop-target={dropTarget}
      style={{ minHeight: height }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onDragEnter={() => setDragging(true)}
      onDragLeave={() => setDragging(false)}
      onDrop={() => setDragging(false)}
      onClick={onPick}
      aria-label={ariaLabel}
      disabled={locked}
    >
      <span className={styles.glow} aria-hidden />
      <span className={styles.body}>
        {step && <span className={styles.step}>{step}</span>}
        <span className={styles.title}>{title}</span>
        {(subtitle || hint || dragging) && (
          <span className={styles.sub} data-drag={dragging}>
            {dragging ? '松开即可导入' : subtitle}
            {!dragging && subtitle && hint ? ' · ' : ''}
            {!dragging ? hint : ''}
          </span>
        )}
      </span>
    </motion.button>
  )
}
