/**
 * Floating3DLayer — 通用「悬浮 3D 叠层」容器
 *
 * 这一版的关键行为（按 NS 的要求）：
 * 1. 浮层从主条目的策略区**部分重叠**着长出来，鼠标不用横跨半屏；
 * 2. 按住标题区可以自由拖动，跟手、越界有橡皮筋阻尼，松手弹回可视区；
 * 3. 一条贝塞尔连线从主条目连到浮层，拖动时实时跟随，表明从属关系；
 * 4. 指针进出由调用方决定开合，拖拽期间不会因为离开行而收起；
 * 5. 只用 transform / opacity / left-top 做动画，Esc 收起，滚动时重新贴回。
 */
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import styles from './Floating3DLayer.module.css'

type Props = {
  open: boolean
  anchor: HTMLElement | null
  onRequestClose: () => void
  children: React.ReactNode
  width?: number
  /** 往左压进主栏多少像素（部分重叠，缩短鼠标行程） */
  overlap?: number
  label?: string
  /** 给自动测试/调试用的稳定标识（sub / peek） */
  kind?: string
  onPointerEnter?: () => void
  onPointerLeave?: () => void
  /** 拖拽开始/结束（调用方据此暂停"移开即收起"） */
  onDragChange?: (dragging: boolean) => void
}

type Pos = { left: number; top: number; width: number }

const EDGE = 12
const GAP = 14

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value))
}

/** 越界时给 0.3 的阻尼，拖起来有"拉橡皮筋"的手感 */
function elastic(value: number, min: number, max: number): number {
  if (value < min) return min - (min - value) * 0.3
  if (value > max) return max + (value - max) * 0.3
  return value
}

function computePos(anchor: HTMLElement, width: number, height: number, overlap: number): Pos {
  const rect = anchor.getBoundingClientRect()
  const vw = window.innerWidth
  const vh = window.innerHeight
  const w = Math.min(width, vw - EDGE * 2)
  // 左边缘压在行的策略区里：不用把鼠标移到表的右边外面
  let left = rect.right - Math.min(overlap, Math.max(48, rect.width * 0.45))
  // 右边放不下就翻到左边（先判断再夹紧，别先夹到右边界导致永远判定"放得下"）
  if (left + w > vw - EDGE) left = rect.left - w - GAP
  left = clamp(left, EDGE, vw - w - EDGE)
  const h = Math.min(height || 380, vh - EDGE * 2)
  let top = rect.top - 16
  if (top + h > vh - EDGE) top = vh - EDGE - h
  if (top < EDGE) top = EDGE
  return { left, top, width: w }
}

