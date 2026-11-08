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
  resume?: { update: string; completed: number; total: number } | null;
  paused?: { stage: string; count: number; files: string[] } | null;
}

export interface DevStrategy {
  name: string;
  current: string;
  excluded: boolean;
  isFile: boolean;
}

/** 「导出内容」勾选项：父项 + 子项（只有父项带 children）。 */
export interface DevRuleNode {
  key: string;
  title: string;
  checked: boolean;
}

export interface DevRuleGroup extends DevRuleNode {
  children: DevRuleNode[];
}

/** 实例扫描结果里的二级条目（顶层目录下的一级子项）。 */
export interface DevInstanceChild {
  path: string;
  files: number;
  bytes: number;
  candidates: number;
}

/** 实例扫描结果里的顶层条目（一个目录或一个根散文件）。 */
export interface DevInstanceRow {
  name: string;
  isFile: boolean;
  files: number;
  bytes: number;
  candidates: number;
  children: DevInstanceChild[];
}

/** 本地整合包目录的扫描摘要（mode = "instance" 时才有）。 */
export interface DevInstance {
  root: string;
  name: string;
  minecraft: string;
  loader: string;
  loaderVersion: string;
  totals: { files: number; bytes: number; candidates: number }
  unsafeLinks?: string[]
  badNames?: string[];
  rows: DevInstanceRow[];
}

export interface DevSpecial {
  key: string;
  title: string;
  desc: string;
  /** 扫到的文件数 / 字节数（只统计，用来判断值不值得勾） */
  files: number;
  bytes: number;
  sample: string[];
  checked: boolean;
}

export interface DevSnapshot {
  /** loading = 已认出是什么，正在读取（扫描/解析在后台线程） */
  phase: 'idle' | 'loading' | 'exporting' | 'done' | 'error';
  /** loading 时的目标；只有 phase=loading 才有值 */
  pending: { kind: 'instance' | 'zip'; path: string; name: string; note?: string } | null;
  /** zip = 导入启动器导出的整合包；instance = 直接读本地实例目录 */
  mode: 'zip' | 'instance';
  instance: DevInstance | null;
  ruleOptions: DevRuleGroup[];
  /** 特殊导出内容：只有扫到了才出现，默认都不勾 */
  specials: DevSpecial[];
  hasCfKey: boolean;
  /** CurseForge Key 从哪来：内置默认 / 用户自己填的 / 被环境变量关掉 */
  cfKeySource: 'user' | 'builtin' | 'off';
  source: { path: string; name: string } | null;
  name: string;
  version: string;
  files: number;
  strategies: DevStrategy[];
  changelog: string;
  options: Record<string, boolean>;
  outPath: string;
  error: string | null;
  progress: { done?: number; total?: number; label?: string };
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
  dev?: DevSnapshot;
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
