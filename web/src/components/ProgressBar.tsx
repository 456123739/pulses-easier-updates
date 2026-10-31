import { motion } from 'framer-motion'
import { usePlayer } from '@/store/player'
import styles from './ProgressBar.module.css'

interface Props {
  onClick?: () => void
}

/** 双层进度条：底层整体（青），上层当前文件（黄）；点击展开槽位面板。 */
export default function ProgressBar({ onClick }: Props) {
  const index = usePlayer((s) => s.indexPercent)
  const file = usePlayer((s) => s.filePercent)
  const pct = Math.max(0, Math.min(100, index ?? 0))

  return (
    <button className={styles.wrap} onClick={onClick} title="点击查看并行槽位">
      <span className={styles.track}>
        <motion.span
          className={styles.fill}
          animate={{ width: `${pct}%` }}
          transition={{ type: 'spring', stiffness: 160, damping: 26, mass: 0.6 }}
        />
        {file !== null && file !== undefined && (
          <motion.span
            className={styles.fileFill}
            animate={{ width: `${Math.max(0, Math.min(1, file)) * pct}%` }}
            transition={{ type: 'spring', stiffness: 220, damping: 28 }}
          />
        )}
      </span>
    </button>
  )
}
