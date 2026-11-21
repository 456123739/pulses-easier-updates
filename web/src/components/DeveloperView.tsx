import { useEffect, useRef, useState } from 'react'
import { invoke, pickPath } from '@/bridge'
import type { AppState, DevSnapshot, DevStrategy } from '@/bridge/protocol'
import { useApp } from '@/store/app'
import { useUi } from '@/store/ui'
import DropZone from './DropZone'
import Floating3DLayer, { type LayerVariant } from './Floating3DLayer'
import Icon from './Icon'
import SpecialPeek from './SpecialPeek'
import SubScopeLayer from './SubScopeLayer'
import styles from './DeveloperView.module.css'

/** 三种悬浮层方案，用 ?layer=tilt|stack|slide 切换，选型后固定默认。 */
const LAYER_VARIANTS: LayerVariant[] = ['tilt', 'stack', 'slide']
const LAYER_VARIANT: LayerVariant = (() => {
  if (typeof location === 'undefined') return 'tilt'
  const raw = new URLSearchParams(location.search).get('layer')
  return (LAYER_VARIANTS as string[]).includes(raw ?? '')
    ? (raw as LayerVariant)
    : 'tilt'
})()

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

export default function DeveloperView() {
  const dev = useApp((s) => s.state?.dev)
  const connected = useApp((s) => s.connected)
  const pushToast = useApp((s) => s.pushToast)
  const patchState = useApp((s) => s.patchState)
  const setState = useApp((s) => s.setState)
  const openSheet = useUi((s) => s.openSheet)
  const [outName, setOutName] = useState('')
  const [outPath, setOutPath] = useState('')
  const [specialBusy, setSpecialBusy] = useState('')
  const [subBusy, setSubBusy] = useState(false)
  // 特殊导出选项的悬浮详情（同一套 3D 叠层）
  const [peek, setPeek] = useState<{ key: string; anchor: HTMLElement } | null>(null)
  const peekOpen = useRef<number | null>(null)
  const peekClose = useRef<number | null>(null)
  // 子项浮层：悬停 160ms 展开，移开 200ms 收起；点了箭头就固定住
  const [layer, setLayer] = useState<{ name: string; anchor: HTMLElement } | null>(null)
  const rowRefs = useRef(new Map<string, HTMLElement>())
  const openTimer = useRef<number | null>(null)
  const closeTimer = useRef<number | null>(null)
  const draggingRef = useRef(false)

  const clearLayerTimers = () => {
    if (openTimer.current !== null) {
      window.clearTimeout(openTimer.current)
      openTimer.current = null
    }
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current)
      closeTimer.current = null
    }
  }

  const scheduleOpen = (row: DevStrategy, anchor: HTMLElement) => {
    if (!row.subs?.length) return
    clearLayerTimers()
    openTimer.current = window.setTimeout(() => {
      setLayer({ name: row.name, anchor })
    }, 160)
  }

  const scheduleClose = () => {
    clearLayerTimers()
    if (draggingRef.current) return
    closeTimer.current = window.setTimeout(() => setLayer(null), 200)
  }

  const keepLayer = () => {
    clearLayerTimers()
  }

  const toggleLayer = (row: DevStrategy) => {
    clearLayerTimers()
    if (layer?.name === row.name) {
      setLayer(null)
      return
    }
    const anchor = rowRefs.current.get(row.name)
    if (anchor) setLayer({ name: row.name, anchor })
  }

  const clearPeek = () => {
    if (peekOpen.current !== null) window.clearTimeout(peekOpen.current)
    if (peekClose.current !== null) window.clearTimeout(peekClose.current)
    peekOpen.current = null
    peekClose.current = null
  }

  const hoverPeek = (key: string, anchor: HTMLElement) => {
    clearPeek()
    peekOpen.current = window.setTimeout(() => setPeek({ key, anchor }), 170)
  }

  const leavePeek = () => {
    clearPeek()
    peekClose.current = window.setTimeout(() => setPeek(null), 180)
  }

  const setSub = async (path: string, checked?: boolean, strategy?: string) => {
    if (!connected || subBusy) return
    setSubBusy(true)
    try {
      const snap = await invoke<DevSnapshot>('dev.sub.set', { path, checked, strategy })
      if (snap) patchState({ dev: snap })
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '保存失败', 'error')
    } finally {
      setSubBusy(false)
    }
  }

  const setSubAll = async (folder: string, checked: boolean) => {
    if (!connected || subBusy) return
    setSubBusy(true)
    try {
      const snap = await invoke<DevSnapshot>('dev.sub.all', { folder, checked })
      if (snap) patchState({ dev: snap })
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '保存失败', 'error')
    } finally {
      setSubBusy(false)
    }
  }

  const instance = dev?.instance ?? null
  const specials = dev?.specials ?? []
  const exporting = dev?.phase === 'exporting'
  const loading = dev?.phase === 'loading'
  const pending = dev?.pending ?? null
  const mode = instance ? 'instance' : 'zip'
  const rootPath = instance?.root ?? dev?.source?.path ?? ''

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

  // 特殊导出内容：勾一个就重扫一次（后端 dev.rule.set），扫描期间禁用这一行
  const setSpecial = async (key: string, value: boolean) => {
    if (!connected || specialBusy) return
    setSpecialBusy(key)
    try {
      const snap = await invoke<DevSnapshot>('dev.rule.set', { key, value })
      if (snap) patchState({ dev: snap })
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '保存失败', 'error')
    } finally {
      setSpecialBusy('')
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

  const progressTotal =
    dev?.progress?.total || instance?.totals.files || 0
  const progressDone = dev?.progress?.done ?? 0
  const percent = progressTotal > 0 ? Math.min(1, progressDone / progressTotal) : null
  const loaderLabel = instance
    ? `${LOADERS[instance.loader] ?? instance.loader ?? '原版'}${
        instance.loaderVersion ? ` ${instance.loaderVersion}` : ''
      }`
    : ''
  const hasSource = Boolean(dev?.source || instance)

  // 更新日志按钮上的摘要：第一行有效内容 + 字数
  const changelogText = (dev?.changelog ?? '').trim()
  const changelogSummary = changelogText
    ? `${changelogText.split('\n').find((line) => line.trim() && !line.trim().startsWith('#'))?.replace(/^[-*]\s*/, '') ?? '已写内容'} · ${changelogText.replace(/\s/g, '').length} 字`
    : '还没写 · 点开编写（所见即所得）'

  const peekItem = peek
    ? (dev?.specials ?? []).find((item) => item.key === peek.key) ?? null
    : null

  const activeRow = layer
    ? (dev?.strategies ?? []).find((row) => row.name === layer.name) ?? null
    : null

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
              <div
                key={row.name}
                ref={(el) => {
                  if (el) rowRefs.current.set(row.name, el)
                  else rowRefs.current.delete(row.name)
                }}
                className={styles.row}
                data-off={row.excluded}
                data-layer={layer?.name === row.name ? 'true' : undefined}
                onPointerEnter={(e) => scheduleOpen(row, e.currentTarget)}
                onPointerLeave={scheduleClose}
              >
                <button className={styles.rowName} onClick={() => void toggleExcluded(row)}>
                  <Icon name={row.isFile ? 'file' : 'folder'} size={13} />
                  <span>{row.name}</span>
                </button>
                {row.subs && row.subs.length > 0 && (
                  <button
                    type="button"
                    className={styles.subBtn}
                    data-layer-toggle="true"
                    data-on={layer?.name === row.name}
                    aria-expanded={layer?.name === row.name}
                    aria-label={`${row.title || row.name}：${row.subs.length} 项可单独设置`}
                    onClick={(e) => {
                      e.stopPropagation()
                      toggleLayer(row)
                    }}
                  >
                    <span className={styles.subCount}>
                      {row.subs.filter((x) => x.checked).length}/{row.subs.length}
                    </span>
                    <Icon name="chevron" size={11} />
                  </button>
                )}
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
          <Floating3DLayer
            open={Boolean(activeRow && layer)}
            anchor={layer?.anchor ?? null}
            onRequestClose={() => setLayer(null)}
            onPointerEnter={keepLayer}
            onPointerLeave={scheduleClose}
            onDragChange={(value) => {
              draggingRef.current = value
              if (value) clearLayerTimers()
            }}
            variant={LAYER_VARIANT}
            kind="sub"
            label={activeRow ? `${activeRow.title || activeRow.name}的子项` : undefined}
          >
            {activeRow && (
              <SubScopeLayer
                row={activeRow}
                busy={subBusy}
                onToggle={(path, checked) => void setSub(path, checked)}
                onStrategy={(path, strategy) => void setSub(path, undefined, strategy)}
                onAll={(checked) => void setSubAll(activeRow.name, checked)}
              />
            )}
          </Floating3DLayer>
          <Floating3DLayer
            open={Boolean(peekItem)}
            anchor={peek?.anchor ?? null}
            onRequestClose={() => setPeek(null)}
            onPointerEnter={clearPeek}
            onPointerLeave={leavePeek}
            variant={LAYER_VARIANT}
            kind="peek"
            width={304}
            overlap={22}
            label={peekItem ? `${peekItem.title} 的详情` : undefined}
          >
            {peekItem && <SpecialPeek item={peekItem} />}
          </Floating3DLayer>
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
        {specials.length > 0 && (
          <div className={`${styles.surface} ${styles.panel}`}>
            <div className={styles.panelHead}>
              <span className="t-title">特殊导出选项</span>
              <span className="t-small">扫到才出现 · 默认不带</span>
            </div>
            <div className={`${styles.opts} scroll`}>
              {specials.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  className={styles.opt}
                  data-on={item.checked}
                  onPointerEnter={(e) => hoverPeek(item.key, e.currentTarget)}
                  onPointerLeave={leavePeek}
                  onClick={() => void setSpecial(item.key, !item.checked)}
                  disabled={!connected || exporting || Boolean(specialBusy)}
                >
                  <span className={styles.switch} data-on={item.checked}>
                    <span className={styles.knob} />
                  </span>
                  <span className={styles.optText}>
                    <span className={styles.optLabel}>
                      {item.title}
                      <span className={styles.optCount}>
                        {fmtInt(item.files)} 个 · {fmtBytes(item.bytes)}
                      </span>
                    </span>
                    <span className={styles.optDesc}>{item.desc}</span>
                  </span>
                </button>
              ))}
            </div>
          </div>
        )}

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

        <button
          type="button"
          className={`${styles.surface} ${styles.changelogButton}`}
          onClick={() => openSheet('changelog-editor')}
          disabled={!connected || exporting || loading}
          aria-label="编写更新日志"
        >
          <span className={styles.changelogText}>
            <span className={styles.changelogTitle}>更新日志</span>
            <span className={styles.changelogSub}>{changelogSummary}</span>
          </span>
          <span className={styles.changelogGo}>编写 ›</span>
        </button>
      </section>
    </div>
  )
}
