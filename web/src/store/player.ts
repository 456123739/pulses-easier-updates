import { create } from 'zustand'
import type { FlowSnapshot } from '@/bridge/protocol'

export type ChangeKind = 'added' | 'modified' | 'deleted'
export type BtnState = 'disabled' | 'ready' | 'confirm' | 'busy' | 'resume' | 'rescan'
export type Phase = 'idle' | 'scan' | 'download' | 'apply' | 'manual'

export interface StrategyRow {
  name: string
  display: string
  isFile: boolean
  strategies: string[]
  current: string
  checked: boolean
  locked?: boolean
  changed?: number // 待更新条目数（0 = 无需更新）
}

export interface ChangeItem {
  path: string
  kind: ChangeKind
  folderLevel?: boolean
  skipped?: boolean
  smartExcluded?: boolean
}

export interface ChangeGroup {
  top: string
  items: ChangeItem[]
  counts: { added: number; modified: number; deleted: number }
  skipped: number
  smartExcluded: number
}

export interface Slot {
  name: string
  percent: number
  speed: string
}

interface PlayerStore {
  phase: Phase
  btn: BtnState
  hint: string
  hintLevel: 'info' | 'warn' | 'error'
  indexPercent: number | null
  filePercent: number | null
  strategies: StrategyRow[]
  groups: ChangeGroup[]
  counts: { added: number; modified: number; deleted: number }
  log: { id: number; level: string; text: string }[]
  slots: Slot[]
  slotsOpen: boolean
  pendingCount: number
  pendingMode: 'wait' | 'complete' | null
  updatePack: { name: string; size: number } | null
  placeholder: string

  setPhase: (p: Phase) => void
  setBtn: (b: BtnState) => void
  setHint: (text: string, level?: 'info' | 'warn' | 'error') => void
  setProgress: (index: number | null, file?: number | null) => void
  toggleRow: (name: string) => void
  setStrategy: (name: string, value: string) => void
  toggleGroup: (top: string) => void
  toggleSlots: () => void
  applyFlow: (flow: FlowSnapshot) => void
  setUpdatePack: (p: { name: string; size: number } | null) => void
  pushLog: (text: string, level?: string) => void
  clearLog: () => void
}

const DIRS: [string, string, number][] = [
  ['mods', '完全匹配', 12],
  ['config', '智能剔除', 4],
  ['resourcepacks', '完全匹配', 0],
  ['shaderpacks', '完全匹配', 0],
  ['saves', '跳过重名', 0],
  ['tacz', '完全匹配', 2],
  ['defaultconfigs', '替换重名', 3],
]

export const DEMO_STRATEGIES: StrategyRow[] = [
  ...DIRS.map(([name, cur, changed]) => ({
    name,
    display: `📁 ${name}`,
    isFile: false,
    strategies:
      name === 'config'
        ? ['智能剔除', '完全匹配', '替换重名', '跳过重名']
        : ['完全匹配', '替换重名', '跳过重名'],
    current: cur,
    checked: true,
    locked: name === 'mods',
    changed,
  })),
  {
    name: 'options.txt',
    display: '📄 options.txt',
    isFile: true,
    strategies: ['完全匹配', '替换重名', '跳过重名', '保留键位'],
    current: '保留键位',
    checked: false,
    changed: 0,
  },
  {
    name: 'servers.dat',
    display: '📄 servers.dat',
    isFile: true,
    strategies: ['完全匹配', '替换重名', '跳过重名', '保留键位'],
    current: '替换重名',
    checked: true,
    changed: 0,
  },
]

export const DEMO_GROUPS: ChangeGroup[] = [
  {
    top: 'mods',
    counts: { added: 2, modified: 4, deleted: 1 },
    skipped: 3,
    smartExcluded: 0,
    items: [
      { path: 'sodium-0.6.5.jar', kind: 'added' },
      { path: 'iris-1.8.1.jar', kind: 'added' },
      { path: 'fabric-api-0.92.2.jar', kind: 'modified' },
      { path: 'lithium-0.14.3.jar', kind: 'modified' },
      { path: 'krypton-0.2.8.jar', kind: 'modified' },
      { path: 'indium-1.0.34.jar', kind: 'modified', skipped: true },
      { path: 'old-render-fix.jar', kind: 'deleted' },
    ],
  },
  {
    top: 'config',
    counts: { added: 1, modified: 1, deleted: 1 },
    skipped: 2,
    smartExcluded: 6,
    items: [
      { path: 'sodium-options.json', kind: 'modified', skipped: true, smartExcluded: true },
      { path: 'iris.properties', kind: 'modified', skipped: true, smartExcluded: true },
      { path: 'xyz/config.json', kind: 'added' },
      { path: 'old.json', kind: 'deleted' },
    ],
  },
  {
    top: 'tacz',
    counts: { added: 0, modified: 0, deleted: 0 },
    skipped: 0,
    smartExcluded: 0,
    items: [],
  },
]

