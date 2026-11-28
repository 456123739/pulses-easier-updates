import { motion, useReducedMotion } from 'framer-motion'
import { useApp } from '@/store/app'
import logo from '@/assets/logo-mark.png'
import styles from './Splash.module.css'

/**
 * 启动页：窗口本身就是 logo 卡片（240×240，圆角由 Win32 区域裁出来）。
 * 展开前先把 logo 淡出成主界面底色，再让窗口长成主窗口；
 * logo 固定 240×240，不随窗口拉伸，避免"被拉大"的观感。
 */
export default function Splash({ expanding = false }: { expanding?: boolean }) {
  const reduced = useReducedMotion()
  const motionOff = useApp((s) => s.state?.settings.window_anim === 0)
  const instant = reduced || motionOff
  return (
    <motion.div
      className={styles.wrap}
      initial={{ opacity: 1 }}
      exit={{ opacity: 0, transition: { duration: instant ? 0 : 0.26, ease: [0.4, 0, 0.2, 1] } }}
    >
      <motion.img
        className={styles.logo}
        src={logo}
        alt="Pulses Easier"
        draggable={false}
        initial={{ opacity: instant ? 1 : 0 }}
        animate={{ opacity: expanding ? 0 : 1 }}
        transition={{ duration: instant ? 0 : (expanding ? 0.22 : 0.32), ease: [0.4, 0, 0.2, 1] }}
      />
    </motion.div>
  )
}
