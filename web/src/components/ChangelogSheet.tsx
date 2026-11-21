import { useEffect, useState } from 'react'
import { invoke } from '@/bridge'
import { useApp } from '@/store/app'
import { useUi } from '@/store/ui'
import MarkdownPreview from './MarkdownPreview'
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
        <MarkdownPreview text={text} />
      ) : (
        <div className="t-small" style={{ padding: '24px 0' }}>
          {hasChangelog ? '正在读取…' : '这个更新包里没有更新日志。'}
        </div>
      )}
    </Sheet>
  )
}
