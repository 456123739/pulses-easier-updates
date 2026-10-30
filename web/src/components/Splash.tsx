import { motion } from 'framer-motion'
import logo from '@/assets/logo.png'
import styles from './Splash.module.css'

/** 启动页：哑光卡 + logo + 细进度条（不做流光）。 */
export default function Splash() {
  return (
    <motion.div
      className={styles.wrap}
      initial={{ opacity: 1 }}
      exit={{ opacity: 0, transition: { duration: 0.26, ease: [0.4, 0, 0.2, 1] } }}
    >
      <motion.div
        className={styles.card}
        initial={{ opacity: 0, y: 14, scale: 0.97, filter: 'blur(10px)' }}
        animate={{ opacity: 1, y: 0, scale: 1, filter: 'blur(0px)' }}
        transition={{ type: 'spring', stiffness: 260, damping: 26, mass: 0.9 }}
      >
        <motion.img
          className={styles.logo}
          src={logo}
          alt=""
          draggable={false}
          animate={{ scale: [1, 1.035, 1] }}
          transition={{ duration: 2.6, repeat: Infinity, ease: 'easeInOut' }}
        />
        <div className={styles.title}>Pulses Easier</div>
        <div className={styles.sub}>正在准备运行环境…</div>
        <div className={styles.bar}>
          <motion.div
            className={styles.fill}
            initial={{ x: '-100%' }}
            animate={{ x: '100%' }}
            transition={{ duration: 1.5, repeat: Infinity, ease: 'easeInOut' }}
          />
        </div>
      </motion.div>
    </motion.div>
  )
}
