import { useEffect, useRef, useState } from 'react'
import { invoke, pickPath } from '@/bridge'
import type { DevStrategy } from '@/bridge/protocol'
import { useApp } from '@/store/app'
import DropZone from './DropZone'
import Icon from './Icon'
import styles from './DeveloperView.module.css'

const OPTIONS = [
  { id: 'include_hashes', label: '写入文件哈希', desc: '更新时可以逐文件校验，强烈建议保留' },
  { id: 'tamper_proof', label: '防篡改签名', desc: '给清单签名，玩家端会拒绝被改过的更新包' },
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

export default function DeveloperView() {
  const dev = useApp((s) => s.state?.dev)
  const connected = useApp((s) => s.connected)
  const pushToast = useApp((s) => s.pushToast)
  const [changelog, setChangelog] = useState('')
  const [outName, setOutName] = useState('')
  const [outPath, setOutPath] = useState('')
  const seq = useRef(0)

  // 后端是事实来源；本地只在打字时短暂领先（防抖动回写）
  useEffect(() => {
    if (dev?.changelog !== undefined && dev.changelog !== changelog) {
      setChangelog(dev.changelog)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dev?.source?.path])

  useEffect(() => {
    if (dev?.source && !outName) {
      setOutName(dev.source.name.replace(/\.(zip|mrpack)$/i, '') + '_update.eapack')
    }
  }, [dev?.source, outName])

  const importZip = async () => {
    if (!connected) return
    try {
      const path = await pickPath('file', '')
      if (path) await invoke('dev.pick', { path })
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '导入失败', 'error')
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
    if (!connected || !dev?.source) return
    const dir = dev.source.path.replace(/[\\/][^\\/]+$/, '')
    const out = outPath || `${dir}/${outName || 'update.eapack'}`
    try {
      await invoke('dev.export', { path: out })
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '导出失败', 'error')
    }
  }

  const exporting = dev?.phase === 'exporting'
  const percent =
    dev?.progress?.total && dev.progress.total > 0
      ? Math.min(1, (dev.progress.done ?? 0) / dev.progress.total)
      : null

  return (
    <div className={styles.page}>
      <section className={styles.left}>
        <DropZone
          step="STEP 1"
          title={dev?.source ? `导出包：${dev.source.name}` : '拖入整合包，开始制作更新包'}
          subtitle={
            dev?.source
              ? `${dev.files} 个文件 · ${dev.version || '未标注版本'}`
              : '支持整合包根目录 / 版本目录'
          }
          hint={dev?.source ? undefined : '或点击选择'}
          height={92}
          onPick={importZip}
        />

        <div className={`card ${styles.table}`}>
          <div className={styles.tableHead}>
            <span className="t-title">更新范围</span>
            <span className="t-small">
              {dev?.strategies.length ?? 0} 项 · 点目录名可排除
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
            {!dev?.source && (
              <div className="t-small" style={{ padding: '18px 4px' }}>
                导入整合包后，这里会列出所有顶层目录与根目录文件。
              </div>
            )}
          </div>
        </div>

        {dev?.source && (
          <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <button
              className="btn btn-ghost"
              style={{ height: 28, fontSize: 11.5 }}
              onClick={() => void invoke('dev.clear')}
              disabled={exporting}
            >
              清空
            </button>
          </div>
        )}

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
            disabled={!dev?.source || exporting}
          >
            {exporting ? '制作中…' : '开始制作更新包'}
          </button>
        </div>

        {(exporting || dev?.phase === 'done' || dev?.phase === 'error') && (
          <div className={`card ${styles.progressCard}`}>
            <div className={styles.progressHead}>
              <span className="t-small">
                {dev?.error
                  ? `失败：${dev.error}`
                  : dev?.phase === 'done'
                    ? `已生成：${dev.outPath}`
                    : dev?.progress?.label || '导出中…'}
              </span>
              <span className="t-small">
                {dev?.progress?.total ? `${dev.progress.done}/${dev.progress.total}` : ''}
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
        <div className={`card ${styles.panel}`}>
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

        <div className={`card ${styles.panel}`}>
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
