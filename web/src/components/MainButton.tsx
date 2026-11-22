import { useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { invoke } from '@/bridge'
import { useApp } from '@/store/app'
import { usePlayer } from '@/store/player'
import styles from './MainButton.module.css'

interface Props {
  onScanToggle?: () => void
  beforeConfirm?: () => Promise<void>
}

/** 主按钮：六态（含下载覆盖态），配色与文案按 Tk 版逐条对齐。 */
export default function MainButton({ onScanToggle, beforeConfirm }: Props) {
  const btn = usePlayer((s) => s.btn)
  const phase = usePlayer((s) => s.phase)
  const setBtn = usePlayer((s) => s.setBtn)
  const setPhase = usePlayer((s) => s.setPhase)
  const setHint = usePlayer((s) => s.setHint)
  const pushLog = usePlayer((s) => s.pushLog)
  const counts = usePlayer((s) => s.counts)
  const totalChanges = (counts?.added ?? 0) + (counts?.modified ?? 0) + (counts?.deleted ?? 0)

  const downloading = phase === 'download'
  const [aborting, setAborting] = useState(false)
  // 点过"取消"后按钮要立刻变成"取消中…"，直到下载态结束（否则像没点上）
  useEffect(() => {
    if (!downloading) setAborting(false)
  }, [downloading])
  const label = downloading
    ? (aborting ? '停止中…' : '停止下载')
    : {
        disabled: '先定位整合包',
        ready: '比对变更',
        confirm: totalChanges > 0 ? `写入更新（${totalChanges} 项）` : '写入更新',
        rescan: '重新比对',
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

  // 下载态的主按钮是「取消下载」，必须可点：以前 busy 把它一并禁用，
  // 玩家在下载中点它没有任何反应（abort 永远发不出去）。
  const disabled = !downloading && (btn === 'disabled' || btn === 'busy')

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
        if (!window.confirm('停止下载？已下载的缓存会保留。')) return
        setAborting(true)
        const ok = await call('update.abort')
        if (!ok) setAborting(false)
        return
      }
      if (btn === 'ready' || btn === 'rescan') {
        setHint('准备比对…')
        await call('pack.scan')
        return
      }
      if (btn === 'resume') {
        setHint('正在继续上次的更新…')
        await call('update.resume')
        return
      }
      if (btn === 'confirm') {
        setHint('正在应用策略并写入…')
        try {
          await beforeConfirm?.()
        } catch (err) {
          pushToast(err instanceof Error ? err.message : '策略提交失败', 'error')
        }
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
      whileTap={disabled ? undefined : { scale: 0.995 }}
    >
      <span className={styles.label}>{label}</span>
      {variant === 'primary' && <span className={styles.sheen} aria-hidden />}
    </motion.button>
  )
}
