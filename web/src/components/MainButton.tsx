import { motion } from 'framer-motion'
import { invoke } from '@/bridge'
import { useApp } from '@/store/app'
import { usePlayer } from '@/store/player'
import styles from './MainButton.module.css'

interface Props {
  onScanToggle?: () => void
}

/** 主按钮：六态（含下载覆盖态），配色与文案按 Tk 版逐条对齐。 */
export default function MainButton({ onScanToggle }: Props) {
  const btn = usePlayer((s) => s.btn)
  const phase = usePlayer((s) => s.phase)
  const setBtn = usePlayer((s) => s.setBtn)
  const setPhase = usePlayer((s) => s.setPhase)
  const setHint = usePlayer((s) => s.setHint)
  const pushLog = usePlayer((s) => s.pushLog)

  const downloading = phase === 'download'
  const label = downloading
    ? '取消下载'
    : {
        disabled: '开始更新',
        ready: '开始更新',
        confirm: '确认更新',
        rescan: '重新扫描',
        resume: '继续更新',
        busy: '处理中…',
      }[btn]

  const variant = downloading
    ? 'cancel'
    : btn === 'confirm' || btn === 'resume'
      ? 'confirm'
      : btn === 'busy' || btn === 'disabled'
        ? 'muted'
        : btn === 'rescan'
          ? 'ghost'
          : 'primary'

  const disabled = btn === 'disabled' || btn === 'busy' || downloading

  const connected = useApp((s) => s.connected)
  const pushToast = useApp((s) => s.pushToast)
  const flowPhase = useApp((s) => s.state?.flow?.phase)

  const call = async (method: string) => {
    if (!connected) return false
    try {
      await invoke(method)
      return true
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '操作失败', 'error')
      return false
    }
  }

  const onClick = async () => {
    if (connected) {
      if (downloading) {
        await call('update.abort')
        return
      }
      if (btn === 'ready' || btn === 'rescan') {
        setHint('准备比对…')
        await call('pack.scan')
        return
      }
      if (btn === 'confirm' || btn === 'resume') {
        setHint('准备开始…')
        await call('update.apply')
        return
      }
      return
    }
    if (flowPhase && flowPhase !== 'idle') return
    if (btn === 'ready') {
      setPhase('scan')
      setBtn('busy')
      setHint('准备比对…')
      pushLog('开始比对：正在读更新包…')
      return
    }
    if (btn === 'confirm') {
      setPhase('download')
      setBtn('busy')
      setHint('正在下载：sodium-0.6.5.jar  ·  3.4 MB/s  ·  3/12')
      pushLog('开始下载 12 个文件')
      return
    }
    if (btn === 'rescan') {
      onScanToggle?.()
      setBtn('confirm')
      setHint('策略已更新，需下载 3 个文件，点击「确认更新」开始')
      return
    }
    if (btn === 'resume') {
      setPhase('scan')
      setBtn('busy')
      setHint('准备比对…')
    }
  }

  return (
    <motion.button
      className={`${styles.btn} ${styles[variant]}`}
      disabled={disabled}
      onClick={onClick}
      whileTap={disabled ? undefined : { scale: 0.985 }}
      transition={{ type: 'spring', stiffness: 480, damping: 34 }}
    >
      <span className={styles.label}>{label}</span>
      {variant === 'primary' && <span className={styles.sheen} aria-hidden />}
    </motion.button>
  )
}
