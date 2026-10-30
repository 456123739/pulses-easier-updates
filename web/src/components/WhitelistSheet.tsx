import { useEffect, useState } from 'react'
import { invoke } from '@/bridge'
import { useApp } from '@/store/app'
import { useUi } from '@/store/ui'
import Sheet from './Sheet'

/** 白名单：这些顶层目录会被逐文件比对（能删掉新版已移除的文件）。 */
export default function WhitelistSheet() {
  const open = useUi((s) => s.sheet) === 'whitelist'
  const close = useUi((s) => s.closeSheet)
  const connected = useApp((s) => s.connected)
  const pushToast = useApp((s) => s.pushToast)
  const [items, setItems] = useState<string[]>([])
  const [draft, setDraft] = useState('')

  useEffect(() => {
    if (!open || !connected) return
    invoke<{ whitelist: string[] }>('app.whitelist.get')
      .then((r) => setItems(r?.whitelist ?? []))
      .catch(() => setItems([]))
  }, [open, connected])

  const save = async (next: string[]) => {
    setItems(next)
    if (!connected) return
    try {
      await invoke('app.whitelist.set', { items: next })
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '保存失败', 'error')
    }
  }

  const add = () => {
    const name = draft.trim().replace(/^\/+|\/+$/g, '')
    if (!name || name.includes('/') || items.includes(name)) return
    setDraft('')
    void save([...items, name])
  }

  return (
    <Sheet
      open={open}
      title="白名单"
      subtitle="列在这里的目录按文件逐个比对，可以删除新版已移除的文件"
      width={520}
      onClose={close}
      footer={
        <button className="btn btn-primary" onClick={close}>
          完成
        </button>
      }
    >
      <div style={{ display: 'grid', gap: 10, paddingTop: 6 }}>
        <div style={{ display: 'flex', gap: 8 }}>
          <input
            className="input"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && add()}
            placeholder="目录名，例如 mods"
            style={{ flex: 1 }}
          />
          <button className="btn" onClick={add}>
            添加
          </button>
        </div>
        <div style={{ display: 'grid', gap: 6 }}>
          {items.length === 0 && <div className="t-small">还没有白名单目录。</div>}
          {items.map((name) => (
            <div
              key={name}
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                padding: '8px 12px',
                borderRadius: 10,
                background: 'rgba(255,255,255,0.03)',
                border: '1px solid var(--hairline)',
              }}
            >
              <span className="t-body">{name}</span>
              <button
                className="btn btn-ghost"
                style={{ height: 26 }}
                onClick={() => void save(items.filter((i) => i !== name))}
              >
                移除
              </button>
            </div>
          ))}
        </div>
      </div>
    </Sheet>
  )
}
