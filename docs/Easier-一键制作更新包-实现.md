# 一键制作更新包：实现说明

> 目标：开发者不必先用启动器导出整合包，直接从本地整合包目录生成 `.eapack`。
> 规则与 PCL2「导出整合包」逐条对齐，依据见 `docs/PCL-整合包导出-技术规范.md`。

## 1. 模块

| 模块 | 职责 |
| --- | --- |
| `app/core/export_rules.py` | 规则引擎：VB `Like` → 正则、靠后规则覆盖靠前、目录补 `*`、PCL 默认选项组与固定排除段 |
| `app/core/instance_scan.py` | 遍历实例、按规则判定去留、分类候选、探测加载器与版本 |
| `app/core/fingerprint.py` | 一次读盘算 sha1 / sha512 / CurseForge murmur2；带磁盘缓存 |
| `app/core/lookup.py` | CurseForge `fingerprints/432` + Modrinth `version_files`，CF 优先、MR 兜底、失败降级 |
| `app/core/index_build.py` | 生成 `modrinth.index.json`（字段顺序与语义按 PCL） |
| `app/core/instance_export.py` | 编排：扫描 → 指纹 → 查询 → index → overrides 暂存（硬链接）→ 打 ZIP |
| `app/ui_web/dev.py` | 开发者流程：`mode="instance"` 与 zip 模式并存，状态走快照 |

## 2. 判定要点

- 规则是**最后一条命中的规则决定去留**，不是“命中即停”。
- 目录规则（`mods\`）自动补星号 → `mods\*`。
- 固定追加在最后的排除段：`!*.log`、`!*.dat_old`、`!*.BakaCoreInfo`、`!hmclversion.cfg`、`!log4j2.xml`。
- 实例根下的 `assets/`、`versions/`、`libraries/` 永不扫描；任意层级的 `.fabric/`、`.git/`、`cache` 类目录跳过。
- 候选（会去联网查直链）＝后缀属于 `.zip/.rar/.jar/.disabled/.old` 且路径含 `mods`/`packs`/`openloader`/`resource`。
- 命中直链的文件**只写 index**，不进 overrides；查不到的原样打包。
- `options.txt` 属于 `NEVER_PACK_FILES`：即使规则包含也不进包。

## 3. 产物契约

`.eapack` 内：`modrinth.index.json`、`overrides/…`、`changelog.md`、`ea_settings.json`、`ea_hashes.json`、`ea_manifest.json`。
玩家端更新流程**不需要任何改动**。

`ea_manifest.json` 额外字段 `"source": "instance"` 用于区分来源。
`ea_settings.json.recommended_strategies` / `whitelist` / `excluded_folders` 与 zip 模式一致。

## 4. 开发者端快照（`dev.state`）

新增：`mode`、`instance{root,name,minecraft,loader,loaderVersion,totals,rows}`、`ruleOptions[]`、`hasCfKey`。
新增桥接：`dev.instance.pick{path}`、`dev.rule.set{key,value}`、`dev.cfkey.set{key}`。

API Key 存本机应用配置（`cf_api_key`），不上传、不进仓库；为空时只走 Modrinth。

## 5. 已知取舍

- 查不到直链时**不猜**：宁可整包进 overrides（玩家端照样能更新），不写假下载地址。
- CurseForge 指纹需要先知道“过滤空白后的长度”，因此 >32MB 的文件会读两遍（一遍算 sha1/sha512/长度，一遍算 murmur2）。
- 暂存 overrides 用硬链接（同盘零拷贝），跨盘或文件系统不支持时自动退回复制。
- 导出线程只读实例目录，不修改玩家文件。

## 6. 开发者端交互（前端）

来源分段「启动器整合包 / 本地整合包目录」→ 选目录后显示实例摘要（包名、MC 版本、加载器+版本号、文件数、体积、候选资源数）。
「导出内容」是两级勾选，改动立刻重扫并更新下面的「更新范围」表；`options.txt` 等永不进包的文件不会出现在范围表里。
右侧是 CurseForge API Key（存本机应用配置，可留空）与导出选项；导出中可点「取消」（文件边界生效）。

## 7. 安全与健壮性（黑盒审查后补）

- **不静默丢文件**：只有真正写进 `index` 的文件才算"网络资源"；命中但没有可用 URL 的一律降级为本地打包。
- **不留残骸**：`*.part-<pid>-<tid>-<uuid>` / `*.stage-*` 在成功、取消、异常三条路径上都会清理。
- **完整性签名 v2**：逐条读包内内容算 sha1 再签名（`signature_version: 2`），能挡住"同 CRC 改写"；旧包（v1，CRC）仍按老算法校验。不含密钥：只能证明没被改坏，不能证明来源。
- **读不出来就失败**：任何文件读不出/暂存失败，导出直接中止（缺一个 mod 玩家就会崩，不能"跳过并报成功"）。
- **软链接边界**：只有实例根下一级、本身是软链接且指向外部的目录（mods 链到别的盘）允许跟进去；更深层的越界链接一律跳过并记进 `unsafeLinks`。
- **多版本不猜**：`.minecraft` 下有多个版本 json 时，只认同名/同 id 的那份，否则不写依赖（宁可留空也不写错）。
- **并发**：`DevFlow` 在同一把锁里做"忙检查 + 置位"，第二次点击直接报错；临时文件名带进程/线程/随机串。
- **软链接**：跟随目录软链接（Windows junction 常见），用 realpath 记账防环；不修改链接目标。
- **可取消**：`dev.abort` → 导出在下一个文件边界退出，不留临时文件。
- **状态一致**：导出开始立刻推 `phase=exporting`，导出中修改策略/选项/日志直接报错，不会被静默忽略。

## 7.5 玩家端配合的一处修复

只在 `modrinth.index.json` 里声明、`overrides/` 里没有同名文件的模组（模组全部走直链的包很常见），
以前玩家端「更新范围」里看不到、也不会下载。现在：

- `flow._build_tables()` 会把 index 声明的顶层目录/根散文件并进表；
- `start_scan()` 的清单比对条件从 `if manifest:` 改成 `if manifest or index_hashes:`。

回归用例：`tests/test_export_roundtrip.py::IndexDownloadCase`（本地 HTTP 服务 + 真实下载 + 字节比对）。

## 7.6 策略语义（别误解）

「完全匹配」= 以包为准，包里没有的文件会被删除；其它三档都不会删玩家文件。
导出只写**推荐**策略，玩家可以在界面上改。想保留玩家自装内容，推荐用「替换重名」。

## 8. 已知限制

- CurseForge 指纹（murmur2）是纯 Python，100MB 候选文件约 8.5MB/s；非候选文件只算 sha1/sha512（约 290MB/s）。超大模组会拖慢导出，可接受但不快。
- 只认 `mods/` + `config/` 同时存在的目录为整合包（`pack_locator` 的既有判据）；只有 mods 的目录会被拒绝。
- 导出线程不做下载：命中直链的文件只写 index，玩家端下载时才会真正拉取。
