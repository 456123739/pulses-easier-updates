import { useEffect, type ReactNode } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import styles from './Sheet.module.css'

interface Props {
  open: boolean
  title?: string
  subtitle?: string
  width?: number
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
}

/** 通用玻璃抽屉/对话框：动态模糊遮罩 + spring 升起，iOS 26 的"sheet"。 */
export default function Sheet({ open, title, subtitle, width = 560, onClose, children, footer }: Props) {
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
          initial={{ opacity: 0, backdropFilter: 'blur(0px)' }}
          animate={{ opacity: 1, backdropFilter: 'blur(2px)' }}
          exit={{ opacity: 0, backdropFilter: 'blur(0px)' }}
          transition={{ duration: 0.24, ease: [0.22, 1, 0.36, 1] }}
          onClick={onClose}
        >
          <motion.div
            className={styles.sheet}
            style={{ width }}
            initial={{ opacity: 0, y: 26, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 14, scale: 0.985 }}
            transition={{ type: 'spring', stiffness: 320, damping: 30, mass: 0.85 }}
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
          >
            {(title || subtitle) && (
              <header className={styles.head}>
                <div>
                  {title && <div className={styles.title}>{title}</div>}
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
