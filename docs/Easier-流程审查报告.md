# Easier 玩家端 / 开发者端 操作与处理流程审查报告

| 项目 | 说明 |
|---|---|
| 审查对象 | `/home/admin/NS/pulses_easier`（玩家端 10 步 + 开发者端 6 步 + 横切异常/并发） |
| 审查方式 | 4 个**只读**子代理分头深读 + 主审自读状态机主干 + **2 个可复现实验**（不是纸面推理） |
| 代码规模 | 15,262 行（`app/` + `main.py`） |
| 结论 | 大纲的两条主干**基本准确、步骤齐全**；但发现 **2 个实测确认的 P0**（其中一个会**永久删除玩家本地文件且不进回收站**）、4 个 P0/P1 级状态机缺口、以及 20+ 条 P1/P2 |
| 未做 | 本文档只做审查，**未修改任何代码**；两个实验在临时目录内完成并已清理 |

**严重度定义**
* **P0** — 会造成数据丢失/损坏，或流程无法完成且无出口
* **P1** — 功能性错误、状态不一致、功能缺失，但有绕行
* **P2** — 体验、健壮性、死代码、文档偏差

---

## 〇、两个实测确认的结论（最高优先）

### 实验 1：UPDATE 包会**永久删除**玩家本地目录里"包没带的文件"（P0，实测确认）

复现脚本（临时目录内，内容已核对）：

```
instance/config/shipped.toml      ← 包里有、内容完全相同（仅 mtime 不同）
instance/config/user-only.toml    ← 玩家本地独有
overrides/config/shipped.toml     ← 更新包只带这一个

diff  → modified=1  根因: config  is_folder_level=True
plan  → replace_dirs=[('config', '完全匹配')]
应用后 instance/config → ['shipped.toml']        ← user-only.toml 没了
回收站 → None（根本没进回收站）
```

**根因链（5 环，每一环都有代码依据）**

| # | 位置 | 行为 |
|---|---|---|
| 1 | `app/core/differ.py:91` | `folder_hash()` 把 `int(st.st_mtime)` 计入摘要，**不读内容** |
| 2 | `player_view.py:734-735` | 更新包是现解压出来的，`zipfile` 不还原 mtime → 即使内容完全相同，`folder_hash` 必然不同 → 产出**文件夹级 MODIFIED** |
| 3 | `player_view.py:934-937` + `959` | `_default_strategy_for(UPDATE)` 返回 `""`，随后被 `if not mapping.get(f)` 填成 **FULL_MATCH（"全部覆盖，含删除"）** |
| 4 | `app/config.py:59-65` | 本该起作用的 `DEFAULT_STRATEGY`（`config`→**替换重名**、`saves`→**跳过重名**）**是死代码**——唯一引用方 `option_panel.py` 全项目从未实例化 |
| 5 | `app/core/updater.py:132-142` | FULL_MATCH 走 `_replace_dir_full`：`shutil.rmtree(dst)` 然后 `copytree(src,dst)`——**绕开回收站**（而模块 docstring 第 20 行写的是"删除走回收站"） |
| 附加 | `app/core/mrpack.py:184-185` | `merge_files` 丢弃 `MERGE_FOLDERS`（`mods/resourcepacks/shaderpacks/tacz`）之外的 index 独有项 → 应用源里只有 overrides 带的那些文件 |

**影响**：任何非白名单顶层目录（`config` / `kubeconfig` / `defaultconfigs` / `scripts` / `kubejs` …）只要更新包里带了其中**任意一个文件**，玩家的整个该目录就会被清空重写。玩家的按键绑定、视频设置、其他模组的配置**全部永久丢失，且回收站里没有备份**。
**触发频率**：几乎每次更新。`final_wl = 更新包 whitelist ∪ index 顶层`，默认 whitelist 是 `mods/resourcepacks/shaderpacks`，所以 `config` 永远落在非白名单侧。

**最小修复方向**（任一即可止血，建议 1+2 一起做）
1. `folder_hash` 去掉 mtime（改成 `相对路径 + 大小`，或直接比文件集合）；mtime 本来就不该参与"内容是否变化"的判断。
2. `_replace_dir_full` 先 `trash_mod.move_to_trash()` 再拷贝；失败时能回滚。
3. 把 `DEFAULT_STRATEGY` 接回玩家端默认值（`config`→替换重名），或至少把非白名单目录的默认改成 `REPLACE_SAME`（合并而非替换）。

### 实验 2：更新包不含 `overrides/` 时，**包自己的元数据会被写进玩家整合包根目录**（P0，实测确认）

```
导出（源包无 overrides）→ 包内条目：
    ['modrinth.index.json', 'ea_settings.json', 'ea_hashes.json', 'ea_manifest.json']
    有没有 overrides/ 条目 : False
玩家端判定 → _overrides_root = 解压根目录（而不是 overrides/）
会被当成"更新内容"的根散文件：
    ['ea_hashes.json', 'ea_manifest.json', 'ea_settings.json', 'modrinth.index.json']
detect_pack_kind → update
```

**根因**：`eapack.py:159-166` 只有存在 override 文件时才写入 `overrides/` 条目；而 `player_view.py:737-741` 用「解压目录里有没有 `overrides/`」来决定内容根：

```python
overrides_dir = extract_dir / "overrides"
if overrides_dir.is_dir():
    self._overrides_root = overrides_dir
else:
    self._overrides_root = extract_dir        # ← 元数据文件成了"更新内容"
```

之后 `_build_merged_source`（`player_view.py:1363-1369`）全量拷入 `_merged`，`differ` 把它们当根文件 ADDED（`differ.py:432-439`），`execute_plan` 逐个 `copy2` 到玩家整合包根目录（`updater.py:249-258`）——若玩家目录里已有同名 `modrinth.index.json` / `changelog.md`，会被**直接覆盖**。

**最小修复**：导出时无条件写入 `overrides/` 目录条目；玩家端回退分支排除保留名（`ea_*` / `modrinth.index.json` / `changelog.md`）。两边都做最稳。

---

## 一、玩家端审查报告

### 1.1 逐流程检查（大纲 vs 实际）

