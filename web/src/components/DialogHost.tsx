import { AnimatePresence, motion } from 'framer-motion'
import { invoke } from '@/bridge'
import { useApp } from '@/store/app'
import './DialogHost.module.css'

/** 弹窗宿主（骨架版）：alert/confirm/locate 用同一个壳，细节待规范。 */
export default function DialogHost() {
  const dialog = useApp((s) => s.dialog)
  const close = useApp((s) => s.closeDialog)
  const connected = useApp((s) => s.connected)

  const respond = (buttonId: string, checked?: boolean) => {
    if (connected && dialog) {
      void invoke('app.dialog.respond', {
        id: dialog.id,
        button: buttonId,
        checked: Boolean(checked),
      }).catch(() => {})
    }
    close()
  }

  return (
    <AnimatePresence>
      {dialog && (
        <motion.div
          className="dialog-mask"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.17 }}
        >
          <motion.div
            className="dialog-card"
            initial={{ opacity: 0, y: 16, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 8, scale: 0.98 }}
            transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
            role="dialog"
            aria-modal="true"
          >
            {dialog.title && <div className="dialog-title">{dialog.title}</div>}
            <div className="dialog-message">{dialog.message}</div>
            {dialog.detail && <div className="dialog-detail selectable">{dialog.detail}</div>}
            <div className="dialog-actions">
              {(dialog.buttons ?? [{ id: 'ok', label: '好', primary: true }]).map((b) => (
                <button
                  key={b.id}
                  className={`dialog-btn${b.primary ? ' primary' : ''}`}
                  onClick={() => respond(b.id)}
                >
                  {b.label}
                </button>
              ))}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
