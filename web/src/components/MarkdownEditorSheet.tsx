import { useEffect, useRef, useState } from 'react'
import { EditorContent, useEditor, type Editor } from '@tiptap/react'
import StarterKit from '@tiptap/starter-kit'
import { Markdown } from 'tiptap-markdown'
import { invoke } from '@/bridge'
import { useApp } from '@/store/app'
import { useUi } from '@/store/ui'
import MarkdownPreview from './MarkdownPreview'
import Sheet from './Sheet'
import styles from './MarkdownEditorSheet.module.css'

/** 扩展只建一次：重建扩展数组会让 Tiptap 整篇重建，光标跳回开头。 */
const EXTENSIONS = [
  // StarterKit 3 已经带 Link，不要再单独注册一份（重复会导致编辑器建不起来）
  StarterKit.configure({
    heading: { levels: [1, 2, 3] },
    link: { openOnClick: false, autolink: true },
  }),
  Markdown.configure({
    html: false,
    tightLists: true,
    linkify: true,
    breaks: false,
    transformPastedText: true,
  }),
]

/** tiptap-markdown 没带类型声明，这里收口成一个小函数。 */
const markdownOf = (editor: Editor): string =>
  (editor.storage as unknown as { markdown: { getMarkdown: () => string } })
    .markdown.getMarkdown()

interface ToolButtonProps {
  label: string
  hint: string
  on?: boolean
  disabled?: boolean
  onClick: () => void
}

function ToolButton({ label, hint, on = false, disabled = false, onClick }: ToolButtonProps) {
  return (
    <button
      type="button"
      className={styles.tool}
      data-on={on}
      title={hint}
      aria-label={hint}
      disabled={disabled}
      onClick={onClick}
    >
      {label}
    </button>
  )
}

/**
 * 更新日志编辑器：独立窗口，所见即所得（Tiptap + Markdown 序列化）。
 *
 * 三个视图：
 *   edit    —— 富文本，写的时候就是成品的样子；
 *   preview —— 玩家端抽屉里的同一套排版（ExportMarkdown 直接喂给 MarkdownPreview）；
 *   source  —— 最终写进 .eapack 的 markdown 原文，可直接改。
 * 内容改动 400ms 防抖回写后端（dev.changelog.set），关窗不丢。
 */
