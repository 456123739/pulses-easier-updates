import { motion } from 'framer-motion'
import { useApp } from '@/store/app'
import Sidebar from './Sidebar'
import PlayerView from './PlayerView'
import DeveloperView from './DeveloperView'
import styles from './Shell.module.css'

/** 主窗口外壳：玻璃侧边栏 + 工作区（玩家 / 开发者两端切换）。 */
export default function Shell() {
  const role = useApp((s) => s.state?.role ?? 'player')

  return (
    <motion.div
      className={styles.shell}
      initial={{ opacity: 0, scale: 0.986 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: 0.19, ease: [0.2, 0.8, 0.2, 1] }}
    >
      <Sidebar />
      <main className={styles.workspace}>
        <motion.div
          key={role}
          className={styles.page}
          initial={{ opacity: 0, y: 8, filter: 'blur(6px)' }}
          animate={{ opacity: 1, y: 0, filter: 'blur(0px)' }}
          transition={{ type: 'spring', stiffness: 300, damping: 32, mass: 0.9 }}
        >
          {role === 'player' ? <PlayerView /> : <DeveloperView />}
        </motion.div>
      </main>
    </motion.div>
  )
}
