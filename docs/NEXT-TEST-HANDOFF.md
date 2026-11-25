# Pulses Easier 下一任务测试交接

## 任务边界

本交接任务只做测试、复现和证据收集，暂不修改产品代码。测试结果返回后，再根据实际失败项制定修复方案。

当前不能宣称“全部功能已完成”。本轮已经修复了一批高风险状态问题，但完整性、真实 Windows 拖放、真实包回归和最新改动的全量回归仍需确认。

## 当前已知基线

- 全量 Python 测试：本轮最新改动之前曾通过 `752 passed, 1 skipped`。
- 本轮完整性/应用相关聚焦测试：
  - `tests/test_web_flow.py`
  - `tests/test_transfer.py`
  - `tests/test_recover.py`
  - 最近一次结果：`61 passed`。
- 本轮较早阶段的 Web 流程聚焦测试曾通过 `65 passed`，但之后又加入了签名拒绝、清单哈希、续传缓存校验、启动错误态等改动，必须重新运行，不能复用旧结果。
- 前端类型检查：`npm run typecheck` 最近一次通过；脚本已改为 `tsc -b --noEmit`，不应再向 `web/src` 生成 `.js` 副产物。
- Windows 真实 Explorer 拖放：此前多次 CI 均失败，尚未形成通过证据。
- 0.13.2 已有发布资产，但当前工作树有未提交的功能和诊断改动；本测试任务不要直接宣称新改动等于已发布版本。

## 测试对象

### 真实标准整合包

- ZIP：`/vol2/@appshare/Harness/.dsh/attachments/v1/files/09/09/09f9033a9f7589cc527d226647faa6a4ff2bda2582d326f0d6b1a879977fa498/Minerizon beta.zip`
- 文件大小：`749872105` bytes
- SHA-256：`09f9033a9f7589cc527d226647faa6a4ff2bda2582d326f0d6b1a879977fa498`
- 已解压标准目录：`/vol2/@appshare/Harness/fixtures/MinerizonBeta/Minerizon beta`
- 真实包基线：`minecraft=1.21.1`、`loader=neoforge`、`loaderVersion=21.1.219`、ZIP 约 3182 个条目、解压后约 2785 个文件。

不要复制第二份 ZIP 或第二棵完整解压树。磁盘空间有限，测试结束清理新生成的临时导出物。

### PCL 参照

- PCL 源码：`/home/admin/NS/pcl_analysis/src/PCL2-full-2.13.1.1`
- PCL 可执行文件：`bench/pcl/PCL.exe`
- PCL 对比时忽略启动器自有文件：`PCL/Setup.ini`、`PCL/config.json`、`command_history.txt`。除此之外，整合包数据应逐路径比较。

## 测试顺序

### 1. 先跑全量回归

```bash
cd /home/admin/NS/pulses_easier
.venv/bin/python -m pytest -q
cd web
npm run typecheck
npm run build
```

记录：通过数、失败数、跳过数、首个失败的完整 traceback。若全量测试失败，先不要做 UI 截图或发布判断。

### 2. 玩家端业务流程

使用现有测试夹具和真实 Web bridge，覆盖以下顺序：

1. 定位实例。
2. 点击导入更新包。
3. 对同一个更新包执行扫描。
4. 修改策略后点击写入。
5. 等待下载、应用、复核完成。
6. 再次扫描确认结果稳定。

必须验证：

- 扫描前未定位实例或未导入更新包时，按钮和错误信息明确。
- 切换实例时，旧更新包、旧 diff、旧任务不会残留。
- 扫描/下载/应用中不能切换实例、清空实例、切换角色或修改策略；拒绝操作不能释放 pack lock，也不能改变当前状态。
- 策略保存失败时，主按钮不能继续执行 `update.apply`。
- 下载失败后点击“放弃”，失败路径不应被下一次 apply 自动重新下载；其余可应用内容仍应按预期处理。
- 取消、暂停后继续、暂停后重新扫描都不会污染下一次更新。
- 成功更新进入 `done`，并清除 resume/checkpoint；失败或复核失败不能进入成功态，必须保留恢复记录。

建议优先运行：

```bash
.venv/bin/python -m pytest -q \
  tests/test_web_bridge.py \
  tests/test_web_flow.py \
  tests/test_web_dev.py \
  tests/test_web_dev_instance.py \
  tests/test_web_dev_open.py \
  tests/test_transfer.py \
  tests/test_recover.py
```

### 3. 更新包完整性与续传

需要新增或手工复现以下黑盒场景，并保存失败包/日志：

- 对开启防篡改的 `.eapack` 修改 `overrides` 或 `changelog.md` 后导入：应拒绝，不应进入扫描或应用。
- 对 `ea_hashes.json` / `modrinth.index.json` 声明的内容做同大小篡改：扫描应失败，不应“以解压内容为准”继续应用。
- 模拟应用中途失败或复核失败：
  - 不得进入 `done`；
  - `resume.json` 必须保留；
  - checkpoint 不得被标为 `applied`；
  - 下次恢复可以重新处理失败路径。
- 模拟已经下载成功后修改缓存文件，再点击继续：缓存必须重新按声明哈希验证，不能只因为文件存在就跳过下载。
- 在 Pack A 留下 resume 后选择 Pack B：Pack A 的恢复记录不能被无条件删除；切回 Pack A 时仍能识别记录。

