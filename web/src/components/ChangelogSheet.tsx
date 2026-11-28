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
  const updatePath = useApp((s) => s.state?.flow?.update?.path)
  const [text, setText] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open || !connected) return
    let cancelled = false
    setText('')
    setError('')
    setLoading(true)
    invoke<{ changelog?: string }>('update.changelog')
      .then((r) => { if (!cancelled) setText(r?.changelog ?? '') })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : '更新日志读取失败')
      })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [open, connected, updatePath])

  return (
    <Sheet
      open={open}
      title="更新日志"
      subtitle="关闭此窗口后，更新会继续进行。"
      width={620}
      motionStyle="zoom"
      onClose={close}
      footer={<button className="btn btn-primary" onClick={close}>关闭</button>}
    >
      {text ? (
        <MarkdownPreview text={text} />
      ) : (
        <div className="t-small" style={{ padding: '24px 0' }} role="status">
          {error || (loading && hasChangelog ? '正在读取…' : '这个更新包里没有更新日志。')}
        </div>
      )}
    </Sheet>
  )
}
