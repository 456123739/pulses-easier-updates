import { useEffect, useRef, useState } from 'react'
import { invoke, pickPath } from '@/bridge'
import type { AppState, DevRuleGroup, DevRuleNode, DevSnapshot, DevStrategy } from '@/bridge/protocol'
import { useApp } from '@/store/app'
import DropZone from './DropZone'
import Icon from './Icon'
import styles from './DeveloperView.module.css'

const OPTIONS = [
  { id: 'include_hashes', label: '写入文件哈希', desc: '更新时可以逐文件校验，强烈建议保留' },
  { id: 'tamper_proof', label: '完整性签名', desc: '逐条记录内容哈希，玩家端会拒绝被改坏的更新包（不证明来源）' },
  { id: 'precompute_overrides', label: '预计算覆盖文件', desc: '打包时就比对好，玩家端比对更快' },
  { id: 'min_size', label: '压缩到最小体积', desc: '忽略排除目录，包更小但更新更慢' },
]

const DIR_STRATEGIES = ['完全匹配', '替换重名', '跳过重名']
const DEFAULT_OPTIONS: Record<string, boolean> = {
  include_hashes: true,
  tamper_proof: true,
  precompute_overrides: true,
  min_size: false,
}

const LOADERS: Record<string, string> = {
  forge: 'Forge',
  fabric: 'Fabric',
  neoforge: 'NeoForge',
  vanilla: '原版',
}

const fmtInt = (n: number | undefined) => (n ?? 0).toLocaleString('zh-CN')