| # | 大纲 | 实际实现 | 结论 |
|---|---|---|---|
| 1 | 拖入任意文件 → 向上找 `mods`+`config` → 信息卡 | `sidebar.py:182-202` → `pack_locator.py:76-122`（规则也接受 `*-native*.jar`）→ `sidebar.py:196 set_modpack` → `player_view.py:411-430` **再读一次** `read_modpack_info` | ⚠️ 基本一致。偏差：定位失败**无任何日志/提示**（`sidebar.py:193-194` 直接 return，`locate_modpack_ex` 给的 reason 被丢弃）；`InfoCard` 不显示模组数（大纲说有）；同一个包被读两遍 |
| 2 | 拖入 `.zip`/`.eapack` → 识别类型 → 建缓存 → 后台预渲染日志 → 按钮 READY | `player_view.py:475-541`；`pack_detector.detect_pack_kind`；`_resolve_cache_root:607-620`；`_start_changelog_render:546`；`538-539` 置 READY | ✅ 一致（无法识别时 log + 零状态改动 + 高亮保持，`489-500`） |
| 3 | 开始更新 → 解压 → 解析 index → 合并 overrides → 变更列表 + 策略表 | `_start_scan:705` → 线程 `_scan_worker:727`（解压 734、overrides 兜底 737-741、`parse_mrpack` 743、`merge_files` 755、`diff_packs_parallel` 819）→ `_apply_strategies:939` → `_on_scan_done:966` | ⚠️ 一致，但 **`parse_mrpack` 失败被静默降级**（`746-753` 无 log、不提示，直接走"无 index"路径） |
| 4 | 策略调整 → 灰显 + 按钮 CONFIRM↔RESCAN | `_on_strategy_changed:440-459` + 快照比较 `461-472`；`change_list.update_marks` | ✅ 一致 |
| 5 | 重新扫描 → 重算任务、不重解压不重 diff | `_recompute_tasks:676-700` 复用 `self.diff/_merged/_overrides_root` | ✅ **完全一致**（确实是毫秒级） |
| 6 | 确认更新 → 锁侧边栏、多线程池、分片直写、失败移入单线程池重试 | `_start_download_and_apply:1020`；下载器主池+**并行**耐心池（G12 已改） | ⚠️ "失败移入单线程池重试"**已过时**（现在是并行耐心池，与主池重叠）；"锁侧边栏"只锁了 more_panel，**主流程没有锁**（见 1.2 P0-3） |
| 7 | 应用 → 构建合并源 → 计划 → copy/replace/delete（走回收站） | `_start_apply:1310` → `_build_merged_source:1357` → `build_plan` → `execute_plan(use_trash=True)` | ❌ **"删除走回收站"只对 `plan.delete` 成立；目录级替换走 `rmtree` 不进回收站**（见实验 1） |
| 8 | 校验与完成 → copy 就位、replace 目录存在、失败写日志、解锁 | `verify_after_update:313-329`；`_on_apply_done:1388-1408` | ⚠️ 只查"存在"不查内容；**无失败也会显示"更新完成 ✓"**（1390 无条件） |
| 9 | 中断恢复 → resume.json，有效则续传、无效则提示重新定位 | `_write_resume:1506`；`main_window._check_resume:272`；`resume.check_resume_availability:179` | ⚠️ **更新包失效时记录被静默删除**（`resume.py:275-277`），"重新定位更新包"整条路径是死代码（见 1.2 P1-2） |
| 10 | 清理缓存 → cache/checkpoints/temp，占用跳过 | `player_view.py:1617`（清空包）与 `sidebar`→`cache.clean_cache:103` | ✅ 一致（逐文件删、占用跳过并统计）；偏差：清不到系统临时目录里的解压副本（见 P1-5） |

### 1.2 问题列表

#### [P0-1] 非白名单目录被默认"完全匹配"→ rmtree 玩家本地整个目录，且不进回收站
* 位置：`differ.py:91`、`player_view.py:934-937/959`、`config.py:59-65`、`updater.py:103-104/132-142`、`mrpack.py:184-185`
* 现象/影响/修复：见 **实验 1**
* 置信度：**实测确认**

#### [P0-2] 更新包无 `overrides/` 时元数据被写进整合包根目录
* 位置：`eapack.py:159-166`、`player_view.py:737-741`、`1363-1369`、`differ.py:432-439`、`updater.py:249-258`
* 现象/影响/修复：见 **实验 2**
* 置信度：**实测确认**

#### [P0-3] 下载阶段从未 `app_state.lock()` → 处理中可换包 / 清空整合包 / 再开一轮下载
* 位置：`player_view.py:1421-1430`（`_install_close_guard` 只做 `sidebar.set_locked(True)`）、`1329`（全项目**唯一** `app_state.lock()`，在应用阶段）、闸门 `476` 与 `1617`、`538-539`、`1557-1593`
* 现象：
  * 下载中拖入新更新包 → `_on_update_pack_dropped` 闸门失效 → `495` 换掉 `_cache_root`，`538-539` 把按钮从"处理中…"**改回"开始更新"**；已启动的下载线程仍持有旧目录（`1025` 的局部变量）。
  * 下载中点「清空」→ `_reset_pack_state` 清空 `_download_tasks/_completed_files/_cache_root`、`_phase=IDLE`、`_abort_flag=False`，并 `_temp_dir.cleanup()`（`1588-1593`）**删掉后续应用要用的解压源**；下载线程仍在跑。
* 影响：更新被应用到错误的包/错误的缓存目录；应用阶段发现源目录已消失；按钮与真实状态脱节。
* 建议：进下载阶段就 `app_state.lock()`；`_on_update_pack_dropped` / `_on_modpack_changed` / 「清空」在 `_phase != IDLE` 时直接拒绝；`_reset_pack_state` 先 abort + join。
* 置信度：确定（代码路径）

#### [P0-4] 「全部跳过」在下载仍在进行时就可点 → 两个 `execute_plan` 并发写同一整合包
* 位置：`player_view.py:1302-1305`（不置 `_abort_flag`、不 join）、`1127-1142`（失败即显示面板）、`download_panel.py:70-75`（按钮无门控）、`1240-1246`（零失败分支会再 `_start_apply`）
* 现象：`on_file_failed` 只在**最终失败**时触发，而耐心池是**并行**的（G12），所以主池可能仍有文件在下；此时点「全部跳过」→ `_start_apply` 读取仍在写入的缓存目录 → 未下完的文件只剩 `.part` 被过滤 → 应用内容不完整；若剩余文件随后都成功，`_on_download_done` 零失败分支会**再应用一次**。
* 影响：两个 `execute_plan` 对同一 `old_root` 做 `rmtree`+`copytree`，互删对方刚复制的目录 → 整合包被删一半/复制一半。
* 建议：`_start_apply` 首行加 `if self._phase == _PHASE_APPLY: return`；`_on_skip_all_failed` 先置 `_abort_flag` 并等下载线程结束；`in_progress` 期间禁用该按钮。
* 置信度：较确定（并发结构确定，"两次应用"依赖时序）

