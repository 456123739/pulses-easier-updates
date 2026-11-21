import type { ReactNode } from 'react'

/**
 * 更新日志的轻量 Markdown 渲染（标题 / 列表 / 段落 / 引用 / 代码块）。
 *
 * 玩家端的「更新日志」抽屉与开发者端的编辑器预览共用这一份，
 * 保证"我编辑时看到的"和"玩家看到的"是同一套排版。
 */
export default function MarkdownPreview({ text }: { text: string }) {
  const lines = (text || '').split('\n')
  const blocks: ReactNode[] = []
  let inCode = false
  const codeLines: string[] = []

  lines.forEach((line, i) => {
    const t = line.trimEnd()
    if (t.startsWith('```')) {
      if (inCode) {
        blocks.push(
          <pre key={`c${i}`} className="md-code">
            {codeLines.join('\n')}
          </pre>,
        )
        codeLines.length = 0
        inCode = false
      } else {
        inCode = true
      }
      return
    }
    if (inCode) {
      codeLines.push(line)
      return
    }
    if (!t) {
      blocks.push(<div key={`s${i}`} style={{ height: 4 }} />)
      return
    }
    if (t.startsWith('### ')) {
      blocks.push(
        <div key={i} className="t-title" style={{ marginTop: 6 }}>
          {t.slice(4)}
        </div>,
      )
      return
    }
    if (t.startsWith('## ')) {
      blocks.push(
        <div key={i} className="t-title" style={{ fontSize: 16, marginTop: 10 }}>
          {t.slice(3)}
        </div>,
      )
      return
    }
    if (t.startsWith('# ')) {
      blocks.push(
        <div key={i} className="t-title" style={{ fontSize: 18, marginTop: 10 }}>
          {t.slice(2)}
        </div>,
      )
      return
    }
    if (t.startsWith('> ')) {
      blocks.push(
        <div key={i} className="md-quote">
          {t.slice(2)}
        </div>,
      )
      return
    }
    if (/^\s*[-*]\s/.test(t)) {
      blocks.push(
        <div key={i} className="t-body md-li">
          <span className="t-muted">·</span>
          <span>{t.replace(/^\s*[-*]\s/, '')}</span>
        </div>,
      )
      return
    }
    if (/^\s*\d+\.\s/.test(t)) {
      const num = t.match(/^\s*(\d+)\.\s/)?.[1] ?? '1'
      blocks.push(
        <div key={i} className="t-body md-li">
          <span className="t-muted">{num}.</span>
          <span>{t.replace(/^\s*\d+\.\s/, '')}</span>
        </div>,
      )
      return
    }
    blocks.push(
      <div key={i} className="t-body">
        {t}
      </div>,
    )
  })

  if (inCode && codeLines.length) {
    blocks.push(
      <pre key="c-end" className="md-code">
        {codeLines.join('\n')}
      </pre>,
    )
  }

  return (
    <div className="selectable md-preview" style={{ display: 'grid', gap: 6 }}>
      {blocks}
    </div>
  )
}
