import { motion } from 'framer-motion'
import logo from '@/assets/logo-mark.png'
import styles from './Splash.module.css'

/**
 * 启动页：窗口本身就是 logo 卡片（240×240，圆角由 Win32 区域裁出来）。
 * 展开前先把 logo 模糊淡出成主界面底色，再让窗口长成主窗口；
 * logo 固定 240×240，不随窗口拉伸，避免"被拉大"的观感。
 */
export default function Splash({ expanding = false }: { expanding?: boolean }) {
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
        animate={expanding
          ? { opacity: 0, filter: 'blur(18px)' }
          : { opacity: 1, filter: 'blur(0px)' }}
        transition={expanding
          ? { duration: 0.18, ease: [0.4, 0, 1, 1] }
          : { duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
      />
    </motion.div>
  )
}