#### [P0-5] 部分失败后状态机没有终态：补入全部成功后**无任何出口**
* 位置：`player_view.py:1248-1258`（失败分支不改 `_phase`、不 `_uninstall_close_guard`、不改按钮）、`download_panel.py:300-305`（`_failed` 清空后整块 `grid_remove()`）
* 现象：下载有失败 → 按钮停在 `_BTN_BUSY`「处理中…」、侧边栏 more 面板仍锁、右栏显示待补入面板。用户把失败文件**全部拖入补齐后**，面板把自己藏起来（连"全部跳过"按钮一起隐藏），player_view 侧**没有任何回调**被通知。
* 影响：本次会话卡死在"处理中"，只能重启进程（重启后靠 resume 继续）。点 X 还会按 `_PHASE_DOWNLOAD` 弹"下载正在进行中"。
* 建议：失败分支进入独立的「等待补入」终态；给 `DownloadPanel` 加 `on_all_resolved` 回调；或失败分支就恢复按钮与解锁。
* 置信度：确定

#### [P0-6] 「无需下载直接应用」不装关闭守卫 → 应用阶段点 X 无强警告
* 位置：`player_view.py:1021-1023`（在 `_install_close_guard()` 之前 return）、`996`（resume 分支同样）、`1443-1447`
* 现象：`_install_close_guard` 是全项目唯一把 `WM_DELETE_WINDOW` 换成 `_on_close_request` 的地方。这两条捷径不经过它 → `_phase` 已是 apply，但点 X 走默认 destroy，不会出现"更新正在写入整合包，强制关闭可能损坏"的确认。
* 建议：把 `_install_close_guard()` 移进 `_start_apply`。
* 置信度：确定

#### [P1-1] 应用无回滚闭环：checkpoint 只写不读、回收站恢复死代码、校验只查存在
* 位置：`updater.py:217-224/270-272/301-302`（写/清）、`checkpoint.py:68 load_checkpoint` **0 调用方**、`trash.py:63 restore_session` **0 调用方**、`trash.py:89 cleanup_trash` **0 调用方**、`player_view.py:1492-1495`（强关直接销毁）、`updater.py:313-329`
* 影响：目录替换中途失败/强杀 → 旧目录已 rmtree、新目录可能只拷一半；`verify_after_update` 只判 `is_dir()`，半成品目录判"通过"；重启后无任何恢复/回滚入口；`.pulses_trash/` 只增不减。
* 建议：FULL_MATCH 替换先入回收站；启动时读 checkpoint 提供"继续/回滚"；verify 至少比对文件数/大小；接上 `cleanup_trash`。
* 置信度：确定

#### [P1-2] resume 记录在更新包暂时失效时被静默删除 → 「重新定位更新包」不可达
* 位置：`resume.py:236-247`（`is_resume_valid` 只看 zip 存在+指纹）、`275-277`（scan 时 `clear_resume`）、`main_window.py:277-285/322-380`
* 现象：`scan_all_resumes` 会先删掉 zip 失效的记录，于是 `check_resume_availability` 里的 `zip_ok=False` 永远不成立 → `need_zip` 恒 False → `main_window.py:361-370` 的「重新定位更新包」与 `_on_relocate_zip/_update_resume_zip` 都是死代码。
* 影响：更新包被移动/改名/在未插的移动盘上 → 记录连同 `completed` 清单一并丢失，无法续传也无法重新定位，只能整包重下。
* 建议：scan 阶段不要因 zip 失效删记录，交给可用性检查 + 重定位流程。
* 置信度：确定

#### [P1-3] 换整合包不失效旧 diff/下载任务 → 用旧比对结果写新包
* 位置：`player_view.py:411-430`（只更新 `pack_info`）、`1311-1326`（`build_plan(self.diff,...)` + `old_root=self.pack_info.path`）
* 影响：在新包上执行针对旧包算出的 copy/delete 计划 → 误删新包文件（进回收站）或漏更新。
* 建议：`pack_root` 变化时强制清空 `diff/_download_tasks/change_list` 并要求重新扫描。
* 置信度：确定

#### [P1-4] 缓存目录只按 zip 文件名 stem 命名 + 整目录并入应用源
* 位置：`player_view.py:607-620`（`safe_name = stem[:64]`，无包身份/指纹）、`1370-1378`（cache 在 overrides **之后**拷，同名以后者为准）
* 影响：同名（同 stem）的另一个更新包或同包新版本复用同一缓存目录时，上次遗留的文件会被当成新内容覆盖本次 overrides 的正确文件；`resume.json` 也会被误认。
* 建议：缓存目录名带 zip 快速指纹；应用前只并入本次任务清单内的 `rel_path`。
* 置信度：确定

#### [P1-5] 解压副本落在系统临时目录 + `os._exit(0)` 绕过清理 → 每次更新残留一整份 merged
* 位置：`player_view.py:730`（`TemporaryDirectory(prefix="pulses_play_")` 用系统 temp）、`1357-1379`、`1588-1593`（唯一 cleanup 在 reset）、`main.py:105-112`（`os._exit(0)`）
* 影响：一次成功更新后 `/tmp/pulses_play_*` 里保留一份完整 merged 副本（GB 级）；「清理缓存」只覆盖 `<db>/cache/*`，boot 只清 `update_packs/**/*.part` —— 都不含系统 temp。
* 建议：`_on_apply_done`/`_force_destroy` 里显式 `cleanup()`；或把工作目录放到 `<db>/cache/temp` 让现有清理覆盖。
* 置信度：确定

#### [P1-6] 应用成功/跳过后不清 resume → 每次启动都弹「继续上次更新」
* 位置：`player_view.py:1242`（**唯一** `clear_resume`，只在下载零失败分支）、`1302-1305`、`996`、`1388-1408`
* 影响：走「全部跳过」或"无需下载"完成更新后，resume 仍在（`failed` 里还留着用户跳过的文件）→ 下次启动弹窗，确认后会重新下载用户明确跳过的文件。
* 建议：`_on_apply_done` 成功后统一 `clear_resume`。
* 置信度：确定

#### [P1-7] 有缺失/失败时仍显示「更新完成 ✓」
* 位置：`player_view.py:1389-1390`（在失败判断**之前**无条件 `set(1.0)` + "更新完成 ✓"）、`1396-1402`（只写日志）、`updater.py:249-260`（源不存在静默 continue）
* 建议：按 `report["failed"]`/`verify_failed` 选择文案与颜色。
* 置信度：确定

#### [P1-8] 中止关闭时 `_completed_files` 尚未回填 → resume 统计偏低
* 位置：`player_view.py:1091-1093`（回填在 `download_files` 返回**之后**）、`1481-1490`（只 `join(timeout=3.0)` 就写 resume）
* 影响：resume 的 `completed` 缺本次已下完的文件，下次"继续更新"的完成/待下载数字错误（实际不会漏文件，因为下载器会对已存在 target 校验后才跳过）。
* 建议：以 `on_file_done` 回调作为 `_completed_files` 的唯一来源。
* 置信度：确定

