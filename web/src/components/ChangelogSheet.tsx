import { useEffect, useState } from 'react'
import { invoke } from '@/bridge'
import { useApp } from '@/store/app'
import { useUi } from '@/store/ui'
import Sheet from './Sheet'

/** 更新日志：后端直接给 markdown，这里只做轻量排版（标题/列表/段落）。 */
export default function ChangelogSheet() {
  const open = useUi((s) => s.sheet) === 'changelog'
  const close = useUi((s) => s.closeSheet)
  const connected = useApp((s) => s.connected)
  const hasChangelog = useApp((s) => s.state?.flow?.changelog)
  const [text, setText] = useState('')

  useEffect(() => {
    if (!open) return
    if (!connected) return
    invoke<{ changelog?: string }>('update.changelog')
      .then((r) => setText(r?.changelog ?? ''))
      .catch(() => setText(''))
  }, [open, connected])

  return (
    <Sheet open={open} title="更新日志" width={620} onClose={close}>
      {text ? (
        <div className="selectable" style={{ display: 'grid', gap: 8, paddingTop: 4 }}>
          {text.split('\n').map((line, i) => {
            const t = line.trimEnd()
            if (!t) return <div key={i} style={{ height: 4 }} />
            if (t.startsWith('### '))
              return (
                <div key={i} className="t-title" style={{ marginTop: 6 }}>
                  {t.slice(4)}
                </div>
              )
            if (t.startsWith('## '))
              return (
                <div key={i} className="t-title" style={{ fontSize: 16, marginTop: 10 }}>
                  {t.slice(3)}
                </div>
              )
            if (/^[-*]\s/.test(t))
              return (
                <div key={i} className="t-body" style={{ display: 'flex', gap: 8 }}>
                  <span className="t-muted">·</span>
                  <span>{t.slice(2)}</span>
                </div>
              )
            return (
              <div key={i} className="t-body">
                {t}
              </div>
            )
          })}
        </div>
      ) : (
        <div className="t-small" style={{ padding: '24px 0' }}>
          {hasChangelog ? '正在读取…' : '这个更新包里没有更新日志。'}
        </div>
      )}
    </Sheet>
  )
}
