/**
 * ResizeGrips — 无边框窗口的八条隐形容器边。
 *
 * frameless 会去掉系统边框，所以缩放要自己做：按下时记下窗口几何，
 * 拖动时按 screenX/screenY 的增量算新尺寸，再调 window.resize / window.move。
 */
import { useRef } from 'react'
import { win } from '@/window'
import styles from './ResizeGrips.module.css'

const EDGES = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'] as const
type Edge = (typeof EDGES)[number]
const MIN_W = 900
const MIN_H = 560

type Start = {
  edge: Edge
  sx: number
  sy: number
  wx: number
  wy: number
  w: number
  h: number
}

export default function ResizeGrips() {
  const drag = useRef<Start | null>(null)
  const raf = useRef(0)
  const last = useRef<{ w: number; h: number; wx: number; wy: number } | null>(null)

  const onDown = (edge: Edge) => (e: React.PointerEvent) => {
    e.preventDefault()
    e.stopPropagation()
    e.currentTarget.setPointerCapture?.(e.pointerId)
    drag.current = {
      edge, sx: e.screenX, sy: e.screenY,
      wx: window.screenX, wy: window.screenY,
      w: window.innerWidth, h: window.innerHeight,
    }
    last.current = null
  }

  const onMove = (e: React.PointerEvent) => {
    const d = drag.current
    if (!d) return
    const dx = e.screenX - d.sx
    const dy = e.screenY - d.sy
    // 始终以"按下时"的几何为基准计算；不要把结果写回 d（会累加漂移）
    let { wx, wy, w, h } = d
    if (d.edge.includes('e')) w = Math.max(MIN_W, d.w + dx)
    if (d.edge.includes('s')) h = Math.max(MIN_H, d.h + dy)
    if (d.edge.includes('w')) {
      w = Math.max(MIN_W, d.w - dx)
      wx = d.wx + (d.w - w)
    }
    if (d.edge.includes('n')) {
      h = Math.max(MIN_H, d.h - dy)
      wy = d.wy + (d.h - h)
    }
    if (w === d.w && h === d.h) return
    last.current = { w, h, wx, wy }
    cancelAnimationFrame(raf.current)
    raf.current = requestAnimationFrame(() => {
      const v = last.current
      if (!v) return
      void win('window.resize', { width: v.w, height: v.h })
      if (d.edge.includes('w') || d.edge.includes('n')) {
        void win('window.move', { x: v.wx, y: v.wy })
      }
    })
  }

  const onUp = () => {
    drag.current = null
  }

  return (
    <>
      {EDGES.map((edge) => (
        <span
          key={edge}
          className={styles.grip}
          data-edge={edge}
          onPointerDown={onDown(edge)}
          onPointerMove={onMove}
          onPointerUp={onUp}
          onPointerCancel={onUp}
          aria-hidden
        />
      ))}
    </>
  )
}