#### [P1-9] 扫描期间无守卫，二次拖入让 `_scan_worker` 混用旧包目录与新 zip
* 位置：`player_view.py:705-730`（无 phase 守卫）、`734/743/769`（三处重读 `self.update_zip`）
* 影响：旧包 overrides + 新包 index → diff/任务/计划全错。
* 建议：`_scan_worker` 开头把 `update_zip/_cache_root/pack_info` 快照成本地变量；拖入加 phase 守卫。
* 置信度：确定

#### [P1-10] 关闭下载时 `cleanup_stale_parts` 与仍在运行的下载线程竞争 `.part`
* 位置：`player_view.py:1481-1490`、`resume.py:308-323`
* 影响：`unlink` 正在写的 `.part` → 随后的 `part.replace(target)` 抛 `FileNotFoundError` 被判失败；也可能清理后又新建 `.part` 无人回收。
* 建议：join 超时就不要清理（或先确认线程已结束）。
* 置信度：确定

#### [P1-11] index 声明了但不在 `MERGE_FOLDERS` 的文件永远无法应用
* 位置：`mrpack.py:184-185`（丢弃）、`player_view.py:775-777`（`index_top_folders` 仍进白名单）、`updater.py:253-256`（源不存在静默 continue）
* 影响：变更列表显示"要更新"，但既无下载任务也无应用源 → 只在事后 `verify_after_update` 打一条"缺失"。
* 建议：要么让 `merge_files` 保留（并提供下载链接），要么在比对/变更列表里就不显示。
* 置信度：确定

#### [P2] 其余（择要，均有 file:line 依据）
* `_reset_pack_state` 不复位 `_btn_state`（`1557-1593`）→ 清除后若残留 RESUME，新拖入的包会显示"继续更新"并**跳过确认**直接下载+应用（`986-997`）。
* `_resume_after_scan` 在 `_start_scan` 早退时残留（`667-669` 先置位、`706-707` 直接 return）→ 之后一次普通"开始更新"会跳过确认。
* `_phase` 没有 scan 态（`50-63`）→ 扫描期 `_phase=idle` 而按钮 `busy`，扫描中关窗走 else 直接 destroy。
* 中止分支不恢复按钮（`1228-1234`）→ 永远停在"处理中…"。
* `checked` 默认值三处不一致：`change_list.py:93 False` / `player_view.py:885 True` / `updater.py:92 False`（当前因策略表总是填满全部 key 而未暴露，属**潜在**）。
* `differ.py:437-439` 根目录同名文件恒判 MODIFIED（不比哈希）；`differ.py:149-151` 丢弃 index 根文件哈希。
* 单连接续传不校验 `Content-Range`（`downloader.py:1357-1366`），`range_validate` 名不符实。
* 四份 `.part` 清理口径不一致：`cache.py:165` 只 glob `*.part`（**`.part.meta` 清不掉**）/ `resume.py:314` glob `*.part*`（可能误删名字含 `.part` 的正常文件）。
* 配置/断点写盘非原子（`database.py:345-354`、`resume.py:98-100`、`checkpoint.py:59-62`），读侧兜底 = 静默回默认；正确写法可参照 `downloader.py:1519-1531`（tmp + `os.replace`）。
* 死配置项：`log_max_lines`、`progress_throttle_ms`、`hash_threads`（玩家端硬编码 `threads=16`）、`defer_to_single_after` 均无读取方。
* 死代码：`checkpoint.load_checkpoint`、`trash.restore_session`、`trash.cleanup_trash`、`pack_builder` 全模块、`metadata`、`option_panel`（202 行从未实例化）、`md_viewer`、`eapack.read_hashes` / `read_manifest`。
* 「是否会被应用」有三套重复实现：`updater.build_plan`、`player_view._collect_download_tasks_from_diff`、`change_list._will_skip`。
* `os._exit(0)`（`main.py:110`）跳过 `TemporaryDirectory` finalizer 与 atexit。

---

## 二、开发者端审查报告

### 2.1 逐流程检查

| # | 大纲 | 实际实现 | 结论 |
|---|---|---|---|
| 1 | 拖入 `.zip`/`.mrpack` → 解析 index + overrides → 合并 → 解压临时目录 → 策略表 | `developer_view.py:129-167` → `mrpack.parse_mrpack` → `_on_parse_done:173-211` | ⚠️ 一致；但 `extractall` 在**主线程**（`180-184`）→ 大包冻结 UI；解析期间可重复拖入（无锁）；解析失败不清理旧状态 |
| 2 | 策略：勾选、切换、统一、导入/导出；文件级/文件夹级图标；黑名单沉底折叠 | `strategy_table.py:133-165/224-292/314-328/331-382` | ⚠️ 一致；但持久化黑名单在**首次拖入时不生效**（`set_blacklist` 在 `set_folders` 之后且不刷新已建行） |
| 3 | 导出选项 4 项 → 写回数据库 → 导出时生效 | `export_options.py:22-27/74-83`、`developer_view.py:322-327` | ❌ **勾选不是即时写库**（只在点导出时写）；且 `precompute_overrides` 的产物被丢弃、`tamper_proof` 无人校验、`include_hashes` 全链路无人读、`min_size` 对 index 声明的文件无效 |
| 4 | Markdown 编辑器 / 编辑预览 / 大屏预览（独立进程）/ 导入导出 md | `markdown_editor.py:391-412/455-474/746-774`、`markdown_renderer.py:551-579`、`preview_worker.py` | ✅ "独立进程"属实（`Popen([sys.executable,"-m","app.core.preview_worker"...])`）；⚠️ 固定临时文件名 + 子进程失败静默无提示 |
| 5 | 导出 → 复制 index、打包 overrides、写 changelog/settings/hashes/manifest、按需签名；失败回初始；完成打开目录 | `developer_view.py:288-335` → `eapack.export_eapack:32-191` → `_on_build_done:369-379` | ⚠️ 一致；但无取消能力、失败留半成品 `.eapack`（无 tmp+replace）、index 复制失败只 warn 仍报成功、`version` 硬编码 `1.0.0` |
| 6 | 清除 → 清空包/临时目录/策略表/编辑器/进度 | `developer_view.py:239-280` | ✅ 一致（大纲属实） |

### 2.2 问题列表（择要）

#### [P1-1] 解析失败不清理旧包状态 → 用「新 ZIP + 旧包元数据」导出错误更新包
* 位置：`developer_view.py:129-171`（`_on_pack_dropped` 只覆盖 `source_zip`；`_on_parse_error` 只 log + 恢复按钮）、守卫 `288-292`（只看 `pack is None or source_zip is None`）
* 现象：拖入一个合法 zip 但缺 index → 解析失败 → 旧 `pack/merged` 仍在 → 点导出会用**旧包的策略/名称 + 新 zip 的 index** 产出一个自相矛盾的包。
* 建议：`_on_pack_dropped` 开头即清空 `pack/merged/extract_dir` 与策略表；失败时 `source_zip=None`。
* 置信度：确定

