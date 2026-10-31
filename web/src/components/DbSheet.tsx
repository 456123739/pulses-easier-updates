import { useEffect, useState } from 'react'
import { invoke, pickPath } from '@/bridge'
import { useApp } from '@/store/app'
import { useUi } from '@/store/ui'
import Sheet from './Sheet'

/** 数据库位置：保存历史、设置、白名单、下载参数都在这里。 */
export default function DbSheet() {
  const open = useUi((s) => s.sheet) === 'db'
  const close = useUi((s) => s.closeSheet)
  const connected = useApp((s) => s.connected)
  const pushToast = useApp((s) => s.pushToast)
  const [path, setPath] = useState('')

  useEffect(() => {
    if (!open || !connected) return
    invoke<{ path: string }>('app.db.get')
      .then((r) => setPath(r?.path ?? ''))
      .catch(() => setPath(''))
  }, [open, connected])

  const choose = async () => {
    if (!connected) return
    try {
      const dir = await pickPath('folder', '')
      if (!dir) return
      const target = `${dir.replace(/[\\/]$/, '')}/pulses-easier.db`
      const res = await invoke<{ path: string }>('app.db.set', { path: target })
      setPath(res?.path ?? target)
      pushToast('数据库位置已更新', 'info')
    } catch (err) {
      pushToast(err instanceof Error ? err.message : '设置失败', 'error')
    }
  }

  return (
    <Sheet
      open={open}
      title="数据库位置"
      subtitle="历史记录、下载设置、白名单都存在这个文件里"
      width={560}
      onClose={close}
      footer={
        <button className="btn btn-primary" onClick={close}>
          完成
        </button>
      }
    >
      <div style={{ display: 'grid', gap: 12, paddingTop: 6 }}>
        <div
          className="selectable"
          style={{
            padding: '12px 14px',
            borderRadius: 10,
            background: 'rgba(255,255,255,0.03)',
            border: '1px solid var(--hairline)',
            wordBreak: 'break-all',
          }}
        >
          {path || '尚未设置（将使用默认位置）'}
        </div>
        <div>
          <button className="btn" onClick={() => void choose()}>
            选择位置…
          </button>
        </div>
        <div className="t-small">
          换位置后旧数据库不会自动迁移；需要保留历史时，先把旧的
          <code> pulses-easier.db </code>复制到新目录。
        </div>
      </div>
    </Sheet>
  )
}
