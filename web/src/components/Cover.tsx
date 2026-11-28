import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { useApp } from '@/store/app'
import wordmark from '@/assets/wordmark.png'
import { useUi } from '@/store/ui'
import styles from './Cover.module.css'

/** 换页遮罩：完全盖住后才换页；桥接与新页首帧完成后再淡出。 */
export default function Cover() {
  const covering = useUi((s) => s.covering)
  const reduced = useReducedMotion()
  const motionOff = useApp((s) => s.state?.settings.window_anim === 0)
  const duration = reduced || motionOff ? 0 : 0.2
  return (
    <AnimatePresence>
      {covering && (
        <motion.div
          className={styles.cover}
          data-role-cover="true"
          aria-hidden="true"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration, ease: [0.4, 0, 0.2, 1] }}
        >
          <img src={wordmark} alt="" className={styles.mark} />
        </motion.div>
      )}
    </AnimatePresence>
  )
}
