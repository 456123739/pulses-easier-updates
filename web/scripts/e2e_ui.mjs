// 端到端套件只有一份：scripts/dev/e2e_ui.mjs。
// 这里转发过去，`npm run test:e2e` 与 `python scripts/dev/run_ui_e2e.py`
// 跑的是同一套断言，不会再出现"两份逻辑各自漂移"。
await import('../../scripts/dev/e2e_ui.mjs')
