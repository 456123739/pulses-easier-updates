# 新界面（React + pywebview）重建进度

分支：`feat/ui-web-render`。Tk 界面保持原样，`PULSES_UI=web` 才启动新界面。

## 已完成

### 骨架与桥
- `app/ui_web/host.py`：pywebview 宿主（1440×860，最小 1000×620，DOM 拖放接 Python，
  `http_server=True` 供 ES module 加载，关闭守卫）
- `app/ui_web/api.py`：js_api 三方法（`invoke` / `pick_path` / `handle_drop`），
  信封统一 `{ok, data|error{code,message,detail}}`；方法白名单
- `app/ui_web/events.py`：EventBus + EventPump（60fps 合批、上限丢弃、顺序保留）
- `app/ui_web/controller.py`：无 UI 控制器（快照 / 身份 / 设置 / 整合包 / 最近打开 / 对话框）
- `app/ui_web/flow.py`：**更新流程无头实现**（导入 → 解压+清单比对 → 策略表 →
  任务收集 → 引擎下载 → build_plan/execute_plan → 复核），单文件重试

### 前端
- React 19 + TS + Zustand + Framer Motion + CSS Modules（Vite，产物 `web/dist`）
- 设计系统：哑光深色（无高光/无发光），发丝线 + 极浅投影，强调色只用于主操作
- 组件：Shell / Sidebar / RoleSwitch / Cover（换页遮罩）/ DropZone / StrategyCard
  （折叠分区「无需更新」「已禁用」）/ ChangeTree / ProgressBar / SlotPanel / LogPanel /
  MainButton（六态）/ PendingBanner + PendingSheet / ChangelogSheet / PreferencesSheet /
  DialogHost / ToastHost / Splash
- 与后端真实联通：定位整合包、最近打开、导入更新包、比对、改策略、应用、清空、
  查看更新日志、补入重试、首选项读写

### 打包与 CI
- `pulses-easier.spec` 收集 `web/dist`；hiddenimports 补 `webview.platforms.winforms`
- `build-windows.yml`：setup-node → `npm ci` → `tsc --noEmit` → `npm run build` → 单元测试 → GUI 冒烟 → PyInstaller
- `build_windows.bat`：有 node 就先构前端，没有则跳过并提示

## 测试

| 层 | 位置 | 现状 |
|---|---|---|
| 单元（Python） | `tests/` | 468 项全过（新增 flow 4 项 + 桥 6 项） |
| 界面交互（Playwright） | `web/scripts/ui_test.mjs` | 12 项全过：渲染/胶囊/折叠/换身份/首选项/受阻条/下载态/无控制台错误 |
| 截图 | `web/scripts/shot.mjs` + `ui_test.mjs --shots` | `web/shots/*.png` |
| 端到端（Tk 真窗口） | `tests/e2e_flow.py`（Windows） | 未改，仍覆盖完整流程 |
| 端到端（新界面，真前端 + 真 Api） | `scripts/dev/run_ui_e2e.py` | **A–H 全覆盖（133 条断言）**：定位 → 导入 → 策略/折叠/日志/首选项/白名单/数据库 → 应用 → 磁盘核对 → 开发者导出 → 真下载/中止/续传 |
| 真机驱动的本地替身 | `scripts/dev/run_e2e_driver_local.py` | 在 Chromium 里跑一遍 Windows 驱动的点击与断言（真机前先自检） |
| 外壳冒烟（真 WebView2 起窗） | `tests/web_e2e_windows.py` / CI 打包后起窗 | CI 里跑，证据进 artifact |

## 已补完（第二轮）

- 开发者流程：导入 zip → 更新范围（点目录名排除）→ 选项 → 更新日志 → 导出 `.eapack`
  （后台线程、文件级进度、可选保存位置、清空）
- 续传：`resume.json`（记录已完成文件/策略/勾选），主按钮变「继续更新」，
  续跑时已下好的文件不重复下载
- 白名单 / 数据库位置抽屉（读写即生效）
- 受阻条：打开补入抽屉、逐个重试、「放弃…」清空待补
- 变更行/范围行 `content-visibility`，几千行滚动不再掉帧
- 进度文案来自后端（正在下载 / 应用 12/40 / 更新完成）
- 真机端到端：`app/ui_web/e2e_driver.py` 在真实 WebView2 页面里点按钮，
  `tests/web_e2e_windows.py` 搭夹具核对磁盘；CI 里阻断并重试一次

## 端到端是怎么做到"真机"的

浏览器里跑不了新界面，是因为它依赖 pywebview 注入的 `window.pywebview`。
`scripts/dev/bridge_server.py` 用 HTTP 把同一套东西补上（`/api/invoke` 直接调
`app/ui_web/api.py`，`/api/events` 拉事件流），页面里注入同构 shim。

于是 CI 上的端到端是：**真前端产物 + 真 Api + 真流程 + 真磁盘**，Playwright 点按钮；
只有外壳（WebView2）不同，外壳另有一条"打包后起窗冒烟"覆盖。

## 稳定性修补（第三轮）

- `_atomic_write_text`：Windows 上目标被占用时 `os.replace` 会 PermissionError，
  现在重试 5 次再退化为直写，失败会打日志（不再静默丢写）