#### [P1-2] 解析期间可重复拖入 → 两个解析线程竞态
* 位置：`developer_view.py:130`（只在 `_locked` 时 return，而解析期不置 `_locked`）、`158/165`（线程捕获 `zip_path`）、`183`（`_on_parse_done` 却重读 `self.source_zip`）
* 影响：`pack/merged` 来自 A、`extract_dir` 来自 B → 导出跨包错配。
* 置信度：确定

#### [P1-3] 解压在 Tk 主线程执行 → 大包拖入后界面冻结
* 位置：`developer_view.py:180-184`（`zf.extractall` 在被 `after(0)` 调度的 `_on_parse_done` 里）；对照导出侧的解压在 worker（`341-344`）
* 建议：把解压放回解析 worker；或只按 `overrides/` 前缀选择性提取。
* 置信度：确定

#### [P1-4] 持久化黑名单首次不生效（顺序错误）
* 位置：`developer_view.py:202-206`（先 `set_folders` 再 `set_blacklist`）、`strategy_table.py:159-160`（建行时读 `self._blacklist`）、`167-168`（`set_blacklist` 只赋值不刷新行）
* 影响：上次会话禁用的文件夹被静默重新勾选；用户不检查直接导出会把它们永久写成"启用"。
* 置信度：确定

#### [P1-5] `min_size`（最小体积）对 index 声明的文件完全无效
* 位置：`eapack.py:61-62/65-74`（`excluded` 只作用于 overrides 物理文件与哈希清单）、`152-157`（index 原样复制）、`player_view.py:756-758`（index 顶层仍进策略表并默认勾选）
* 影响：mods 走 index 下载链接 → 玩家照样下载，开发者以为已排除；`settings["excluded_folders"]` 写入后**无人读取**。
* 置信度：确定

#### [P1-6] `tamper_proof` 签名无校验点、且不覆盖真正载荷
* 位置：`eapack.py:138-143`（`payload = {"settings","manifest"}` 的 sha256 写进 `manifest["signature"]`）；全仓 `signature` 无任何读取/校验
* 影响：勾选"防篡改"不产生任何防护；也不是密钥 MAC，任何人都能重算。
* 建议：接上校验链路（含 overrides 清单摘要 + HMAC），或从 UI 移除以免误导。
* 置信度：确定

#### [P1-7] `include_hashes` / `precompute_overrides` 产出无人消费
* 位置：`eapack.py:176-184`（写 `ea_hashes.json`）、`224-237`（`read_manifest`/`read_hashes` 定义）、`81/177`（`precompute_overrides` 只触发计算、只有 `include_hashes` 才写）
* 影响：白付一遍全量哈希计算与体积；`precompute_overrides` 的产品根本不存在。
* 置信度：确定

#### [P1-8] 导出失败/中断留半成品 `.eapack`（无原子写）
* 位置：`eapack.py:152`（`zipfile.ZipFile(out_path,"w")` 直写最终路径）、`189-191`（异常吞掉返回 None，不删半成品）
* 建议：写 `.tmp` 后 `os.replace`。
* 置信度：确定

#### [P1-9] `modrinth.index.json` 复制失败只告警，导出仍报"成功"
* 位置：`eapack.py:194-210`（内部 try/except 只 `log("warn")` 后 return）、`developer_view.py:373-376`（走成功分支）
* 影响：产出玩家端无法识别为 EXPORT、缺下载清单的包，却提示"更新包制作完成 ✓"。
* 置信度：确定

#### [P2] 其余（择要）
* `self.version` 硬编码 `"1.0.0"`（`developer_view.py:44`）且优先于源包 `versionId`（`eapack.py:130`）→ manifest 版本恒为 1.0.0，UI 无版本入口。
* `total_steps = len(override_files)+5` 但实际只有 +3（`eapack.py:76/110/114/125`）→ 进度百分比系统性偏低。
* 拖入对话框允许 `.eapack`，但 `developer_view.py:133-139` 明确拒绝 → UI 与行为矛盾。
* 「导入配置」丢 `export_options` / `whitelist`（`strategy_table.py:343-347` 写 4 项，`363-382` 只读 2 项）。
* 导出选项并非"勾选即写库"（`export_options._fire_change` 无消费者，`developer_view.py:115` 未传 `on_change`）。
* `overrides/` 只认根级，而 `modrinth.index.json` 允许嵌套（`mrpack.py:69-71` vs `85-90`）→ 压缩包内嵌一层目录时"index 找得到、overrides 找不到"。
* 大屏预览固定临时文件名 + 子进程 import 失败时静默返回成功（`markdown_renderer.py:557/567-572`）。
* 开发者端策略默认恒 `FULL_MATCH`（`strategy_table.py:160`），无视 `config.DEFAULT_STRATEGY/DEFAULT_CHECKED`。
* 成功导出后不清理导出用临时目录；开发者端没有关闭守卫。
* 无"取消导出"能力（`progress_panel.py` 只有 label/bar/pct，`export_eapack` 不轮询取消）。
* 死代码/文档漂移：`pack_builder`、`metadata`、`md_viewer`、`config.META_*`、`option_panel` 全无人调用；`metadata.py` docstring 仍写"内嵌 meta.json"（现状是 `ea_settings/ea_manifest`）。
* 开发者端导出/导入链路**零测试覆盖**。

### 2.3 导出字段 ↔ 读取字段对照（重点）

图例：✅ 闭环 ｜ ⚠️ 写了没人读 ｜ 🕳 读了可能不存在（已容错） ｜ 💀 死代码