export default function Floating3DLayer({
  open,
  anchor,
  onRequestClose,
  children,
  width = 392,
  overlap = 132,
  label,
  kind,
  onPointerEnter,
  onPointerLeave,
  onDragChange,
}: Props) {
  const reduce = useReducedMotion()
  const hostRef = useRef<HTMLDivElement | null>(null)
  const bodyRef = useRef<HTMLDivElement | null>(null)
  const pathRef = useRef<SVGPathElement | null>(null)
  const startDotRef = useRef<SVGCircleElement | null>(null)
  const endDotRef = useRef<SVGCircleElement | null>(null)

  const [pos, setPos] = useState<Pos | null>(null)
  const [height, setHeight] = useState(0)
  const [moved, setMoved] = useState(false)
  const movedRef = useRef(false)

  const dragRef = useRef<{
    id: number
    sx: number
    sy: number
  } | null>(null)
  // 拖动偏移：弹簧跟随（不是硬贴指针），松手后带阻尼回弹到可视区
  const posRef = useRef<Pos | null>(null)
  const offRef = useRef({ x: 0, y: 0 })
  const targetRef = useRef({ x: 0, y: 0 })
  const velRef = useRef({ x: 0, y: 0 })
  const rafRef = useRef(0)
  const lastTRef = useRef(0)
  const draggingRef = useRef(false)
  const settleRef = useRef(false)

  // 连线：主条目的策略区左端 → 浮层左边缘
  const updateConnector = useCallback((left?: number, top?: number) => {
    const host = hostRef.current
    const anchorEl = anchor
    const path = pathRef.current
    if (!host || !anchorEl || !path) return
    const a = anchorEl.getBoundingClientRect()
    const h = host.getBoundingClientRect()
    const sx = a.left + Math.min(Math.max(a.width * 0.5, 220), 460)
    const sy = a.top + a.height / 2
    const ex = (left ?? h.left) + 2
    const ey = (top ?? h.top) + Math.min(34, h.height / 2)
    const bend = Math.max(36, Math.abs(ex - sx) * 0.45)
    const d = `M ${sx} ${sy} C ${sx + bend} ${sy}, ${ex - bend} ${ey}, ${ex} ${ey}`
    path.setAttribute('d', d)
    startDotRef.current?.setAttribute('cx', String(sx))
    startDotRef.current?.setAttribute('cy', String(sy))
    endDotRef.current?.setAttribute('cx', String(ex))
    endDotRef.current?.setAttribute('cy', String(ey))
  }, [anchor])

  const sync = useCallback(() => {
    if (!anchor || movedRef.current) return
    setPos(computePos(anchor, width, height, overlap))
  }, [anchor, width, height, overlap])

  const bounds = useCallback(() => {
    const host = hostRef.current
    const base = posRef.current
    if (!host || !base) return { minX: 0, maxX: 0, minY: 0, maxY: 0 }
    const w = host.offsetWidth
    const h = host.offsetHeight
    return {
      minX: EDGE - base.left,
      maxX: window.innerWidth - w - EDGE - base.left,
      minY: EDGE - base.top,
      maxY: window.innerHeight - h - EDGE - base.top,
    }
  }, [])

  const applyOffset = useCallback(() => {
    const host = hostRef.current
    if (!host) return
    const { x, y } = offRef.current
    host.style.transform = `translate3d(${x.toFixed(1)}px, ${y.toFixed(1)}px, 0)`
    updateConnector()
  }, [updateConnector])

  const step = useCallback((now: number) => {
    const host = hostRef.current
    if (!host) return
    const dt = Math.min(0.032, Math.max(0.008, (now - lastTRef.current) / 1000 || 0.016))
    lastTRef.current = now
    const k = draggingRef.current ? 560 : 240
    const damp = draggingRef.current ? 44 : 24
    const off = offRef.current
    const vel = velRef.current
    const target = targetRef.current
    for (const axis of ['x', 'y'] as const) {
      vel[axis] += (target[axis] - off[axis]) * k * dt - vel[axis] * damp * dt
      off[axis] += vel[axis] * dt
    }
    applyOffset()
    const settled =
      Math.abs(target.x - off.x) < 0.4 &&
      Math.abs(target.y - off.y) < 0.4 &&
      Math.abs(vel.x) < 4 && Math.abs(vel.y) < 4
    if (settleRef.current && settled && !draggingRef.current) {
      offRef.current = { ...target }
      applyOffset()
      settleRef.current = false
      rafRef.current = 0
      return
    }
    rafRef.current = requestAnimationFrame(step)
  }, [applyOffset])

  const kick = useCallback(() => {
    if (rafRef.current) return
    lastTRef.current = performance.now()
    rafRef.current = requestAnimationFrame(step)
  }, [step])

  useEffect(() => {
    posRef.current = pos
  }, [pos])

  // 每次重新打开都回到"贴着主条目"的默认位置；拖动过就尊重用户的位置
  useEffect(() => {
    if (open) return
    setMoved(false)
    movedRef.current = false
    offRef.current = { x: 0, y: 0 }
    targetRef.current = { x: 0, y: 0 }
    velRef.current = { x: 0, y: 0 }
    settleRef.current = false
    draggingRef.current = false
    if (rafRef.current) {
      cancelAnimationFrame(rafRef.current)
      rafRef.current = 0
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    sync()
    let raf = 0
    const onMove = () => {
      cancelAnimationFrame(raf)
      raf = requestAnimationFrame(() => updateConnector())
    }
    window.addEventListener('resize', onMove)
    window.addEventListener('scroll', onMove, true)
    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('resize', onMove)
      window.removeEventListener('scroll', onMove, true)
    }
  }, [open, sync, updateConnector])

  // 真实高度：内容渲染完再定位，避免"估高"把浮层顶到屏幕上方
  useEffect(() => {
    if (!open) return
    const el = bodyRef.current
    if (!el) return
    const measure = () => setHeight(el.offsetHeight)
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [open])

  useEffect(() => {
    if (!open) return
    const id = requestAnimationFrame(() => updateConnector())
    return () => cancelAnimationFrame(id)
  }, [open, pos, updateConnector])

  // 点浮层外的任意地方：自然收起（不用先移开鼠标）
  useEffect(() => {
    if (!open) return
    const onDown = (e: PointerEvent) => {
      const target = e.target as HTMLElement | null
      const host = hostRef.current
      if (host && target instanceof Node && host.contains(target)) return
      // 触发按钮自己负责开合：否则"点箭头关"会先被这里关掉、再被 click 打开
      if (target?.closest?.('[data-layer-toggle]')) return
      onRequestClose()
    }
    document.addEventListener('pointerdown', onDown, true)
    return () => document.removeEventListener('pointerdown', onDown, true)
  }, [open, onRequestClose])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onRequestClose()
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [open, onRequestClose])

  const endDrag = useCallback(() => {
    const d = dragRef.current
    if (!d) return
    dragRef.current = null
    draggingRef.current = false
    const { minX, maxX, minY, maxY } = bounds()
    targetRef.current = {
      x: clamp(targetRef.current.x, Math.min(minX, maxX), Math.max(minX, maxX)),
      y: clamp(targetRef.current.y, Math.min(minY, maxY), Math.max(minY, maxY)),
    }
    settleRef.current = true
    kick()
    movedRef.current = true
    setMoved(true)
    onDragChange?.(false)
  }, [bounds, kick, onDragChange])

  const onPointerDown = (e: React.PointerEvent) => {
    const target = e.target as HTMLElement
    if (!target.closest('[data-drag-handle]')) return
    if (target.closest('button, a, input, textarea, select')) return
    const host = hostRef.current
    if (!host) return
    e.preventDefault()
    e.currentTarget.setPointerCapture?.(e.pointerId)
    dragRef.current = { id: e.pointerId, sx: e.clientX, sy: e.clientY }
    draggingRef.current = true
    settleRef.current = false
    kick()
    onDragChange?.(true)
  }

  const onPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current
    if (!d || d.id !== e.pointerId) return
    const { minX, maxX, minY, maxY } = bounds()
    targetRef.current = {
      x: elastic(e.clientX - d.sx, minX, maxX),
      y: elastic(e.clientY - d.sy, minY, maxY),
    }
    kick()
  }

  const onPointerUp = (e: React.PointerEvent) => {
    if (!dragRef.current || dragRef.current.id !== e.pointerId) return
    endDrag()
  }

  const motionProps = useMemo(() => {
    if (reduce) {
      return {
        initial: { opacity: 0, filter: 'blur(3px)' },
        animate: { opacity: 1, filter: 'blur(0px)' },
        exit: { opacity: 0, filter: 'blur(2px)', transition: { duration: 0.1 } },
        transition: { duration: 0.14 },
      }
    }
    return {
      initial: { opacity: 0, x: -8, y: 4 },
      animate: { opacity: 1, x: 0, y: 0 },
      exit: { opacity: 0, x: -4, transition: { duration: 0.12 } },
      transition: { duration: 0.16, ease: [0.2, 0.8, 0.2, 1] as const },
    }
  }, [reduce])

  if (typeof document === 'undefined') return null

  return createPortal(
    <>
      <AnimatePresence>
        {open && (
          <motion.svg
            className={styles.connectorSvg}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.16 }}
            aria-hidden
          >
            <motion.path
              ref={pathRef}
              className={styles.connectorPath}
              initial={{ pathLength: 0, opacity: 0 }}
              animate={{ pathLength: 1, opacity: 1 }}
              transition={{ duration: 0.38, ease: [0.22, 1, 0.36, 1] }}
            />
            <circle ref={startDotRef} r={2.4} className={styles.connectorDot} />
            <circle ref={endDotRef} r={3.4} className={styles.connectorDotEnd} />
          </motion.svg>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {open && pos && (
          <div
            ref={hostRef}
            className={styles.host}
            data-variant="tilt"
            data-kind={kind}
            data-reduced={reduce ? 'true' : 'false'}
            data-moved={moved ? 'true' : 'false'}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            style={{
              left: pos.left,
              top: pos.top,
              width: pos.width,
              perspective: reduce ? undefined : 1200,
              perspectiveOrigin: '0px 34px',
            }}
          >
            {!reduce && (
              <span className={`${styles.plate} ${styles.plateNear}`} aria-hidden />
            )}
            <motion.div
              ref={bodyRef}
              className={styles.body}
              data-dragging={dragRef.current ? 'true' : undefined}
              role="group"
              aria-label={label}
              onPointerEnter={onPointerEnter}
              onPointerLeave={onPointerLeave}
              style={{ transformOrigin: '0px 34px', transformStyle: 'preserve-3d' }}
              {...motionProps}
            >
              {children}
            </motion.div>
          </div>
        )}
      </AnimatePresence>
    </>,
    document.body,
  )
}