- 白名单保存后立刻回读校验，不一致直接报错（避免"界面显示已保存其实没存"）
- 比对前先 `recover_leftovers`，清掉上次中断留下的 `.xxx.pulses_tmp`
- 策略表：待更新条数来自后端；「统一策略」浮层可一次改全部；策略配置可导入/导出
- 更新完成后主按钮回到「开始更新」，不再是停在「确认更新」

## CI 结论：GitHub Windows runner 起不了 WebView2

- 源码运行与打包运行都一样：`webview.start()` 后窗口报 `Main window failed to start`，
  页面从未 loaded（`tests/web_e2e_windows.py` 与打包冒烟都能复现）。
- 因此 CI 里能自动验证的是：类型检查、构建、474 项单元测试、**浏览器里的真前端 + 真 Api 端到端**
 （`scripts/dev/run_ui_e2e.py`，12 项断言）、Tk GUI 冒烟、PyInstaller 打包、**打包后 exe 起窗**。
- 真正的 WebView2 渲染只能在用户 Windows 机器上验证。
- 程序侧兜底：`runtime_available()` 找不到 `msedgewebview2.exe` 就直接用 Tk；
  `webview.start()` 返回而页面没加载 → 抛错 → main.py 的 auto 分支退回 Tk。

## 交付前的兜底

- 默认启动顺序：受监督子进程跑新界面 → 页面 `loaded` 写就绪文件 → 交棒；
  25s 未就绪或子进程异常退出 → 杀掉子进程、退回 Tk（`app/ui_web/launch.py`）。
- 本机没有 WebView2（找不到 `msedgewebview2.exe`）→ 直接用 Tk。
- `PULSES_UI=tk` / `web` 可强制；`PULSES_WEB_LOG` 写启动留痕，方便排查。

## 未完成（按优先级）

1. **Tk 完整对齐**：检查点/中断残留恢复（`recover`）、应用阶段的"最低空间"提示、
   多显示器 DPI 细节仍未搬到新界面
2. **拖放**：DOM 拖放已接（pywebview `pywebviewFullPath`），Windows 实机路径待验证
3. **槽位面板**：已按 `bytes` 事件渲染，但"磁盘策略限制后的有效并发"未显示
4. **虚拟列表**：目前用 `content-visibility` 折中；上万行仍可能需要真虚拟化
5. **主题**：只有深色；未做浅色/跟随系统

## 端到端扩到覆盖所有功能（第四轮）

原来只跑主流程（定位 → 导入 → 比对 → 应用 → 磁盘），现在按功能逐项断言：

- **策略**：`mods` 从「完全匹配」切「跳过重名」→ 重新扫描 → 右栏出现「跳过 2」、
  快照 `skipped=2` 且逐条标记，再切回来归零
- **折叠区**：「无需更新（1）」/「已禁用（1）」展开收起时内容可见性真的变化
- **更新日志**：抽屉里出现版本号与正文，Esc 关闭
- **首选项**：「更多 → 首选项」抽屉里有「界面」（自动/旧界面/新界面），
  选了会落到 `app.settings.ui_mode`（新界面以前漏了这一项，切不回旧界面）
- **白名单**：写 `["mods","config"]` 回读一致；`["../x","","mods"]` 被清洗成 `["mods"]`；
  跑完还原原值
- **数据库**：`app.db.get` 路径存在 → 切到临时目录回读一致 → 还原
- **开发者**：切身份 → 导入 zip → 排除 `config` → 改策略 → 开关选项 → 写日志 →
  导出 `.eapack`；导出的包在 Python 侧用 `app.core.eapack` 读回校验
  （策略/排除目录/哈希清单/签名/日志）
- **真下载**：`make_download_fixture.py` 造"index 里带 http URL"的更新包
  + `file_server.py`（支持 Range、可限速）→ 3 个文件真下载（5MiB 那个走文件内分片），
  逐字节 sha256 一致；进度条与槽位面板确实动过
- **中止/续传**：下载中点「取消下载」→ 不卡死、进"待补入"→ `update.resume` 续传完成
- **关闭守卫**：下载中 `controller.can_close()` 为 False（bridge 的 `/api/close_guard` 等价
  WebView2 的 closing 事件）

顺带修掉的真 bug（都是由断言暴露出来的）：

1. `Flow.collect_tasks` 把 `MRFile` 当 dict 调 `.get()`（异常被吞）→ 下载任务永远为空
2. 同一个函数按 `dest/size/hashes` 造 `DownloadTask`（字段名全错）→ 一旦有任务就 TypeError
3. 扫描不读更新包 index 里的哈希（只读从不被写入的 `ea_hashes["index"]`）
   → index 声明的文件既不显示也不会下载
4. `start_apply`/`_apply_worker` 进入下载/应用时不推状态 → 主按钮变不出「取消下载」
5. `MainButton` 在下载态把自己 `disabled` → 取消按钮点了没反应
6. `PlayerView` 从没把后端 `progress` 接到进度条 → 一直显示演示态的 100%
7. `group_changes` 的 `skipped` 恒为 0 → 改策略后右栏毫无变化（"跳过 N"/"已跳过"是死 UI）
8. `can_close()` 只看没人会设置的 `_busy` → 下载中点关闭会直接退出
9. `app.settings` 没有 `ui_mode`、首选项没有「界面」→ 新界面里切不回旧界面
