import { create } from 'zustand'
import type { AppState, DialogSpec, JobInfo, ServerEvent } from '@/bridge/protocol'

export interface LogLine {
  id: number
  time: string
  level: string
  message: string
}

export interface Toast {
  id: number
  message: string
  level: 'info' | 'warn' | 'error'
}

export interface ProgressState {
  phase: string
  message: string
  percent: number | null
  current?: number
  total?: number
}

const LOG_LIMIT = 800

let seq = 0

interface Store {
  /* --- 连接 --- */
  connected: boolean
  booting: boolean
  setConnected: (v: boolean) => void
  setBooting: (v: boolean) => void

  /* --- 全局状态 --- */
  state: AppState | null
  setState: (s: AppState) => void
  patchState: (p: Partial<AppState>) => void

  /* --- 高频事件 --- */
  progress: ProgressState | null
  bytes: Record<string, { done: number; total: number; speed: number; state: string }>
  logs: LogLine[]
  job: JobInfo | null
  toasts: Toast[]
  dialog: DialogSpec | null

  pushLogs: (lines: Omit<LogLine, 'id'>[]) => void
  clearLogs: () => void
  applyEvents: (events: ServerEvent[]) => void
  pushToast: (message: string, level?: Toast['level']) => void
  dismissToast: (id: number) => void
  openDialog: (spec: DialogSpec) => void
  closeDialog: () => void
}

export const useApp = create<Store>((set, get) => ({
  connected: false,
  booting: true,
  setConnected: (v) => set({ connected: v }),
  setBooting: (v) => set({ booting: v }),

  state: null,
  setState: (s) => set({ state: s }),
  patchState: (p) => {
    const cur = get().state
    if (!cur) return
    set({ state: { ...cur, ...p } })
  },

  progress: null,
  bytes: {},
  logs: [],
  job: null,
  toasts: [],
  dialog: null,

  pushLogs: (lines) =>
    set((s) => {
      const next = lines.map((l) => ({ ...l, id: ++seq }))
      const merged = s.logs.concat(next)
      return { logs: merged.length > LOG_LIMIT ? merged.slice(-LOG_LIMIT) : merged }
    }),

  clearLogs: () => set({ logs: [] }),

  pushToast: (message, level = 'info') =>
    set((s) => ({ toasts: s.toasts.concat({ id: ++seq, message, level }).slice(-3) })),

  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),

  openDialog: (spec) => set({ dialog: spec }),
  closeDialog: () => set({ dialog: null }),

  applyEvents: (events) => {
    let state: AppState | null = null
    let progress: ProgressState | null = null
    const byteUpdates: Record<string, { done: number; total: number; speed: number; state: string }> = {}
    const logs: Omit<LogLine, 'id'>[] = []
    let job: JobInfo | null = null
    const toasts: Toast[] = []
    let dialog: DialogSpec | null = null
    let dirtyProgress = false

    for (const ev of events) {
      switch (ev.type) {
        case 'state':
          state = ev.data
          break
        case 'progress':
          progress = ev.data
          dirtyProgress = true
          break
        case 'bytes': {
          const b = ev.data
          byteUpdates[b.key] = {
            done: b.done,
            total: b.total,
            speed: b.speed ?? 0,
            state: b.state ?? 'running',
          }
          break
        }
        case 'log':
          logs.push({
            time: ev.data.time ?? new Date().toLocaleTimeString('zh-CN', { hour12: false }),
            level: ev.data.level || 'INFO',
            message: ev.data.message,
          })
          break
        case 'job':
          job = ev.data
          break
        case 'toast':
          toasts.push({ id: ++seq, message: ev.data.message, level: ev.data.level ?? 'info' })
          break
        case 'dialog':
          dialog = ev.data
          break
        case 'dnd':
          if (ev.data.kind === 'error') {
            toasts.push({ id: ++seq, message: ev.data.message ?? '拖入失败', level: 'error' })
          }
          break
        default:
          break
      }
    }

    const patch: Partial<Store> = {}
    if (state) patch.state = state
    if (dirtyProgress) patch.progress = progress
    if (Object.keys(byteUpdates).length) patch.bytes = { ...get().bytes, ...byteUpdates }
    if (job) patch.job = job
    if (dialog) patch.dialog = dialog
    if (toasts.length) patch.toasts = get().toasts.concat(toasts).slice(-3)
    if (Object.keys(patch).length) set(patch as never)
    if (logs.length) get().pushLogs(logs)
  },
}))

/** 派生：总体进度百分比（0~100），不确定性进度返回 null */
export function overallPercent(): number | null {
  const p = useApp.getState().progress
  if (!p) return null
  if (p.percent !== null && p.percent !== undefined) return p.percent
  if (p.total && p.total > 0 && p.current !== undefined) {
    return Math.min(100, (p.current / p.total) * 100)
  }
  return null
}