const DEMO_LOG = [
  { id: 1, level: 'success', text: 'Pulses Easier v0.9.2 启动' },
  { id: 2, level: 'info', text: '整合包已就绪：1.20.1-Fabric（14 个模组）' },
  { id: 3, level: 'info', text: '更新包：Pulses-Easier-0.9.2-2026.10.22.zip（122.5 MB）' },
  { id: 4, level: 'info', text: '比对完成：+3 ~5 -2，跳过 5，智能剔除 6' },
  { id: 5, level: 'warn', text: 'indium-1.0.34.jar 命中「跳过重名」，本次不更新' },
]

const DEMO_SLOTS = [
  { name: 'sodium-0.6.5.jar', percent: 0.72, speed: '3.4 MB/s' },
  { name: 'iris-1.8.1.jar', percent: 0.41, speed: '2.1 MB/s' },
  { name: 'fabric-api-0.92.2.jar', percent: 0.18, speed: '1.6 MB/s' },
  { name: '空闲', percent: 0, speed: '' },
]

let logSeq = 0

export const usePlayer = create<PlayerStore>((set, get) => ({
  phase: 'idle',
  btn: 'confirm',
  hint: '比对完成，需下载 3 个文件，点击「确认更新」开始',
  hintLevel: 'info',
  indexPercent: 100,
  filePercent: 0.42,
  strategies: DEMO_STRATEGIES,
  groups: DEMO_GROUPS,
  counts: { added: 3, modified: 5, deleted: 2 },
  log: DEMO_LOG,
  slots: DEMO_SLOTS,
  slotsOpen: false,
  pendingCount: 0,
  pendingMode: null,
  updatePack: { name: 'Pulses-Easier-0.9.2-2026.10.22.zip', size: 128_400_000 },
  placeholder: '请先在侧边栏定位整合包，再拖入更新包',

  setPhase: (phase) => set({ phase }),
  setBtn: (btn) => set({ btn }),
  setHint: (hint, level = 'info') => set({ hint, hintLevel: level }),
  setProgress: (index, file = null) => set({ indexPercent: index, filePercent: file }),
  toggleRow: (name) =>
    set((s) => ({
      strategies: s.strategies.map((r) =>
        r.name === name && !r.locked ? { ...r, checked: !r.checked } : r,
      ),
    })),
  setStrategy: (name, value) =>
    set((s) => ({
      strategies: s.strategies.map((r) => (r.name === name ? { ...r, current: value } : r)),
    })),
  toggleGroup: (top) =>
    set((s) => ({
      groups: s.groups.map((g) =>
        g.top === top
          ? { ...g, items: g.items.map((i) => ({ ...i, skipped: !i.skipped })) }
          : g,
      ),
    })),
  toggleSlots: () => set({ slotsOpen: !get().slotsOpen }),
  setUpdatePack: (updatePack) => set({ updatePack }),
  applyFlow: (flow) =>
    set(() => {
      const strategies: StrategyRow[] = flow.strategies.map((r) => ({
        name: r.name,
        display: r.name,
        isFile: r.isFile,
        strategies: r.strategies,
        current: r.current,
        checked: r.checked,
        // 待更新条数来自后端；0 的行会折叠进「无需更新」
        changed: flow.changeCounts?.[r.name] ?? 0,
      }))
      const groups: ChangeGroup[] = flow.groups.map((g) => ({
        top: g.top,
        counts: g.counts,
        skipped: g.skipped,
        smartExcluded: g.smartExcluded,
        items: g.items.map((i) => ({
          path: i.path,
          kind: i.kind,
          smartExcluded: i.smartExcluded,
          skipped: i.skipped,
        })),
      }))
      const phase: Phase =
        flow.phase === 'scan'
          ? 'scan'
          : flow.phase === 'download'
            ? 'download'
            : flow.phase === 'apply'
              ? 'apply'
              : 'idle'
      const hasDiff = groups.some((g) => g.items.length > 0)
      const finished = flow.phase === 'done'
      const btn: BtnState =
        phase !== 'idle'
          ? 'busy'
          : finished
            ? 'ready'                 // 更新完成：回到「开始更新」，可再扫一次
            : hasDiff
              ? 'confirm'
              : flow.update
                ? 'ready'
                : flow.resume
                  ? 'resume'
                  : 'disabled'
      return {
        strategies,
        groups,
        counts: flow.counts,
        phase,
        btn,
        updatePack: flow.update
          ? { name: flow.update.name, size: flow.update.size ?? 0 }
          : null,
        pendingCount: flow.pending.length,
        pendingMode: flow.pending.length ? 'wait' : null,
      }
    }),
  pushLog: (text, level = 'info') =>
    set((s) => ({ log: [...s.log, { id: ++logSeq, level, text }].slice(-500) })),
  clearLog: () => set({ log: [] }),
}))