| 导出产物 / 字段 | 写入 | 读取 | 状态 |
|---|---|---|---|
| `modrinth.index.json` | `eapack.py:152-157,194-210` | `mrpack.parse_mrpack`、`player_view.py:743`、`pack_detector.py:56-57` | ✅（复制失败仅 warn） |
| `overrides/<rel>` | `eapack.py:159-166` | `mrpack.py:85-90`、`developer_view.py:192-199`、`player_view.py:737-741` | ✅（空 overrides 触发**实验 2** 的 P0） |
| `changelog.md` | `eapack.py:168-170` | `read_changelog:240-260` → `player_view.py:528` | ✅ |
| `ea_settings.format_version` | `eapack.py:115` | 无 | ⚠️ |
| `ea_settings.recommended_strategies` | `eapack.py:116` | `read_recommended_strategies:262-273` → `player_view.py:947-955` | ✅ |
| `ea_settings.changelog_file` | `eapack.py:117` | 无（读侧硬编码常量） | ⚠️ |
| `ea_settings.whitelist` | `eapack.py:118` | `read_whitelist:276-282` → `player_view.py:769-775` | ✅ |
| `ea_settings.export_options` | `eapack.py:119` | 无 | ⚠️ |
| `ea_settings.excluded_folders` | `eapack.py:120` | **无** | ⚠️ **min_size 失效的根因** |
| `ea_hashes.{files,folders,whitelist}` | `eapack.py:176-180` | `read_hashes:232-237` **无调用点** | ⚠️ |
| `ea_manifest.*`（format_version/created_at/pack_name/pack_version/game/dependencies/total_files/whitelist/excluded_folders/has_changelog） | `eapack.py:127-136` | 均**无读取方** | ⚠️ |
| `ea_manifest.signature` | `eapack.py:138-143` | **无校验点** | ⚠️ |
| `ea_manifest.json`（存在性） | `eapack.py:182-184` | `pack_detector.py:54-62`（**唯一**被消费的方式：判类型） | ✅ |
| `ea_settings.recommended_checked`（大纲提"预存勾选"） | **从未写入** | 无 | ⚠️ 缺失 |
| `pulses_meta.json.*`（legacy） | `pack_builder.py:29-30`、`metadata.py:15-25` | `metadata.extract_meta_from_zip:42-50` **无调用方** | 💀 |

---

## 三、异常与边界审查报告

| 场景 | 触发点 | 处理位置 | 闭环 | 缺口 |
|---|---|---|---|---|
| 中途取消（下载） | `player_view.py:1477` | `1481-1490`（join 3s→清理→写 resume→销毁） | ⚠️ 部分 | join 超时后清理与在写线程竞争（P1-10）；`_completed_files` 未回填（P1-8）；中止分支不恢复按钮（P2） |
| 中途取消（应用） | `1457-1467` → `1492-1495` | 仅强警告文案 | ❌ 未 | 应用线程被 `os._exit` 杀掉；checkpoint 只写不读 → 无恢复入口（P1-1） |
| 下载失败 | `downloader.py:2250` | 面板 `1127-1142`；补入 `1273-1300`；跳过 `1302-1305` | ❌ 未 | 补入全部完成后面板自隐 → 无任何可点入口（P0-5）；「全部跳过」下载中可点（P0-4） |
| 校验失败 | `downloader.py:1975-2013` | `target.unlink()` + 换源；`_verify:688-707` | ✅ 闭环 | 单连接续传不校验 `Content-Range`（P2） |
| 校验失败（手工补入） | `player_view.py:1287-1295` | 不过则删文件 | ✅ 闭环 | — |
| 定位失效（resume 更新包） | `main_window.py:322-380` | `resume.py:179-233` | ❌ 未 | 记录在 `resume.py:275-277` 已被删 → `need_zip` 恒 False，重定位是死代码（P1-2） |
| 定位失效（侧边栏整合包） | `sidebar.py:182-194` | 直接 return | ❌ 未 | **无任何提示**，`locate_modpack_ex` 的 reason 被丢弃 |
| 镜像换源 | `downloader.py:1952-2049` | 每 URL 独立配额；降级 → 并行耐心池 | ✅ 闭环 | `defer_to_single_after` 配置项从未读取（死配置） |
| 网络卡死 | `downloader.py:1220-1235` | 停滞/时限/速度三判定 + 不排空响应体 | ✅ 闭环 | `_set_socket_timeout` 失败被静默忽略 |
| 状态流转 | `294-334` 六态 | `_BTN_*:50-55` | ❌ 未 | ①下载期无锁（P0-3）②失败等待态卡死（P0-5）③无"完成"终态：完成后回 `_BTN_CONFIRM`（1407），可再点重复执行 ④`_phase` 无 scan 态 |
| 应用部分失败 | `1388-1402` | 只写日志 | ❌ 未 | hint 仍是"更新完成 ✓"（P1-7） |
| 重复触发 | `1302-1305` + `1240-1246` | 无互斥 | ❌ 未 | 两个 `execute_plan` 并发（P0-4） |
| 数据库未设置 | `db_wizard.py:148-160` | `1025-1028` 报错 return | ✅ 闭环 | — |
| 配置/断点写坏 | `database.py:345-354`、`resume.py:98-100` | 读侧兜底 | ⚠️ 部分 | 写非原子 → 静默回默认（P2） |
| `.part` 残留清理 | `main_window.py:186`、`1486`、`downloader.py:2331` | 三处逻辑 | ⚠️ 口径不一 | `cache.py:165` 只清 `*.part` → **`.part.meta` 永久残留**；`resume.py:314` 的 `*.part*` 反而可能误删 |
| 回收站膨胀 | `updater.py:276-285` | `trash.cleanup_trash` 存在但**无调用方** | ❌ 未 | 只增不减；秒级时间戳同秒会覆盖 manifest |
| 导出中断 | `developer_view.py:329-335` | 无取消 | ❌ 未 | `zipfile` 直写最终路径 → 留半截 `.eapack`（P1-8） |

---

## 四、优化建议（按优先级）

### 第一批：止血（P0，建议本次就改）

| # | 动作 | 位置 | 预期收益 |
|---|---|---|---|
| 1 | `folder_hash` 去掉 `mtime`，只比"相对路径 + 大小"（或比文件集合） | `differ.py:81-101` | **消除误判 MODIFIED**，从根上避免目录级替换被无谓触发 |
| 2 | `_replace_dir_full` 改为"先进回收站（或 rename 到同盘临时名）→ 再拷贝 → 失败回滚" | `updater.py:132-142` | 目录替换失败**不再永久丢数据**；与 docstring 承诺一致 |
| 3 | 非白名单目录的默认策略改为 `REPLACE_SAME`（或把 `DEFAULT_STRATEGY` 接回） | `player_view.py:934-959`、`config.py:59-65` | 默认行为从"整体替换"变成"合并覆盖"，不再删玩家本地文件 |
| 4 | 导出时无条件写 `overrides/` 条目；玩家端回退分支排除保留名 | `eapack.py:159-166`、`player_view.py:737-741` | 消除**实验 2** 的跨端契约破损 |
| 5 | 进下载阶段即 `app_state.lock()`；`drop/清空/重新扫描` 在 `_phase != IDLE` 时拒绝；`_reset_pack_state` 先 abort+join | `player_view.py:1030-1036/476/1617/1557` | 处理中不可再换包/清空，杜绝对错误的包执行更新 |
| 6 | `_start_apply` 首行加 `if self._phase == _PHASE_APPLY: return`；`_on_skip_all_failed` 先 abort+join | `player_view.py:1310/1302` | 消除两个 `execute_plan` 并发写同一整合包 |
| 7 | 失败分支给状态归宿：进"等待补入"态；`DownloadPanel` 增加 `on_all_resolved` 回调 | `player_view.py:1248-1258`、`download_panel.py:300-305` | 补入完成后流程能继续，不再卡死 |
| 8 | 把 `_install_close_guard()` 移进 `_start_apply` | `player_view.py:1328` | "无需下载直接应用"也有强关警告 |

