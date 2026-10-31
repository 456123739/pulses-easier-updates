import { useState } from 'react'
import { invoke } from '@/bridge'
import { useApp } from '@/store/app'
import { useUi } from '@/store/ui'
import Sheet from './Sheet'

const EMPTY_PENDING: { path: string; error?: string }[] = []

/** 补入窗口：列出所有下载失败的文件，逐个重试或自己下好再拖进来。 */
export default function PendingSheet() {
  const open = useUi((s) => s.sheet) === 'pending'
  const close = useUi((s) => s.closeSheet)
  const connected = useApp((s) => s.connected)
  const raw = useApp((s) => s.state?.flow?.pending)
  // 选择器不能返回新数组：zustand 用 Object.is 比较，每次新引用会打穿渲染
  const pending = raw ?? EMPTY_PENDING
  const pushToast = useApp((s) => s.pushToast)
  const [busy, setBusy] = useState<string | null>(null)

  const retry = async (path: string) => {
    if (!connected) return
    setBusy(path)
    try {
      await invoke('update.retry', { path })
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '重试失败', 'error')
    } finally {
      setBusy(null)
    }
  }

  return (
    <Sheet
      open={open}
      title="补入文件"
      subtitle={`还差 ${pending.length} 个文件，补齐后本次更新才算完整`}
      width={560}
      onClose={close}
      footer={
        <button className="btn" onClick={close}>
          关闭
        </button>
      }
    >
      {pending.length === 0 ? (
        <div className="t-small" style={{ padding: '24px 0' }}>
          没有待补入的文件。
        </div>
      ) : (
        <div style={{ display: 'grid', gap: 8, paddingTop: 6 }}>
          {pending.map((p) => (
            <div
              key={p.path}
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 12,
                padding: '10px 12px',
                borderRadius: 10,
                background: 'rgba(255,255,255,0.03)',
                border: '1px solid var(--hairline)',
              }}
            >
              <div style={{ minWidth: 0 }}>
                <div className="t-body selectable" style={{ overflowWrap: 'anywhere' }}>
                  {p.path}
                </div>
                {p.error && <div className="t-small">{p.error}</div>}
              </div>
              <button
                className="btn"
                disabled={busy === p.path}
                onClick={() => void retry(p.path)}
                style={{ flex: '0 0 auto' }}
              >
                {busy === p.path ? '重试中…' : '重试'}
              </button>
            </div>
          ))}
        </div>
      )}
    </Sheet>
  )
}