### 4. 开发者 ZIP 导出范围

用 `tests/test_web_dev.py` 的 ZIP 模式或等价黑盒操作：

1. 导入同时包含 `mods/` 和 `config/` 的 `.mrpack`。
2. STEP 1 只选择 `mods`。
3. 保持 `min_size=false`。
4. 导出 `.eapack`。

验收：

- `overrides/mods/**` 存在。
- `overrides/config/**` 不存在。
- `ea_manifest.json`、`ea_settings.json` 与实际内容一致。
- 未选择的顶层目录不能因为 ZIP 模式而重新进入包。
- `options.txt` 的行为与 PCL 默认口径一致，不能因历史 NEVER_PACK 逻辑再次漏掉。

### 5. 拖放路由

先做 bridge 黑盒，再做 Windows 原生：

- 玩家 STEP 1 拖入实例目录/实例文件：走实例定位。
- 玩家 STEP 2 拖入有效 `.eapack` / 更新 `.zip`：走 `update.pick`，不能走 `pack_locator.locate_modpack`。
- 开发者 STEP 1 拖入实例或启动器导出包：走 `dev.open`。
- 拖入错误文件、路径为空或没有 `pywebviewFullPath`：有明确错误，遮罩会结束。

Windows 原生探针：

```powershell
python scripts/dev/win_drag_probe.py --source
```

需要保存：

- 应用窗口和 Explorer 的截图。
- 拖动起点、终点和窗口矩形。
- `PULSES_E2E_STATE` 内容。
- 探针退出码。

只有 `RESULT PASS` 且状态文件包含正确更新包路径，才能宣称真实拖放通过。此前的 `WM_DROPFILES` 兜底失败不能算 HTML5/WebView2 drop 通过。

### 6. 冷启动与错误反馈

在桌面 WebView2 冷启动、首次运行、较慢机器条件下验证：

- 桥接延迟超过首帧时，桌面端不能进入假的演示数据。
- 后台桥接超时后应显示“桌面桥接未就绪”和“重试连接”。
- 点击重试后，桥接恢复时能进入真实状态。
- 浏览器预览模式仍可使用 mock 状态，不要把浏览器预览失败误判为桌面应用成功。
- 下载、应用、清空、拖放失败均要有可操作错误，不得只写日志或静默吞错。

### 7. 真实 Minerizon 回归

必须使用上面的真实 ZIP/目录，不得只用合成包：

- 扫描显示 `Minecraft 1.21.1 / NeoForge 21.1.219`。
- 完成一次默认范围扫描，记录文件数、总字节、耗时、跳过项、坏文件名和 unsafe link。
- 用同配置导出 legacy ZIP，并与 PCL 导出结果按相对路径比较。
- 记录 Easier/PCL 的导出耗时和输出大小；Easier 不得比 PCL 更慢。
- 若测试超级压缩 beta，单独记录其耗时/大小，不与 legacy 兼容结论混淆。
- 真实包测试结束后删除临时输出，不删除标准源对象。

已有脚本可参考：

```bash
PULSES_REAL_FIXTURE='/vol2/@appshare/Harness/fixtures/MinerizonBeta/Minerizon beta' \
  .venv/bin/python scripts/dev/pcl_export_bench.py
```

Windows PCL 对比需要在 Windows CI 执行，不要把 Linux 本地结果当作 PCL UI 速度证据。

## 当前修改约束

- 测试返回前不要修改产品代码、不要重写分支、不要回滚已有用户改动。
- 不要把诊断脚本或失败探针自动并入正式发布版本。
- 不要强制删除不属于本任务的 `/tmp/mcc-bench` 或其他共享目录。
- 不要重复下载/解压 Minerizon ZIP。
- 每个失败项记录：复现步骤、输入文件 SHA-256、实际结果、期望结果、日志路径、截图路径、是否稳定复现。

## 测试交付格式

返回一份简短报告，按下面格式：

```text
环境：OS / Python / Node / WebView2
代码版本：git branch + commit + dirty files
全量测试：passed / failed / skipped
前端检查：typecheck / build
玩家流程：PASS/FAIL + 失败项
完整性与续传：PASS/FAIL + 失败项
ZIP 范围：PASS/FAIL + 证据
拖放 bridge：PASS/FAIL + 证据
Windows 原生拖放：PASS/FAIL + run id / artifact
Minerizon：加载器、文件数、PCL 对比、速度、输出大小
阻塞问题：按 P0/P1/P2 排序
建议下一步：只列需要改代码的项目
```

## 通过标准

只有以下条件同时满足，才能进入下一轮发布判断：

1. 最新工作树全量测试通过，或每个失败都有明确的非产品原因。
2. 玩家端成功、取消、暂停、失败恢复、切包保护全部通过。
3. 损坏签名、清单哈希不一致、缓存篡改不会被应用。
4. ZIP 导出范围严格遵守 STEP 1 选择。
5. STEP 2 更新包拖放 bridge 通过；Windows 原生拖放另有 `RESULT PASS` 证据。
6. Minerizon 真实包加载器识别、完整性和速度对比有可复核数据。

测试报告返回前，本任务不做进一步功能修改。
