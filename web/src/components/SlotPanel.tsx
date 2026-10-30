import { useApp } from '@/store/app'
import styles from './SlotPanel.module.css'

const humanRate = (n: number) => {
  if (!n || n < 1024) return ''
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB/s`
  return `${(n / 1024 / 1024).toFixed(1)} MB/s`
}

/** 槽位 = 正在下载的文件里进度最靠前的几个（后端只推字节数，不推槽位）。 */
function useLiveSlots() {
  const bytes = useApp((s) => s.bytes)
  const rows = Object.entries(bytes)
    .filter(([, v]) => v.state !== 'done' && v.total > 0)
    .map(([key, v]) => ({
      name: key.split('/').pop() ?? key,
      percent: Math.min(1, v.done / Math.max(1, v.total)),
      speed: humanRate(v.speed ?? 0),
    }))
    .sort((a, b) => b.percent - a.percent)
  const filled = rows.slice(0, 8)
  while (filled.length < 4) filled.push({ name: '空闲', percent: 0, speed: '' })
  return filled
}

interface Props {
  onClose: () => void
}

export default function SlotPanel({ onClose }: Props) {
  const slots = useLiveSlots()

  return (
    <div className={`card ${styles.panel}`}>
      <div className={styles.head}>
        <span className={styles.title}>并行槽位</span>
        <button className={styles.close} onClick={onClose}>
          收起
        </button>
      </div>
      <div className={styles.rows}>
        {slots.map((s, i) => (
          <div key={i} className={styles.row} data-idle={s.percent === 0}>
            <span className={styles.name} title={s.name}>
              {s.name}
            </span>
            <span className={styles.bar}>
              <span className={styles.fill} style={{ width: `${s.percent * 100}%` }} />
            </span>
            <span className={styles.speed}>{s.speed}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