export default function MarkdownEditorSheet() {
  const open = useUi((s) => s.sheet) === 'changelog-editor'
  const close = useUi((s) => s.closeSheet)
  const connected = useApp((s) => s.connected)
  const stored = useApp((s) => s.state?.dev?.changelog ?? '')

  const [view, setView] = useState<'edit' | 'preview' | 'source'>('edit')
  const [text, setText] = useState('')
  const [linkOpen, setLinkOpen] = useState(false)
  const [href, setHref] = useState('https://')
  const seq = useRef(0)
  const loaded = useRef(false)

  const editor = useEditor({
    extensions: EXTENSIONS,
    // 由 effect 里按需灌内容；immediatelyRender=false 走"挂载后创建"，
    // 避免 React 19 下「创建 → 1ms 后判定未挂载 → destroy」留下已销毁实例
    immediatelyRender: false,
    content: '',
    editorProps: { attributes: { class: styles.prose ?? '', spellcheck: 'false' } },
    onUpdate: ({ editor: ed }) => {
      const md = markdownOf(ed)
      setText(md)
      if (!connected) return
      const mine = ++seq.current
      window.setTimeout(() => {
        if (mine !== seq.current) return
        invoke('dev.changelog.set', { text: md }).catch(() => {
          /* 忽略：离线/未连接时不打扰 */
        })
      }, 400)
    },
  })

  // 打开时把后端的旧内容灌进去（只灌一次，避免打字过程中被回写覆盖）
  useEffect(() => {
    if (!open) {
      loaded.current = false
      return
    }
    if (!editor || editor.isDestroyed || loaded.current) return
    loaded.current = true
    setText(stored)
    editor.commands.setContent(stored || '', { emitUpdate: false })
    setView('edit')
  }, [open, editor, stored])

  // 源码视图改动 → 回写并同步进富文本
  const onSource = (value: string) => {
    setText(value)
    editor?.commands.setContent(value || '', { emitUpdate: false })
    if (!connected) return
    invoke('dev.changelog.set', { text: value }).catch(() => {
      /* 忽略 */
    })
  }

  const applyLink = () => {
    if (!editor) return
    const url = href.trim()
    if (!url || url === 'https://') {
      editor.chain().focus().unsetLink().run()
    } else {
      editor.chain().focus().extendMarkRange('link').setLink({ href: url }).run()
    }
    setLinkOpen(false)
    setHref('https://')
  }

  const words = text.replace(/\s/g, '').length

  return (
    <Sheet
      open={open}
      title="更新日志"
      subtitle="所见即所得；写的内容会一起打进更新包，玩家在「更新日志」里看到的就是它"
      width={860}
      onClose={close}
      footer={
        <div className={styles.foot}>
          <span className="t-small">
            {words ? `${words} 字` : '还没写内容'}
            {text ? ` · ${text.split('\n').filter((l) => l.trim()).length} 行` : ''}
          </span>
          <div className={styles.footRight}>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => {
                onSource('')
                editor?.commands.clearContent(true)
              }}
              disabled={!text}
            >
              清空
            </button>
            <button type="button" className="btn btn-primary" onClick={close}>
              完成
            </button>
          </div>
        </div>
      }
    >
      <div className={styles.wrap}>
        <div className={styles.bar}>
          <div className="seg">
            {(
              [
                ['edit', '编辑'],
                ['preview', '预览'],
                ['source', 'Markdown'],
              ] as const
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                className="seg-item"
                data-active={view === key}
                onClick={() => setView(key)}
              >
                {label}
              </button>
            ))}
          </div>

          {view === 'edit' && (
            <>
              <span className={styles.divider} />
              <ToolButton
                label="H1"
                hint="一级标题"
                on={editor?.isActive('heading', { level: 1 })}
                onClick={() => editor?.chain().focus().toggleHeading({ level: 1 }).run()}
              />
              <ToolButton
                label="H2"
                hint="二级标题"
                on={editor?.isActive('heading', { level: 2 })}
                onClick={() => editor?.chain().focus().toggleHeading({ level: 2 }).run()}
              />
              <ToolButton
                label="H3"
                hint="三级标题"
                on={editor?.isActive('heading', { level: 3 })}
                onClick={() => editor?.chain().focus().toggleHeading({ level: 3 }).run()}
              />
              <span className={styles.divider} />
              <ToolButton
                label="B"
                hint="加粗"
                on={editor?.isActive('bold')}
                onClick={() => editor?.chain().focus().toggleBold().run()}
              />
              <ToolButton
                label="I"
                hint="斜体"
                on={editor?.isActive('italic')}
                onClick={() => editor?.chain().focus().toggleItalic().run()}
              />
              <ToolButton
                label="S"
                hint="删除线"
                on={editor?.isActive('strike')}
                onClick={() => editor?.chain().focus().toggleStrike().run()}
              />
              <span className={styles.divider} />
              <ToolButton
                label="•"
                hint="无序列表"
                on={editor?.isActive('bulletList')}
                onClick={() => editor?.chain().focus().toggleBulletList().run()}
              />
              <ToolButton
                label="1."
                hint="有序列表"
                on={editor?.isActive('orderedList')}
                onClick={() => editor?.chain().focus().toggleOrderedList().run()}
              />
              <ToolButton
                label="❝"
                hint="引用"
                on={editor?.isActive('blockquote')}
                onClick={() => editor?.chain().focus().toggleBlockquote().run()}
              />
              <ToolButton
                label="</>"
                hint="代码块"
                on={editor?.isActive('codeBlock')}
                onClick={() => editor?.chain().focus().toggleCodeBlock().run()}
              />
              <ToolButton
                label="—"
                hint="分割线"
                onClick={() => editor?.chain().focus().setHorizontalRule().run()}
              />
              <span className={styles.divider} />
              <ToolButton
                label="链接"
                hint="链接"
                on={editor?.isActive('link')}
                onClick={() => setLinkOpen((v) => !v)}
              />
              <ToolButton
                label="撤销"
                hint="撤销"
                disabled={!editor?.can().undo()}
                onClick={() => editor?.chain().focus().undo().run()}
              />
            </>
          )}

          {view !== 'edit' && (
            <span className={`t-small ${styles.barNote}`}>
              {view === 'preview'
                ? '玩家端「更新日志」抽屉里的排版'
                : '最终写进 .eapack 的原文，可直接改'}
            </span>
          )}
        </div>

        {linkOpen && view === 'edit' && (
          <div className={styles.linkRow}>
            <input
              className="input"
              autoFocus
              value={href}
              onChange={(e) => setHref(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') applyLink()
                if (e.key === 'Escape') setLinkOpen(false)
              }}
              placeholder="https://…"
              spellCheck={false}
            />
            <button type="button" className="btn" onClick={applyLink}>
              应用
            </button>
            <button type="button" className="btn btn-ghost" onClick={() => setLinkOpen(false)}>
              取消
            </button>
          </div>
        )}

        {view === 'edit' && (
          <div className={styles.area} onClick={() => editor?.chain().focus().run()}>
            {!text.trim() && (
              <div className={styles.placeholder}>
                写点给玩家看的内容：新增了什么、修了什么、要注意什么。
              </div>
            )}
            <EditorContent editor={editor} />
          </div>
        )}

        {view === 'preview' && (
          <div className={`${styles.area} ${styles.previewArea} scroll`}>
            {text.trim() ? (
              <MarkdownPreview text={text} />
            ) : (
              <div className="t-small">还没写内容。</div>
            )}
          </div>
        )}

        {view === 'source' && (
          <textarea
            className={`${styles.source} scroll`}
            value={text}
            spellCheck={false}
            onChange={(e) => onSource(e.target.value)}
            placeholder={'# 1.2.0\n\n- 新增 …\n- 修复 …'}
          />
        )}

        {!connected && <div className="t-small">没连上后端，这次改动不会保存。</div>}
        {connected && text.trim() && (
          <div className="t-small">
            已自动保存；关掉窗口也不会丢。
          </div>
        )}
      </div>
    </Sheet>
  )
}
