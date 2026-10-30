import { motion } from 'framer-motion'
import { invoke } from '@/bridge'
import type { Role } from '@/bridge/protocol'
import { useApp } from '@/store/app'
import { useUi } from '@/store/ui'
import styles from './RoleSwitch.module.css'

const ITEMS: { id: Role; label: string }[] = [
  { id: 'player', label: '玩家' },
  { id: 'developer', label: '开发者' },
]

export default function RoleSwitch() {
  const state = useApp((s) => s.state)
  const connected = useApp((s) => s.connected)
  const setState = useApp((s) => s.setState)
  const role: Role = state?.role ?? 'player'
  const setCovering = useUi((s) => s.setCovering)

  const pick = async (next: Role) => {
    if (!state || next === role) return
    // 换页时先盖一层：内容在遮罩后面换，视觉上是一次"刷新"而不是跳变
    setCovering(true)
    window.setTimeout(() => setCovering(false), 260)
    setState({ ...state, role: next })
    if (connected) {
      try {
        await invoke('app.role.set', { role: next })
      } catch {
        /* 失败由 Python 推回真实状态 */
      }
    }
  }

  return (
    <div className={styles.seg}>
      {ITEMS.map((it) => {
        const active = it.id === role
        return (
          <button
            key={it.id}
            className={styles.item}
            data-active={active}
            onClick={() => void pick(it.id)}
          >
            {active && (
              <motion.span
                layoutId="role-thumb"
                className={styles.thumb}
                transition={{ type: 'spring', stiffness: 480, damping: 36, mass: 0.8 }}
              />
            )}
            <span className={styles.label}>{it.label}</span>
          </button>
        )
      })}
    </div>
  )
}
