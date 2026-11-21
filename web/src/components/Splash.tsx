import { motion } from 'framer-motion'
import logo from '@/assets/logo.png'
import styles from './Splash.module.css'

/**
 * 启动页：只显示圆角 logo。
 *
 * 桌面版此时窗口还是 logo 大小；状态准备好后由 App 调 window.expand，
 * 窗口自身长到主窗口尺寸 —— 看起来就是 logo 展开成了窗口。
 */
export default function Splash() {
  return (
    <motion.div
      className={styles.wrap}
      initial={{ opacity: 1 }}
      exit={{ opacity: 0, transition: { duration: 0.22, ease: [0.4, 0, 0.2, 1] } }}
    >
      <motion.img
        className={styles.logo}
        src={logo}
        alt="Pulses Easier"
        draggable={false}
        initial={{ opacity: 0, scale: 0.92, filter: 'blur(10px)' }}
        animate={{ opacity: 1, scale: 1, filter: 'blur(0px)' }}
        transition={{ type: 'spring', stiffness: 220, damping: 24, mass: 0.9 }}
      />
    </motion.div>
  )
}