### 第二批：闭环（P1）

| # | 动作 | 收益 |
|---|---|---|
| 9 | 接上 `load_checkpoint`：启动时若有未完成 checkpoint，弹"继续/回滚"；`_replace_dir_full` 入回收站后可用 `restore_session` 回滚 | 应用中断**可恢复** |
| 10 | `verify_after_update` 增加内容校验（至少文件数/大小，最好用 index 哈希） | 半成品目录不再被判"通过" |
| 11 | `scan_all_resumes` 不因 zip 失效删记录 | 「重新定位更新包」复活，续传不丢 |
| 12 | 换整合包时清空 `diff/_download_tasks/change_list` | 不再用旧比对结果写新包 |
| 13 | 缓存目录名带 zip 快速指纹；应用前只并入本次任务清单内的 rel_path | 消除陈旧文件覆盖新内容 |
| 14 | `_on_apply_done` 成功后统一 `clear_resume`；失败时提示改为"更新未完全就绪" | 不再重复弹续传、不再误导成功 |
| 15 | 解压/合并源移出系统 temp（放 `<db>/cache/temp`），或在完成/退出时显式 `cleanup()` | 不再残留 GB 级副本 |
| 16 | `_scan_worker` 开头快照 `update_zip/_cache_root/pack_info`；拖入加 phase 守卫 | 扫描期不再被换包污染 |
| 17 | 开发者端：`_on_pack_dropped` 先清状态；解析期加锁；解压移回 worker；`set_blacklist` 后重建行 | 消除跨包错配 + UI 冻结 + 黑名单失效 |
| 18 | `eapack` 写 `.tmp` 后 `os.replace`；index 复制失败视为致命错误 | 不再留半成品 / 不再静默"成功" |
| 19 | `min_size` 真正生效（按 `excluded` 过滤 index）；或移除该选项 | 选项名与行为一致 |
| 20 | `tamper_proof` 接上校验（含 overrides 摘要 + HMAC），否则移除 | 不再误导"防篡改" |

### 第三批：健壮性与整洁（P2）

* 统一 `.part` 清理口径为 `*.part` + `*.part.meta`（不要用 `*.part*`）；接上 `cleanup_trash`。
* `resume.json` / `config.json` / `checkpoint.json` 全部改成 tmp + `os.replace` 原子写。
* 把"是否会被应用"的判断收敛成一个函数（`updater` 侧），`player_view` 与 `change_list` 复用。
* 接上或删除死配置项（`log_max_lines`、`progress_throttle_ms`、`hash_threads`、`defer_to_single_after`）。
* 删除或标注 deprecated：`pack_builder`、`metadata`、`option_panel`、`md_viewer`、`eapack.read_hashes/read_manifest`、`checkpoint.load_checkpoint`（在补上恢复流程前）。
* 单连接续传也做 `Content-Range` 校验（复用 `_content_range_ok`）。
* `_report_final` 里 `on_file_done` 只在成功时调用（或改名 `on_file_finished`）；`progress` 回调移出 `done_lock`。
* 槽位面板按磁盘策略的有效并发值显示（`SlotPanel.set_slots`）。
* 补齐开发者端往返测试（"构造最小 mrpack → 导出 → 检测类型 → 读 settings/whitelist/changelog → 解析"，覆盖"无 overrides"与"最小体积"两种用例）——本次两个 P0 都能被这类测试挡住。

---

## 五、大纲 vs 实现：偏差清单

| 大纲描述 | 实际 | 类型 |
|---|---|---|
| 更新包"失败文件移入单线程池重试" | 已改为**并行**耐心池（与主池重叠，G12） | 文档过时 |
| 应用"执行 copy / replace / delete（走回收站）" | **只有 delete 走回收站**；目录级 replace 走 `rmtree` 不进回收站 | 实现不符 |
| "确认更新 → 锁定侧边栏" | 只锁了 more_panel；`app_state` 在下载阶段**未锁** | 实现不符 |
| "中途取消（应用）→ 弹强警告，确认后退出" | 仅"无需下载直接应用"这两条捷径**没有装守卫**，点 X 直接销毁 | 实现不符（条件性） |
| "中断恢复：无效则提示重新定位" | 更新包失效时记录被**静默删除**，重定位分支不可达 | 实现不符 |
| "更新后校验：确认 copy 项就位、replace 目录存在" | 与实现一致（但只查存在性，不查内容） | 一致（偏弱） |
| "导出选项勾选 → 写回数据库" | 只在点"导出"时才写库 | 实现不符 |
| "按需签名" | 签名写入但**全链路无人校验** | 实际无效 |
| "预存 overrides 结构 / 最小体积" | `precompute_overrides` 无产物；`min_size` 对 index 文件无效 | 实现不符/无效 |
| "定位整合包……读取名称/版本/图标/模组数，显示信息卡" | 信息卡**不显示模组数**；定位失败**无日志** | 部分不符 |
| "大屏预览用独立进程" | ✅ 属实 | 一致 |
| "重新扫描：不重解压、不重 diff，毫秒级" | ✅ 属实 | 一致 |
| "清理缓存：cache/checkpoints/temp，占用文件跳过" | ✅ 属实（但管不到系统 temp 与 `.pulses_trash`） | 一致（覆盖面不足） |
| "状态流转：……处理中 → 完成" | **没有"完成"态**：完成后按钮回到「确认更新」，可重复执行 | 实现不符 |

---

## 六、本次审查的复现方式

* **实验 1（目录误删）**：构造 `instance/config/{shipped,user-only}.toml` 与 `overrides/config/shipped.toml`（内容相同、mtime 不同）→ `differ.diff_packs_parallel(whitelist=["mods"])` → `updater.build_plan(checked={"config":True}, strategies={"config":"完全匹配"})` → `updater.execute_plan(use_trash=True)` → 观察 `config/` 内容与 `trash_dir`。
* **实验 2（元数据误写）**：造一个只有 `modrinth.index.json`、没有 `overrides/` 的源包 → `eapack.export_eapack` → 检查包内条目与"玩家端 `_overrides_root` 回退"结果。
* 两个实验都在 `tempfile.mkdtemp()` 内完成，脚本退出时已 `shutil.rmtree` 清理；**未改动项目任何文件**。

> 审查范围声明：本次只读审查仅访问 `/home/admin/NS/pulses_easier`；未读取、未修改工作区内其他项目目录（`ysm/`、`openysm/`、`YesSteveModel-Native/` 等），未做任何 git 操作。

---

## 附录：修复状态（v0.5.0）

