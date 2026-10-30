import { useState } from 'react'
import { motion } from 'framer-motion'
import { usePlayer } from '@/store/player'
import DropZone from './DropZone'
import StrategyCard from './StrategyCard'
import styles from './DeveloperView.module.css'

const OPTIONS = [
  { id: 'include_hashes', label: '写入文件哈希', desc: '更新时可以逐文件校验，强烈建议保留' },
  { id: 'tamper_proof', label: '防篡改签名', desc: '给清单签名，玩家端会拒绝被改过的更新包' },
  { id: 'precompute_overrides', label: '预计算覆盖文件', desc: '打包时就比对好，玩家端比对更快' },
  { id: 'min_size', label: '压缩到最小体积', desc: '忽略排除目录，包更小但更新更慢' },
]

export default function DeveloperView() {
  const [opts, setOpts] = useState<Record<string, boolean>>({
    include_hashes: true,
    tamper_proof: true,
    precompute_overrides: true,
    min_size: false,
  })
  const [changelog, setChangelog] = useState(
    '## 0.9.2\n\n- 扫描阶段边解压边比对，2000 文件从 3.1s 降到 0.5s\n- 下载新增慢速车道，卡死的文件不再拖慢整体\n',
  )
  const setHint = usePlayer((s) => s.setHint)

  return (
    <div className={styles.page}>
      <section className={styles.left}>
        <DropZone
          step="STEP 1"
          title="拖入整合包，开始制作更新包"
          subtitle="支持整合包根目录 / 版本目录"
          height={92}
          onPick={() => setHint('已就绪：选择一个整合包')}
        />
        <StrategyCard compact />
        <div className={styles.actions}>
          <button className="btn">导出配置</button>
          <button className="btn btn-primary" style={{ height: 38, padding: '0 20px' }}>
            开始制作更新包
          </button>
        </div>
      </section>

      <section className={styles.right}>
        <div className={`card ${styles.panel}`}>
          <div className={styles.panelHead}>
            <span className="t-title">更新日志</span>
            <span className={styles.count}>{changelog.length} 字</span>
          </div>
          <textarea
            className={`input selectable ${styles.editor}`}
            value={changelog}
            onChange={(e) => setChangelog(e.target.value)}
            spellCheck={false}
          />
        </div>

        <div className={`card ${styles.panel}`}>
          <div className={styles.panelHead}>
            <span className="t-title">导出选项</span>
          </div>
          <div className={styles.opts}>
            {OPTIONS.map((o) => (
              <button
                key={o.id}
                className={styles.opt}
                data-on={opts[o.id]}
                onClick={() => setOpts((p) => ({ ...p, [o.id]: !p[o.id] }))}
              >
                <motion.span
                  className={styles.switch}
                  data-on={opts[o.id]}
                  layout
                  transition={{ type: 'spring', stiffness: 520, damping: 34 }}
                >
                  <span className={styles.knob} />
                </motion.span>
                <span className={styles.optText}>
                  <span className={styles.optLabel}>{o.label}</span>
                  <span className={styles.optDesc}>{o.desc}</span>
                </span>
              </button>
            ))}
          </div>
        </div>
      </section>
    </div>
  )
}
