/**
 * TitleBar — 无边框窗口的自绘标题栏。
 *
 * 关闭/最小化/最大化走 window.* 桥；空白区域可拖动（用 screenX/screenY
 * 算窗口位移，不依赖 DOM 的 client 坐标，窗口动的时候不会"追着跑"）。
 */
import { useEffect, useRef, useState } from 'react'
import { useApp } from '@/store/app'
import { win } from '@/window'
import styles from './TitleBar.module.css'

export default function TitleBar() {
  const pushToast = useApp((s) => s.pushToast)
  const [maxed, setMaxed] = useState(false)

  useEffect(() => {
    void win<{ maximized?: boolean }>('window.state').then((s) => {
      if (s?.maximized !== undefined) setMaxed(Boolean(s.maximized))
    })
  }, [])
  const drag = useRef<{ sx: number; sy: number; wx: number; wy: number } | null>(null)
  const raf = useRef(0)

  const onDown = (e: React.PointerEvent) => {
    if ((e.target as HTMLElement).closest('[data-no-drag]')) return
    e.preventDefault()
    e.currentTarget.setPointerCapture?.(e.pointerId)
    drag.current = { sx: e.screenX, sy: e.screenY, wx: window.screenX, wy: window.screenY }
  }

  const onMove = (e: React.PointerEvent) => {
    const d = drag.current
    if (!d) return
    const x = Math.round(d.wx + (e.screenX - d.sx))
    const y = Math.round(d.wy + (e.screenY - d.sy))
    cancelAnimationFrame(raf.current)
    raf.current = requestAnimationFrame(() => void win('window.move', { x, y }))
  }

  const onUp = () => {
    drag.current = null
  }

  const minimize = () => void win('window.minimize')
  const toggleMax = async () => {
    const res = await win<{ maximized?: boolean }>('window.toggle_max')
    if (res?.maximized !== undefined) setMaxed(Boolean(res.maximized))
  }
  const close = async () => {
    const res = await win<{ blocked?: boolean }>('window.close')
    if (res?.blocked) {
      pushToast('更新/导出进行中，暂时不能关闭窗口', 'warn')
    }
  }

  return (
    <header
      className={styles.bar}
      onPointerDown={onDown}
      onPointerMove={onMove}
      onPointerUp={onUp}
      onPointerCancel={onUp}
      onDoubleClick={(e) => {
        // 双击按钮不能冒泡成"最大化"（否则双击关闭会先最大化再关）
        if ((e.target as HTMLElement).closest('[data-no-drag]')) return
        void toggleMax()
      }}
    >
      <span className={styles.brand}>
        <span className={styles.dot} />
        Pulses Easier
      </span>
      <span className={styles.spacer} />
      <div
        className={styles.actions}
        data-no-drag="true"
        onPointerDown={(e) => e.stopPropagation()}
        onDoubleClick={(e) => e.stopPropagation()}
      >
        <button className={styles.btn} aria-label="最小化" title="最小化" onClick={minimize}>
          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
            <path d="M2 5.5h6" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
        </button>
        <button className={styles.btn} aria-label={maxed ? '还原' : '最大化'} title={maxed ? '还原' : '最大化'} onClick={toggleMax}>
          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
            <rect x="2.2" y="2.2" width="5.6" height="5.6" rx="1" fill="none" stroke="currentColor" strokeWidth="1.1" />
          </svg>
        </button>
        <button className={`${styles.btn} ${styles.close}`} aria-label="关闭" title="关闭" onClick={close}>
          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
            <path d="M2.6 2.6l4.8 4.8M7.4 2.6L2.6 7.4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
        </button>
      </div>
    </header>
  )
}
