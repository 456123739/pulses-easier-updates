import { useEffect, useState } from 'react'
import { invoke } from '@/bridge'
import { useApp } from '@/store/app'
import { useUi } from '@/store/ui'
import Sheet from './Sheet'

interface Settings {
  engine?: string
  mirror_first?: number
  multi_slots?: number
  window_anim?: number
  log_max_lines?: number
  [k: string]: unknown
}

const EMPTY: Settings = {}

const ENGINES = [
  { id: 'ridex', label: 'RideX' },
  { id: 'aster', label: 'Aster' },
]

/** 首选项：只放真正影响行为的几项，其余留在开发者面板。 */
export default function PreferencesSheet() {
  const open = useUi((s) => s.sheet) === 'preferences'
  const close = useUi((s) => s.closeSheet)
  const connected = useApp((s) => s.connected)
  const raw = useApp((s) => s.state?.settings)
  const state = (raw ?? EMPTY) as Settings
  const pushToast = useApp((s) => s.pushToast)
  const [draft, setDraft] = useState<Settings>(state)

  useEffect(() => {
    if (open) setDraft(state)
  }, [open, state])

  const patch = async (next: Settings) => {
    setDraft((d) => ({ ...d, ...next }))
    if (!connected) return
    try {
      await invoke('app.settings.set', { patch: next })
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '保存失败', 'error')
    }
  }

  return (
    <Sheet
      open={open}
      title="首选项"
      subtitle="改动即时保存"
      width={520}
      onClose={close}
      footer={
        <button className="btn btn-primary" onClick={close}>
          完成
        </button>
      }
    >
      <div style={{ display: 'grid', gap: 18, paddingTop: 6 }}>
        <Row label="下载引擎" note="RideX 稳定，Aster 更会抢带宽">
          <div className="seg">
            {ENGINES.map((e) => {
              const active = (draft.engine ?? 'ridex') === e.id
              return (
                <button
                  key={e.id}
                  className="seg-item"
                  data-active={active}
                  onClick={() => void patch({ engine: e.id })}
                  style={active ? { background: 'rgba(255,255,255,0.12)', borderRadius: 999 } : undefined}
                >
                  {e.label}
                </button>
              )
            })}
          </div>
        </Row>

        <Row label="并行下载" note="同时下载的文件数（2–16）">
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <input
              type="range"
              min={2}
              max={16}
              step={1}
              value={Number(draft.multi_slots ?? 8)}
              onChange={(e) => void patch({ multi_slots: Number(e.target.value) })}
              style={{ width: 160 }}
            />
            <span className="t-small" style={{ width: 24, textAlign: 'right' }}>
              {Number(draft.multi_slots ?? 8)}
            </span>
          </div>
        </Row>

        <Row label="优先镜像源" note="国内镜像不可用时自动回落官方 CDN">
          <Switch
            on={Boolean(draft.mirror_first)}
            onChange={(v) => void patch({ mirror_first: v ? 1 : 0 })}
          />
        </Row>

        <Row label="窗口动画" note="关闭后所有过渡立刻到位">
          <Switch
            on={Boolean(draft.window_anim)}
            onChange={(v) => void patch({ window_anim: v ? 1 : 0 })}
          />
        </Row>
      </div>
    </Sheet>
  )
}

function Row({
  label,
  note,
  children,
}: {
  label: string
  note?: string
  children: React.ReactNode
}) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16 }}>
      <div>
        <div className="t-body">{label}</div>
        {note && <div className="t-small" style={{ marginTop: 2 }}>{note}</div>}
      </div>
      {children}
    </div>
  )
}

function Switch({ on, onChange }: { on: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      onClick={() => onChange(!on)}
      role="switch"
      aria-checked={on}
      style={{
        width: 44,
        height: 26,
        borderRadius: 999,
        background: on ? 'var(--c-accent)' : 'rgba(255,255,255,0.1)',
        border: '1px solid var(--hairline)',
        position: 'relative',
        transition: 'background 180ms var(--ease-out)',
      }}
    >
      <span
        style={{
          position: 'absolute',
          top: 2,
          left: on ? 20 : 2,
          width: 20,
          height: 20,
          borderRadius: 999,
          background: on ? '#0b1f1b' : '#e9e9ec',
          transition: 'left 180ms var(--ease-spring)',
        }}
      />
    </button>
  )
}