function fmtBytes(bytes: number | undefined): string {
  const value = Number(bytes) || 0
  if (value < 1024) return `${value} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let size = value / 1024
  let index = 0
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024
    index += 1
  }
  const digits = size >= 100 ? 0 : size >= 10 ? 1 : 2
  return `${size.toFixed(digits)} ${units[index] ?? 'TB'}`
}

interface RuleRowProps {
  node: DevRuleNode
  child?: boolean
  count?: number
  pending?: boolean
  disabled?: boolean
  onToggle: (key: string, value: boolean) => void
}

function RuleRow({ node, child = false, count, pending = false, disabled = false, onToggle }: RuleRowProps) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={node.checked}
      className={styles.rule}
      data-child={child}
      data-pending={pending}
      disabled={disabled}
      onClick={() => onToggle(node.key, !node.checked)}
    >
      <span className={styles.check} data-on={node.checked} aria-hidden />
      <span className={styles.ruleTitle}>{node.title}</span>
      {!child && count !== undefined && <span className={styles.ruleCount}>{fmtInt(count)} 个</span>}
    </button>
  )
}

export default function DeveloperView() {
  const dev = useApp((s) => s.state?.dev)
  const connected = useApp((s) => s.connected)
  const pushToast = useApp((s) => s.pushToast)
  const patchState = useApp((s) => s.patchState)
  const setState = useApp((s) => s.setState)
  const [changelog, setChangelog] = useState('')
  const [outName, setOutName] = useState('')
  const [outPath, setOutPath] = useState('')
  const [cfKey, setCfKey] = useState('')
  const [ruleBusy, setRuleBusy] = useState('')
  const seq = useRef(0)

  const instance = dev?.instance ?? null
  const ruleOptions: DevRuleGroup[] = dev?.ruleOptions ?? []
  const rows = instance?.rows ?? []
  const exporting = dev?.phase === 'exporting'
  const loading = dev?.phase === 'loading'
  const pending = dev?.pending ?? null
  const mode = instance ? 'instance' : 'zip'
  const rootPath = instance?.root ?? dev?.source?.path ?? ''

  // 后端是事实来源；本地只在打字时短暂领先（防抖动回写）
  const sourceKey = dev?.source?.path ?? instance?.root ?? ''
  useEffect(() => {
    if (dev?.changelog !== undefined && dev.changelog !== changelog) {
      setChangelog(dev.changelog)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceKey])

  useEffect(() => {
    if (outName) return
    const base = instance?.name ?? dev?.source?.name
    if (!base) return
    setOutName(`${base.replace(/\.(zip|mrpack)$/i, '')}_update.eapack`)
  }, [instance?.name, dev?.source?.name, outName])

  // STEP 1 只有一个动作：把路径交给后端判断（实例目录 / 导出包都行）。
  // 后端只做"认出是什么"就返回，扫描在后台；所以点完不会被卡住。
  const openPath = async (path: string) => {
    if (!connected || !path) return
    try {
      const res = await invoke<{ state: AppState }>('app.pick_pack', { path })
      const next = (res as unknown as { state?: AppState }).state
      if (next) setState(next)
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '打不开这个整合包', 'error')
    }
  }

  const openSource = async () => {
    if (!connected) return
    try {
      const path = await pickPath('file', '')
      if (path) await openPath(path)
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '选择失败', 'error')
    }
  }

  const openDir = async () => {
    if (!connected) return
    try {
      const path = await pickPath('folder', '')
      if (path) await openPath(path)
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '选择失败', 'error')
    }
  }

  const setRule = async (key: string, value: boolean) => {
    if (!connected || ruleBusy) return
    setRuleBusy(key)
    try {
      const snap = await invoke<DevSnapshot>('dev.rule.set', { key, value })
      if (snap) patchState({ dev: snap })
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '保存失败', 'error')
    } finally {
      setRuleBusy('')
    }
  }

  const saveCfKey = async () => {
    if (!connected) return
    const key = cfKey.trim()
    if (!key) return
    try {
      const snap = await invoke<DevSnapshot>('dev.cfkey.set', { key })
      if (snap) patchState({ dev: snap })
      setCfKey('')
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '保存失败', 'error')
    }
  }

  const setOption = async (key: string, value: boolean) => {
    if (!connected) return
    try {
      await invoke('dev.option.set', { key, value })
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '保存失败', 'error')
    }
  }

  const onChangelog = (text: string) => {
    setChangelog(text)
    if (!connected) return
    const mine = ++seq.current
    window.setTimeout(async () => {
      if (mine !== seq.current) return
      try {
        await invoke('dev.changelog.set', { text })
      } catch {
        /* 忽略 */
      }
    }, 500)
  }

  const setStrategy = async (name: string, value: string) => {
    if (!connected) return
    try {
      await invoke('dev.strategy.set', { name, value })
    } catch {
      /* 忽略 */
    }
  }

  const toggleExcluded = async (row: DevStrategy) => {
    if (!connected) return
    try {
      await invoke('dev.strategy.set', { name: row.name, excluded: !row.excluded })
    } catch {
      /* 忽略 */
    }
  }

  const clearSource = async () => {
    if (!connected) return
    try {
      await invoke('dev.clear')
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '清空失败', 'error')
    }
  }

  const chooseOut = async () => {
    if (!connected) return
    try {
      const picked = await pickPath('save', outName || 'update.eapack')
      if (picked) {
        setOutPath(picked)
        setOutName(picked.replace(/^.*[\\/]/, ''))
      }
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '选择失败', 'error')
    }
  }

  const build = async () => {
    if (!connected || !rootPath) return
    const dir = rootPath.replace(/[\\/][^\\/]+$/, '')
    const out = outPath || `${dir}/${outName || 'update.eapack'}`
    try {
      await invoke('dev.export', { path: out })
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '导出失败', 'error')
    }
  }

  // 顶层目录在当前扫描结果里被选中的文件数（按名字对 instance.rows）
  const topFiles = (key: string): number => {
    const want = key.toLowerCase()
    const exact = rows.find((r) => r.name.toLowerCase() === want)
    if (exact) return exact.files
    const prefixed = rows.find((r) => r.name.toLowerCase().startsWith(want))
    return prefixed ? prefixed.files : 0
  }

  const progressTotal =
    dev?.progress?.total || instance?.totals.files || 0
  const progressDone = dev?.progress?.done ?? 0
  const percent = progressTotal > 0 ? Math.min(1, progressDone / progressTotal) : null
  const loaderLabel = instance
    ? `${LOADERS[instance.loader] ?? instance.loader ?? '原版'}${
        instance.loaderVersion ? ` ${instance.loaderVersion}` : ''
      }`
    : ''
  const checkedGroups = ruleOptions.filter((g) => g.checked).length
  const hasSource = Boolean(dev?.source || instance)

  return (
    <div className={styles.page} data-mode={mode}>
      <section className={styles.left}>
        <DropZone
          step="STEP 1"
          title={
            loading
              ? '识别中…'
              : instance
                ? instance.name
                : dev?.source
                  ? `导出包：${dev.source.name}`
                  : '整合包定位'
          }
          subtitle={
            loading
              ? pending?.path ?? ''
              : instance
                ? `本地实例 · ${instance.root}`
                : dev?.source
                  ? `导出包 · ${dev.source.path}`
                  : '整合包目录 / 整合包里的任意文件 / 启动器导出的 .zip'
          }
          hint={loading ? '' : hasSource ? '点击更换' : '拖入 · 或点击选择'}
          height={96}
          locked={exporting || loading}
          ariaLabel="开发者端整合包定位"
          onPick={() => void openSource()}
        />

        <div className={styles.pickRow}>
          <span className="t-small">
            {loading
              ? '正在读取整合包…（不挡界面，读完自动出现）'
              : hasSource
                ? '拖入 / 点击卡片可更换整合包'
                : '本地实例直接选目录，不用先让启动器导出'}
          </span>
          <button
            className="btn btn-ghost"
            onClick={() => void openDir()}
            disabled={!connected || exporting || loading}
          >
            选择目录…
          </button>
        </div>

        {instance && (
          <div className={`${styles.surface} ${styles.panel} ${styles.instance}`}>
            <div className={styles.meta}>
              <div className={styles.metaItem}>
                <span className={styles.metaLabel}>MC 版本</span>
                <span className={styles.metaValue}>{instance.minecraft || '未识别'}</span>
              </div>
              <div className={styles.metaItem}>
                <span className={styles.metaLabel}>加载器</span>
                <span className={styles.metaValue}>{loaderLabel || '未识别'}</span>
              </div>
              <div className={styles.metaItem}>
                <span className={styles.metaLabel}>文件数</span>
                <span className={styles.metaValue}>{fmtInt(instance.totals.files)}</span>
              </div>
              <div className={styles.metaItem}>
                <span className={styles.metaLabel}>总体积</span>
                <span className={styles.metaValue}>{fmtBytes(instance.totals.bytes)}</span>
              </div>
              <div className={styles.metaItem}>
                <span className={styles.metaLabel}>候选资源</span>
                <span className={styles.metaValue}>{fmtInt(instance.totals.candidates)}</span>
              </div>
            </div>
            <p className={styles.note}>
              候选：能查到 CurseForge 或 Modrinth 直链的模组，包里只写 index、不打包本体
              {(instance.unsafeLinks?.length ?? 0) > 0 && (
                <span className={styles.warnLine}>
                  已跳过 {instance.unsafeLinks?.length} 个指向实例外的链接
                </span>
              )}
              {(instance.badNames?.length ?? 0) > 0 && (
                <span className={styles.warnLine}>
                  有 {instance.badNames?.length} 个文件名不是 UTF-8，导出会失败
                </span>
              )}
            </p>
          </div>
        )}

        {instance && (
          <div className={`${styles.surface} ${styles.panel}`}>
            <div className={styles.panelHead}>
              <span className="t-title">导出内容</span>
              <span className="t-small">
                已选 {checkedGroups} / {ruleOptions.length} 组
              </span>
            </div>
            <div className={`${styles.rules} scroll`}>
              {ruleOptions.map((group) => (
                <div key={group.key} className={styles.ruleGroup}>
                  <RuleRow
                    node={group}
                    count={topFiles(group.key)}
                    pending={ruleBusy === group.key}
                    disabled={!connected || exporting || Boolean(ruleBusy)}
                    onToggle={setRule}
                  />
                  {group.children.map((child) => (
                    <RuleRow
                      key={child.key}
                      node={child}
                      child
                      pending={ruleBusy === child.key}
                      disabled={!connected || exporting || Boolean(ruleBusy)}
                      onToggle={setRule}
                    />
                  ))}
                </div>
              ))}
              {!ruleOptions.length && (
                <span className="t-small" style={{ padding: '14px 2px' }}>
                  选择目录后，这里会列出可以导出的内容。
                </span>
              )}
            </div>
          </div>
        )}

        <div className={`${styles.surface} ${styles.table}`}>
          <div className={styles.tableHead}>
            <span className="t-title">更新范围</span>
            <span className={styles.tableTools}>
              <span className="t-small">
                {dev?.strategies.length ?? 0} 项 · 点目录名可排除 ·
                「完全匹配」以包为准（包里没有的文件会被删除）
              </span>
              {hasSource && (
                <button
                  className={`btn btn-ghost ${styles.clearBtn}`}
                  onClick={() => void clearSource()}
                  disabled={exporting}
                >
                  清空
                </button>
              )}
            </span>
          </div>
          <div className={`${styles.rows} scroll`}>
            {(dev?.strategies ?? []).map((row) => (
              <div key={row.name} className={styles.row} data-off={row.excluded}>
                <button className={styles.rowName} onClick={() => void toggleExcluded(row)}>
                  <Icon name={row.isFile ? 'file' : 'folder'} size={13} />
                  <span>{row.name}</span>
                </button>
                <div className={styles.chips}>
                  {DIR_STRATEGIES.map((s) => (
                    <button
                      key={s}
                      className="chip"
                      data-on={row.current === s}
                      disabled={row.excluded || !connected}
                      onClick={() => void setStrategy(row.name, s)}
                    >
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            ))}
            {!hasSource && (
              <div className="t-small" style={{ padding: '18px 4px' }}>
                定好整合包后，这里会列出所有顶层目录与根目录文件。
              </div>
            )}
          </div>
        </div>

        <div className={styles.actions}>
          <input
            className="input"
            value={outName}
            onChange={(e) => setOutName(e.target.value)}
            placeholder="输出文件名"
            style={{ flex: 1, minWidth: 0 }}
          />
          <button className="btn" style={{ height: 38 }} onClick={() => void chooseOut()}>
            位置…
          </button>
          <button
            className="btn btn-primary"
            style={{ height: 38, padding: '0 20px' }}
            onClick={() => void build()}
            disabled={!rootPath || exporting || loading}
          >
            {exporting ? '制作中…' : '开始制作更新包'}
          </button>
        </div>

        {(exporting || dev?.phase === 'done' || dev?.phase === 'error') && (
          <div className={`${styles.surface} ${styles.progressCard}`}>
            <div className={styles.progressHead}>
              <span className="t-small">
                {dev?.error
                  ? `失败：${dev.error}`
                  : dev?.phase === 'done'
                    ? `已生成：${dev.outPath}`
                    : dev?.progress?.label || '导出中…'}
              </span>
              {exporting && (
                <button
                  type="button"
                  className={styles.cancel}
                  onClick={() => void invoke('dev.abort')}
                >
                  取消
                </button>
              )}
              <span className="t-small">
                {progressTotal ? `${progressDone}/${progressTotal}` : ''}
              </span>
            </div>
            <div className={styles.track}>
              <span
                className={styles.fill}
                style={{ width: `${Math.round((percent ?? 0) * 100)}%` }}
              />
            </div>
          </div>
        )}
      </section>

      <section className={styles.right}>
        {instance && (
          <div className={`${styles.surface} ${styles.panel}`}>
            <div className={styles.panelHead}>
              <span className="t-title">CurseForge API Key</span>
              {dev?.hasCfKey && <span className={styles.saved}>已保存</span>}
            </div>
            <div className={styles.keyRow}>
              <input
                className="input"
                value={cfKey}
                onChange={(e) => setCfKey(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void saveCfKey()
                }}
                placeholder={dev?.hasCfKey ? '已保存，重新填写可替换' : '粘贴 API Key'}
                spellCheck={false}
                autoComplete="off"
                style={{ flex: 1, minWidth: 0 }}
              />
              <button
                className="btn"
                onClick={() => void saveCfKey()}
                disabled={!connected || !cfKey.trim()}
              >
                保存
              </button>
            </div>
            <p className={styles.note}>仅保存在本机，用于查模组直链；不填则只走 Modrinth</p>
          </div>
        )}

        <div className={`${styles.surface} ${styles.panel} ${styles.changelogPanel}`}>
          <div className={styles.panelHead}>
            <span className="t-title">更新日志</span>
            <span className={styles.count}>{changelog.length} 字</span>
          </div>
          <textarea
            className={`input selectable ${styles.editor}`}
            value={changelog}
            onChange={(e) => onChangelog(e.target.value)}
            placeholder="写点给玩家看的内容：新增了什么、修了什么、要注意什么。支持 Markdown。"
            spellCheck={false}
          />
        </div>

        <div className={`${styles.surface} ${styles.panel}`}>
          <div className={styles.panelHead}>
            <span className="t-title">导出选项</span>
          </div>
          <div className={styles.opts}>
            {OPTIONS.map((o) => {
              const on = Boolean((dev?.options ?? DEFAULT_OPTIONS)[o.id])
              return (
                <button
                  key={o.id}
                  className={styles.opt}
                  data-on={on}
                  onClick={() => void setOption(o.id, !on)}
                  disabled={!connected}
                >
                  <span className={styles.switch} data-on={on}>
                    <span className={styles.knob} />
                  </span>
                  <span className={styles.optText}>
                    <span className={styles.optLabel}>{o.label}</span>
                    <span className={styles.optDesc}>{o.desc}</span>
                  </span>
                </button>
              )
            })}
          </div>
        </div>
      </section>
    </div>
  )
}