> 本报告的问题清单已在 **v0.5.0** 中逐条处理。下表给出每条的落点，
> 便于对照复查。结论分三类：**已修复** / **按设计不做** / **报告有误**。

### P0（6/6 已修复）

| 编号 | 状态 | 落点 |
|---|---|---|
| P0-1 非白名单目录被 rmtree | 已修复 | `differ.folder_hash` 去掉 mtime（两级内容判定）；`config.DEFAULT_STRATEGY_BY_DIR` 作为唯一默认策略来源；`updater._replace_dir_full` 事务性替换 |
| P0-2 元数据写进整合包根目录 | 已修复 | `eapack` 无条件写 `overrides/` 条目；`mrpack.RESERVED_ROOT_NAMES` + 玩家端回退分支排除 |
| P0-3 下载阶段未加锁 | 已修复 | `_start_download_and_apply` 里 `app_state.lock()`；drop/清空/重扫加 `_phase` 守卫；`_reset_pack_state` 先 abort+join |
| P0-4 两个 execute_plan 并发 | 已修复 | `_start_apply` 重入保护；「全部跳过」先 abort 并等线程结束 |
| P0-5 失败后无终态 | 已修复 | 新增 `_PHASE_MANUAL` 等待补入态 + `DownloadPanel.on_all_resolved` 回调 |
| P0-6 直通应用无关闭守卫 | 已修复 | `_install_close_guard()` 移进 `_start_apply` |

### P1（12/12 已处理）

| 编号 | 状态 | 落点 |
|---|---|---|
| P1-1 无回滚闭环 | 部分按设计不做 | 保留**失败时事务性回滚** + 检查点用于"发现上次没跑完"（启动提示，不动文件）。**不提供手动回滚**：向后恢复需假设"更新后用户什么都没改过"，无法验证，容易把版本搞乱；恢复方向统一为"重新拖入更新包再更新"。`verify_after_update` 升级为内容级校验 |
| P1-2 resume 记录被静默删除 | 已修复 | `scan_all_resumes` 保留失效记录 30 天；原子写 |
| P1-3 换包不失效旧 diff | 已修复 | `_on_modpack_changed` / `_on_update_pack_dropped` 强制作废 |
| P1-4 缓存目录 + 整目录并入 | 已修复 | 目录名带指纹；只并入本次任务清单里的文件 |
| P1-5 系统临时目录残留 | 已修复 | 工作目录改到 `<db>/cache/temp`；启动清理 + 退出清理 + 应用后丢弃合并副本 |
| P1-6 成功后不清 resume | 已修复 | `_on_apply_done` 零失败时 `clear_resume` |
| P1-7 有失败仍显示"完成 ✓" | 已修复 | 按结果选文案/颜色 |
| P1-8 `_completed_files` 偏低 | 已修复 | `on_file_done` 改为只在成功时触发，实时回填 |
| P1-9 扫描期无守卫 | 已修复 | 局部快照 + 新增 `scan` 阶段 |
| P1-10 清理与在写线程竞争 | 已修复 | 线程未停则不清理 |
| P1-11 index 条目永不应用 | 已修复 | `merge_files` 保留任意路径的 index 条目 |
| P1-12 非法路径 | 已修复 | `is_safe_rel_path` 在解析期丢弃 |

### P2（已处理，逐条落点）

* **三套重复判定** → 新增 `app/core/apply_rules.py`，`updater` / `change_list` /
  `player_view` 共用；`tests/test_polish.py::TestApplyRulesAgree` 穷举三种策略 ×
  三种变更 × 勾选与否，逐一比对三条路径结果一致。
* **根目录同名文件恒判 MODIFIED / 丢弃 index 根文件哈希** → 已修复
  （`_split_index_by_top` 增加 root_files；根文件按内容比对）。
* **单连接续传不校验 `Content-Range`** → 已修复（`_content_range_start_ok`）。
* **`.part` 清理口径不一** → 统一为 `*.part` + `*.part.meta`
  （`cache.clean_orphan_parts`、`resume.cleanup_stale_parts`）。
* **配置/断点非原子写** → `database` / `resume` / `checkpoint` 全部 tmp + `os.replace`。
* **死配置项** → `log_max_lines`（日志裁剪）、`progress_throttle_ms`
  （字节级进度节流）、`hash_threads`（比对线程数）已接上；
  `defer_to_single_after` 已无实现，连同 UI 项一起删除。
* **死代码** → 删除 `pack_builder.py`、`metadata.py`、`option_panel.py`、
  `md_viewer.py`、`change_tree.py`、`checkpoint.load_checkpoint`、
  `trash.py`（整模块）、`config.META_*` / `DEFAULT_CHECKED`；
  新增 `tests/test_polish.py::TestDeadCodeRemoved` 防止回潮。
* **`os._exit(0)` 跳过清理** → `main.py` 在退出前调用 `on_exit_cleanup()`，
  启动时另有 `clean_orphan_workdirs()` 兜底。
* **槽位面板显示配置值** → 改为显示磁盘策略限制后的**有效并发**。
* **开发者端**：解析失败清状态、解析期加锁、解压移出主线程、
  黑名单首次生效（`set_blacklist` 会刷新已建行）、版本取自源包、
  导出后清理临时目录、勾选即写库、导入配置恢复白名单/导出选项、
  接受 `.eapack`、策略默认值走 `config`。
* **eapack**：原子写、index 复制失败致命、`min_size` 过滤 index 条目、
  签名改为**可校验**的完整性校验（`verify_signature`）、
  `include_hashes` / `precompute_overrides` 的产物被玩家端真正消费
  （`ea_hashes.files` → 期望哈希，新增 blake2b 通道；
  `ea_hashes.folders` → 顶层目录期望哈希，省一半读取）。
* **大屏预览固定临时文件名 + 子进程失败静默** → 唯一文件名 +
  看护线程失败自动浏览器兜底。
* **`_resume_after_scan` 残留** → 早退时清标记。
* **`_phase` 无 scan 态** → 已加。
* **中止分支不恢复按钮** → 已恢复。
* **`checked` 默认值三处不一致** → 统一为"缺省未勾选 = 不应用"
  （空映射时按需处理，兼容尚未建表的路径）。

### 报告有误 / 未做

| 项 | 说明 |
|---|---|
| `_reset_pack_state` 不复位 `_btn_state` | **报告有误**：该函数末尾会调用 `_set_button_state(_BTN_DISABLED)`，`_btn_state` 已被复位 |
| 开发者端"取消导出" | **未做**：导出已改原子写，中途关闭只会留一个 `.part-eapack` 临时文件，不会产生半成品包 |
| 开发者端关闭守卫 | **未做**：同上，原子写已消除损坏风险；加守卫会与玩家端争 `WM_DELETE_WINDOW`，收益不足 |
| 自动更新机制 | **未做**：超出本次范围 |
