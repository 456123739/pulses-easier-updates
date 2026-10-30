import { AnimatePresence, motion } from 'framer-motion'
import wordmark from '@/assets/wordmark.png'
import { useUi } from '@/store/ui'
import styles from './Cover.module.css'

/** 换页遮罩：切换玩家/开发者时做一次"擦除式"刷新，避免硬切。 */
export default function Cover() {
  const covering = useUi((s) => s.covering)
  return (
    <AnimatePresence>
      {covering && (
        <motion.div
          className={styles.cover}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.14, ease: [0.4, 0, 0.2, 1] }}
        >
          <motion.img
            src={wordmark}
            alt=""
            className={styles.mark}
            initial={{ opacity: 0, scale: 0.96 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ duration: 0.22, ease: [0.25, 0.8, 0.35, 1] }}
          />
        </motion.div>
      )}
    </AnimatePresence>
  )
}
