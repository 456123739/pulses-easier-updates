import { useEffect, useId, type ReactNode } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import styles from './Sheet.module.css'

interface Props {
  open: boolean
  title?: string
  subtitle?: string
  width?: number
  motionStyle?: 'sheet' | 'zoom'
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
}

/** 通用玻璃抽屉/对话框：动态模糊遮罩 + spring 升起，iOS 26 的"sheet"。 */
export default function Sheet({ open, title, subtitle, width = 560, motionStyle = 'sheet', onClose, children, footer }: Props) {
  const reduce = useReducedMotion()
  const titleId = useId()
  const zoom = motionStyle === 'zoom'
  // Esc 关闭：抽屉是"可撤销"的操作，键盘用户也得能退出
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className={styles.mask}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: reduce ? 0 : 0.22, ease: [0.22, 1, 0.36, 1] }}
          onClick={onClose}
        >
          <motion.div
            className={styles.sheet}
            style={{ width }}
            initial={reduce ? { opacity: 1 } : { opacity: 0, y: zoom ? 0 : 26, scale: zoom ? 0.92 : 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={reduce ? { opacity: 0 } : { opacity: 0, y: zoom ? 0 : 14, scale: zoom ? 0.96 : 0.985 }}
            transition={{ duration: reduce ? 0 : zoom ? 0.34 : 0.28, ease: [0.22, 1, 0.36, 1] }}
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-labelledby={title ? titleId : undefined}
          >
            {(title || subtitle) && (
              <header className={styles.head}>
                <div>
                  {title && <div id={titleId} className={styles.title}>{title}</div>}
                  {subtitle && <div className={styles.sub}>{subtitle}</div>}
                </div>
                <button className={styles.close} onClick={onClose} aria-label="关闭">
                  ✕
                </button>
              </header>
            )}
            <div className={`${styles.body} scroll`}>{children}</div>
            {footer && <footer className={styles.foot}>{footer}</footer>}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
