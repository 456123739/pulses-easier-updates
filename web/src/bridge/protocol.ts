/* 桥协议：Python ↔ 前端唯一契约。
   改了这里就必须同步 app/ui_web/api.py 与 docs/UI-重建计划.md 的事件表。 */

export type Role = 'player' | 'developer';

/** Python 推送的事件（EventPump 合批后一次性 push） */
export type ServerEvent =
  | { v: 1; seq: number; type: 'log'; data: { level: string; message: string; time?: string } }
  | { v: 1; seq: number; type: 'progress'; data: Progress }
  | { v: 1; seq: number; type: 'bytes'; data: ByteProgress }
  | { v: 1; seq: number; type: 'state'; data: AppState }
  | { v: 1; seq: number; type: 'job'; jobId: string; data: JobInfo }
  | { v: 1; seq: number; type: 'dnd'; data: { kind: 'ok' | 'error'; message?: string; paths?: string[] } }
  | { v: 1; seq: number; type: 'toast'; data: { message: string; level?: 'info' | 'warn' | 'error' } }
  | { v: 1; seq: number; type: 'dialog'; data: DialogSpec };

export interface Progress {
  phase: string;
  message: string;
  percent: number | null; // null = 不确定进度
  current?: number;
  total?: number;
}

export interface ByteProgress {
  key: string; // 文件相对路径
  done: number;
  total: number;
  speed?: number;
  state?: 'queued' | 'running' | 'slow' | 'done' | 'failed';
}

export interface JobInfo {
  id: string;
  title: string;
  status: 'queued' | 'running' | 'done' | 'error' | 'cancelled';
  cancelable: boolean;
  message?: string;
}

export interface DialogSpec {
  id: string;
  kind: 'alert' | 'confirm' | 'locate' | 'choice';
  title?: string;
  message: string;
  detail?: string;
  buttons?: { id: string; label: string; primary?: boolean }[];
  checkbox?: { id: string; label: string; checked?: boolean };
}

/** 全局状态快照（低频）。字段随功能阶段增长。 */
export interface FlowStrategyRow {
  name: string;
  display: string;
  isFile: boolean;
  strategies: string[];
  current: string;
  checked: boolean;
}

export interface FlowChange {
  path: string;
  kind: 'added' | 'modified' | 'deleted';
  smartExcluded?: boolean;
  skipped?: boolean;
}

export interface FlowGroup {
  top: string;
  items: FlowChange[];
  counts: { added: number; modified: number; deleted: number };
  skipped: number;
  smartExcluded: number;
}

export interface FlowSnapshot {
  phase: 'idle' | 'scan' | 'download' | 'apply' | 'done';
  update: UpdatePackInfo | null;
  strategies: FlowStrategyRow[];
  groups: FlowGroup[];
  counts: { added: number; modified: number; deleted: number };
  changeCounts: Record<string, number>;
  tasks: number;
  done: number;
  pending: { path: string; error?: string }[];
  report: { applied: number; deleted: number; failed: number } | null;
  error: string | null;
  changelog: boolean;
}

export interface AppState {
  version: string;
  role: Role;
  pack: PackInfo | null;
  recent: RecentPack[];
  settings: Record<string, unknown>;
  busy: boolean;
  job: JobInfo | null;
  flow?: FlowSnapshot;
}

export interface PackInfo {
  path: string;
  name: string;
  version?: string;
  loader?: string;
  mcVersion?: string;
  fileCount?: number;
  updatePack?: UpdatePackInfo | null;
}

export interface UpdatePackInfo {
  path: string;
  size?: number;
  kind?: string;
  signature?: string;
  changelog?: boolean;
  name: string;
  version?: string;
  builtAt?: string;
  fileCount?: number;
}

export interface RecentPack {
  name: string;
  path: string;
}
