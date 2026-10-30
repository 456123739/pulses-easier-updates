import { useEffect, useRef } from 'react'
import { useApp } from '@/store/app'
import { usePlayer } from '@/store/player'
import styles from './LogPanel.module.css'

export default function LogPanel() {
  const demo = usePlayer((s) => s.log)
  const clearDemo = usePlayer((s) => s.clearLog)
  const connected = useApp((s) => s.connected)
  const real = useApp((s) => s.logs)

  // 后端推的日志是唯一事实；没连桥（浏览器预览）时才用演示数据
  const log = connected
    ? real.map((l) => ({ id: l.id, level: l.level.toLowerCase(), text: l.message }))
    : demo
  const clearLog = () => {
    clearDemo()
    if (connected) useApp.getState().clearLogs()
  }
  const boxRef = useRef<HTMLDivElement>(null)
  const stickRef = useRef(true)

  useEffect(() => {
    const el = boxRef.current
    if (!el || !stickRef.current) return
    el.scrollTop = el.scrollHeight
  }, [log])

  const onScroll = () => {
    const el = boxRef.current
    if (!el) return
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
  }

  return (
    <div className={`card ${styles.panel}`}>
      <div className={styles.head}>
        <span className={styles.title}>日志</span>
        <button className={styles.clear} onClick={clearLog}>
          清空
        </button>
      </div>
      <div className={`scroll ${styles.box} selectable`} ref={boxRef} onScroll={onScroll}>
        {log.length === 0 && <div className={styles.empty}>暂无日志</div>}
        {log.map((l) => (
          <div key={l.id} className={styles.line} data-level={l.level}>
            {l.text}
          </div>
        ))}
      </div>
    </div>
  )
}
