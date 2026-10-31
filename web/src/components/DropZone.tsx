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
      style={{ minHeight: height }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onDragEnter={() => setDragging(true)}
      onDragLeave={() => setDragging(false)}
      onDrop={() => setDragging(false)}
      onClick={onPick}
      disabled={locked}
      whileTap={locked ? undefined : { scale: 0.995 }}
    >
      <span className={styles.glow} aria-hidden />
      <span className={styles.body}>
        {step && <span className={styles.step}>{step}</span>}
        <span className={styles.title}>{title}</span>
        {(subtitle || hint) && (
          <span className={styles.sub}>
            {subtitle}
            {subtitle && hint ? ' · ' : ''}
            {hint}
          </span>
        )}
      </span>
    </motion.button>
  )
}
