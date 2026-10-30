import { useUi } from '@/store/ui'
import styles from './PendingBanner.module.css'

interface Props {
  count: number
  mode: 'wait' | 'complete'
}

export default function PendingBanner({ count, mode }: Props) {
  const openSheet = useUi((s) => s.openSheet)
  const title = mode === 'wait' ? `还差 ${count} 个文件` : `上次更新未完成 · 还差 ${count} 个文件`
  const sub =
    mode === 'wait'
      ? '这些文件所有下载源都失败了：点源名称自己下，再拖进补入窗口'
      : '补齐后本次更新才算完整'

  return (
    <div className={`card ${styles.banner}`}>
      <span className={styles.bar} />
      <div className={styles.text}>
        <div className={styles.title}>⚠ {title}</div>
        <div className={styles.sub}>{sub}</div>
      </div>
      <div className={styles.actions}>
        <button
          className="btn"
          style={{ height: 30 }}
          onClick={() => openSheet('pending')}
        >
          打开补入窗口
        </button>
        <button className="btn btn-ghost" style={{ height: 30 }}>
          {mode === 'wait' ? '放弃…' : '不再提醒'}
        </button>
      </div>
    </div>
  )
}
