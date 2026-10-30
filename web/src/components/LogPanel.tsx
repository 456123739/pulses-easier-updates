import { useEffect, useRef } from 'react'
import { usePlayer } from '@/store/player'
import styles from './LogPanel.module.css'

export default function LogPanel() {
  const log = usePlayer((s) => s.log)
  const clearLog = usePlayer((s) => s.clearLog)
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
