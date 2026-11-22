import { motion } from 'framer-motion'
import logo from '@/assets/logo.png'
import styles from './Splash.module.css'

/**
 * 启动页：窗口本身就是 logo 卡片（240×240，圆角由 Win32 区域裁出来）。
 * 状态准备好后 App 调 window.expand，窗口长成主窗口，logo 随窗口一起放大，
 * 最后淡出交给 Shell —— 观感是"logo 本体展开成窗口"。
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
        initial={{ opacity: 0, filter: 'blur(8px)' }}
        animate={{ opacity: 1, filter: 'blur(0px)' }}
        transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
      />
    </motion.div>
  )
}
