/**
 * SpecialPeek — 「特殊导出选项」的悬浮详情层
 *
 * 同一套 Floating3DLayer：悬停即展开，不挡操作；列出扫到的样例文件，
 * 让人在勾选前就知道这一项到底会带走什么。
 */
import type { DevSpecial } from '@/bridge/protocol'
import styles from './SpecialPeek.module.css'

const fmtBytes = (bytes: number | undefined): string => {
  const value = Number(bytes) || 0
  if (value < 1024) return `${value} B`
  const units = ['KB', 'MB', 'GB']
  let size = value / 1024
  let index = 0
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024
    index += 1
  }
  return `${size.toFixed(size >= 10 ? 1 : 2)} ${units[index] ?? 'GB'}`
}

export default function SpecialPeek({ item }: { item: DevSpecial }) {
  return (
    <div className={styles.card}>
      <header className={styles.head} data-drag-handle="true">
        <span className={styles.title}>{item.title}</span>
        <span className={styles.count}>
          {item.files} 个 · {fmtBytes(item.bytes)}
        </span>
      </header>
      <p className={styles.desc}>{item.desc}</p>
      <div className={styles.samples}>
        {item.sample.slice(0, 8).map((name) => (
          <span key={name} className={styles.sample}>{name}</span>
        ))}
        {item.sample.length > 8 && (
          <span className={styles.more}>…还有 {item.sample.length - 8} 个</span>
        )}
        {item.sample.length === 0 && <span className={styles.more}>没有样例文件</span>}
      </div>
      <footer className={styles.foot}>
        <span>{item.checked ? '已选：会写进更新包' : '默认不带 · 勾选后才进包'}</span>
        <span className={styles.hint}>移开收起</span>
      </footer>
    </div>
  )
}
