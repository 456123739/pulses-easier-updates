import { useEffect } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { useApp } from '@/store/app'
import Sidebar from './Sidebar'
import PlayerView from './PlayerView'
import DeveloperView from './DeveloperView'
import styles from './Shell.module.css'

/** 主窗口外壳：玻璃侧边栏 + 工作区（玩家 / 开发者两端切换）。 */
export default function Shell() {
  const role = useApp((s) => s.state?.role ?? 'player')
  const reduced = useReducedMotion()
  const motionOff = useApp((s) => s.state?.settings.window_anim === 0)
  const resetTransfers = useApp((s) => s.resetTransfers)
  const clearLogs = useApp((s) => s.clearLogs)
  useEffect(() => {
    // 切角色时清掉上一个角色的进度/传输统计，避免导出条留在玩家界面。
    resetTransfers()
    clearLogs()
  }, [role, resetTransfers, clearLogs])

  return (
    <motion.div
      className={styles.shell}
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: reduced || motionOff ? 0 : 0.24, ease: [0.2, 0.8, 0.2, 1] }}
    >
      <Sidebar />
      <main className={styles.workspace}>
        <div key={role} className={styles.page}>
          {role === 'player' ? <PlayerView /> : <DeveloperView />}
        </div>
      </main>
    </motion.div>
  )
}
