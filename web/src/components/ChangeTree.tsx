import { useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { usePlayer } from '@/store/player'
import Icon from './Icon'
import type { ChangeGroup } from '@/store/player'
import styles from './ChangeTree.module.css'

export default function ChangeTree() {
  const groups = usePlayer((s) => s.groups)
  const counts = usePlayer((s) => s.counts)
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const [idleOpen, setIdleOpen] = useState(false)

  const active = groups.filter((g) => g.items.length > 0)
  const idle = groups.filter((g) => g.items.length === 0)
  const skippedAll = active.filter((g) => g.items.every((i) => i.skipped))
  const shown = active.filter((g) => !g.items.every((i) => i.skipped))

  return (
    <div className={`card ${styles.card}`}>
      <div className={styles.head}>
        <span className="t-title">变更内容</span>
        <div className={styles.summary}>
          <span style={{ color: 'var(--c-added)' }}>+{counts.added}</span>
          <span style={{ color: 'var(--c-modified)' }}>~{counts.modified}</span>
          <span style={{ color: 'var(--c-deleted)' }}>-{counts.deleted}</span>
        </div>
      </div>

      <div className={`scroll ${styles.body}`}>
        {shown.map((g) => (
          <Group
            key={g.top}
            group={g}
            open={!collapsed[g.top]}
            onToggle={() => setCollapsed((c) => ({ ...c, [g.top]: !c[g.top] }))}
          />
        ))}

        {(skippedAll.length > 0 || idle.length > 0) && (
          <div className={styles.idleWrap}>
            <button className={styles.idleHead} onClick={() => setIdleOpen((v) => !v)}>
              <motion.span
                animate={{ rotate: idleOpen ? 90 : 0 }}
                transition={{ type: 'spring', stiffness: 420, damping: 30 }}
                className={styles.arrow}
              >
                ▶
              </motion.span>
              不更新（{skippedAll.length + idle.length} 组 ·{' '}
              {skippedAll.reduce((n, g) => n + g.items.length, 0)} 项）
            </button>
            <AnimatePresence initial={false}>
              {idleOpen && (
                <motion.div
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: 'auto', opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  transition={{ type: 'spring', stiffness: 320, damping: 32 }}
                  style={{ overflow: 'hidden' }}
                >
                  {skippedAll.map((g) => (
                    <Group key={g.top} group={g} open onToggle={() => {}} dim />
                  ))}
                  {idle.map((g) => (
                    <div key={g.top} className={styles.idleGroup}>
                      {g.top}/
                    </div>
                  ))}
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        )}
      </div>
    </div>
  )
}

function Group({
  group,
  open,
  onToggle,
  dim,
}: {
  group: ChangeGroup
  open: boolean
  onToggle: () => void
  dim?: boolean
}) {
  return (
    <div className={styles.group} data-dim={dim}>
      <button className={styles.groupHead} onClick={onToggle}>
        <motion.span
          className={styles.arrow}
          animate={{ rotate: open ? 90 : 0 }}
          transition={{ type: 'spring', stiffness: 420, damping: 30 }}
        >
          ▶
        </motion.span>
        <span className={styles.groupName}>
          <Icon name="folder" size={13} className={styles.folderIcon} />
          {group.top}/
        </span>
        <span className={styles.groupSum}>
          <span style={{ color: 'var(--c-added)' }}>+{group.counts.added}</span>
          <span style={{ color: 'var(--c-modified)' }}>~{group.counts.modified}</span>
          <span style={{ color: 'var(--c-deleted)' }}>-{group.counts.deleted}</span>
          {group.skipped > 0 && <span className={styles.skipTag}>跳过 {group.skipped}</span>}
          {group.smartExcluded > 0 && <span className={styles.skipTag}>智能剔除</span>}
        </span>
      </button>

      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ type: 'spring', stiffness: 360, damping: 34 }}
            style={{ overflow: 'hidden' }}
          >
            {group.items.map((it) => (
              <div key={it.path} className={styles.item} data-skipped={it.skipped}>
                <span className={styles.mark} data-kind={it.kind}>
                  {it.kind === 'added' ? '+' : it.kind === 'modified' ? '~' : '-'}
                </span>
                <span className={styles.path}>{it.path}</span>
                {it.skipped && <span className={styles.skipped}>已跳过</span>}
              </div>
            ))}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
