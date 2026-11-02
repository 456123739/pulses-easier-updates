import { invoke } from '@/bridge'
import { useApp } from '@/store/app'
import { usePlayer } from '@/store/player'
import styles from './PauseBanner.module.css'

/**
 * 更新途中检测到外部改动时的暂停条。
 *
 * 只给三个选择：继续（接受覆盖）、重新扫描（放弃本次计划）、取消。
 * 不自动重试、不静默跳过——玩家自己决定。
 */
export default function PauseBanner() {
  const paused = usePlayer((s) => s.paused)
  const connected = useApp((s) => s.connected)
  if (!paused) return null

  const names = paused.files.slice(0, 3).map((p) => p.split('/').pop() ?? p)
  const more = paused.count > names.length ? ` 等 ${paused.count} 个文件` : ''

  const call = async (method: string) => {
    if (!connected) return
    try {
      await invoke(method)
    } catch {
      /* 状态会自己刷新 */
    }
  }

  return (
    <div className={`card ${styles.banner}`} data-testid="pause-banner">
      <span className={styles.bar} />
      <div className={styles.text}>
        <div className={styles.title}>更新已暂停 · 整合包被外部修改</div>
        <div className={styles.sub}>
          {names.join('、')}
          {more} 在更新途中被别的程序改过。继续会以更新包内容为准覆盖它们。
        </div>
      </div>
      <div className={styles.actions}>
        <button
          className="btn btn-primary"
          style={{ height: 30 }}
          onClick={() => call('update.pause.resume')}
        >
          继续更新
        </button>
        <button className="btn" style={{ height: 30 }} onClick={() => call('update.pause.rescan')}>
          重新扫描
        </button>
        <button className="btn btn-ghost" style={{ height: 30 }} onClick={() => call('update.abort')}>
          取消
        </button>
      </div>
    </div>
  )
}
