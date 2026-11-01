# Easier 开发者端「一键制作更新包」方案（PCL 导出引擎移植）

> 目标：开发者**只定位整合包文件夹**，在 Easier 里选好内容与策略，一次点完就产出 `.eapack`。
> 不再需要「先用 PCL 导出整合包 → 再拖进 Easier」。
>
> 依据：`docs/PCL-整合包导出-技术规范.md`（PCL 2.13.1.1 实测源码 + PCL-CE 交叉核对）。

---

## 0. 决策（已定）

| 项 | 结论 |
|---|---|
| 联网查询 | **CurseForge 优先**，未命中的再查 Modrinth（CF 一般更快，两者互补能把包体压到最小） |
| CurseForge API Key | 代码里给默认值（已实测可用，见 §10），**可用本地配置覆盖**；换 key 不用发版 |
| 打包启动器 | 不打包（PCL 那套 `IncludePCL` 整段跳过） |
| 导出内容 | **与策略无关**：只由"范围（目录勾选）+ 规则"决定。策略随包下发，玩家端生效 |
| 产物 | 只出 `.eapack`（更新包是轻量增量，不是完整整合包；不产出 `.mrpack`） |
| 导出选项默认 | `precompute_overrides`、`min_size` **默认开**（预存 overrides 哈希、最小体积） |
| 预设配置 | 只管**策略**（导出/更新通用），**不含任何设置项**；支持拖入「更新策略」栏应用 |

---

## 1. 与 PCL 的流程对照

| PCL 步骤 | Easier 对应 | 说明 |
|---|---|---|
| 选「导出内容」勾选项 | 开发者端的**范围表 + 策略胶囊** | 底层仍展开成 PCL 规则串，语义完全一致 |
| 按规则复制到 `overrides/` | 扫描阶段只**建清单**，不复制 | 省一次全量拷贝；打包时再按清单取文件 |
| 判定「可联网资源」 | 同一套条件（扩展名 + 路径子串） | 见规范 §2 |
| Modrinth / CurseForge 查询 | **CF 先、MR 后**，各自批量 + 缓存 | PCL 是并行两路；我们串行以便 CF 优先 |
| 写 `modrinth.index.json` | 新增 `index_build.py`，字段逐字对齐 | 含 `sha1/sha512/downloads/fileSize` |
| 压 zip | 交给现有 `export_eapack` 的写入器 | 复用压缩/签名/进度/校验 |

**刻意不同的两点**（都在文档里标注了理由）：
1. PCL 先把文件复制到 overrides 再算哈希；Easier 直接对源文件算哈希，打包时再读一次（少一次全量拷贝，SSD 上更快，机械盘上顺序读也不亏）。
2. PCL 命中后 `File.Delete()` 删 overrides 里的副本；Easier 从不复制命中项，天然不会双份。

---

## 2. 策略在「导出侧」的语义（必须先定死）

Easier 现有四档策略是给**更新**用的，导出侧要给出等价含义，否则开发者会误用：

**四档策略在导出侧不改变文件集合**——哪些文件重名只有玩家端才知道，导出时按策略裁剪会漏文件。

| 策略 | 导出侧行为 | 玩家端行为 |
|---|---|---|
| 完全匹配 | 全量导出 | 按清单覆盖同名文件 |
| 替换重名 | 全量导出 | 同名以包为准 |
| 跳过重名 | 全量导出 | 同名保留玩家本地版本 |
| 智能剔除 | 全量导出 | 命中剔除表的模组 config 不动 |

因此导出页只需要：**目录级 导出/不导出** + 策略胶囊（记录用）+ 根散文件的勾选。

---

## 3. 流程（四阶段，全程可中止）

```
① 扫描（本地，秒级）
   定位实例根 → 读 loader/版本 → 按规则展开条目 → 标出"可联网候选"
        ↓ 用户在这里配置（范围 / 策略 / 版本号 / 更新日志 / 选项）
② 指纹（本地 CPU）
   sha1 + sha512（所有文件）+ murmur2（仅候选）
   带缓存：path+mtime+size → 三个哈希
        ↓
③ 联网匹配（网络）
   CF：POST /v1/fingerprints/432（x-api-key，500/批）
   未命中 → MR：POST /v2/version_files（sha1，500/批）
   仍未命中 → local（进 overrides）
        ↓
④ 组装 + 打包
   写 modrinth.index.json（CF 链接在前，MR 在后）
   命中项进 files[]；未命中项进 overrides/
   → 交给 export_eapack 写入压缩/签名/清单 → .eapack
```

进度事件沿用现有 `bus.progress("export", …)`，UI 显示：`扫描 120/1200 · 联网 380/1200（CF 210 / MR 170）· 打包 0.42`。

---

## 4. 模块划分（Easier 侧）

| 新模块 | 职责 | 依赖 |
|---|---|---|
| `app/core/export_rules.py` | PCL 规则引擎（`!`/通配/后者覆盖/目录补 `*`）+ 默认规则集（转录 PCL 的选项组，含中文标题与默认勾选） | 纯函数，无 IO |
| `app/core/instance_scan.py` | 定位实例、探测 loader（`versions/<名>.json` 的 `inheritsFrom` + `PCL/Setup.ini`）、遍历 + 规则判定 + 候选分类 + 大小统计 | `pack_locator`、`export_rules` |
| `app/core/fingerprint.py` | sha1/sha512/murmur2（**去 `\t\n\r空格`**）+ 哈希缓存（json，原子写） | 标准库 |
| `app/core/lookup.py` | CF / MR 客户端：批量、限流（500/批 + 200ms 间隔 + 429 退避）、失败降级、结果缓存、可中止 | `urllib`（已有下载器基建可复用） |
| `app/core/index_build.py` | 生成 `modrinth.index.json`（字段/顺序/依赖） | `fingerprint`、`lookup` |
| `app/core/eapack.py` | 新增 `export_from_instance(...)` 编排 ①②③④；旧的 `export_eapack` 保留给「导入 zip」路径 | 上述全部 |
| `app/ui_web/dev.py` | 新状态机：`idle → scanned → configured → matching → packing → done`；新增 `scan / configure / start_build / abort` | `eapack` |
| `app/ui_web/api.py` | 新 invoke 方法：`dev.scan`、`dev.scope.set`、`dev.strategy.set`（已有）、`dev.build`、`dev.abort` | — |
| `web/src/components/DeveloperView.tsx` | 四步式界面（见 §6） | — |
| `web/src/components/ExportScopeCard.tsx` | 范围表（复用 StrategyCard 的视觉：名称 + 点线 + 胶囊） | — |

---

## 5. 数据契约（前后端）

```ts
// 扫描结果（一次返回，条目可能上千，做上限 20k）
interface DevScan {
  root: string;                 // 实例根
  packName: string; version: string;
  loader: { type: 'fabric' | 'forge' | 'neoforge' | 'vanilla'; version: string };
  minecraft: string;
  rows: Array<{                 // 行 = 顶层目录 / 根散文件组
    name: string; display: string; isFile: boolean;
    strategy: string;           // 完全匹配 / 替换重名 / 跳过重名 / 智能剔除
    excluded: boolean;
    files: number; bytes: number;
    candidates: number;         // 可联网候选数
    children?: Array<{ path: string; files: number; bytes: number; excluded: boolean }>;
  }>;
  skipped: Array<{ path: string; reason: string }>;   // 规则跳过/隐私文件
}

interface DevBuildProgress {
  phase: 'fingerprint' | 'lookup' | 'pack';
  done: number; total: number; label: string;
  hitCf: number; hitMr: number; local: number;
  savedBytes: number;          // 预计比全量本地打包少多少
}
```

---

## 6. 界面（开发者端，文字草图）

```
┌ ① 定位整合包 ────────────────────────────────┐
│ [选择文件夹…]  Pulses-Easier · 1.20.1 Fabric │
└──────────────────────────────────────────────┘
┌ ② 选择内容（策略） ─────────────────────────┐
│ mods            ──○ 智能剔除 ○ 完全匹配 …   │   ← 复用现有策略胶囊
│ config          ──○ 智能剔除 ○ 完全匹配 …   │
│ resourcepacks   ──○ 完全匹配 ○ 跳过重名      │
│ …                                             │
│ [统一策略]  [导入 PCL 配置]  [导出配置]      │
└──────────────────────────────────────────────┘
┌ ③ 联网匹配 ─────────────────────────────────┐
│ CF 命中 210 · MR 命中 170 · 本地 820         │
│ 预计包体 42.1 MB（全量 380 MB）              │
└──────────────────────────────────────────────┘
┌ ④ 导出 ─────────────────────────────────────┐
│ 版本号 [2026.11.1]  更新日志 [……]            │
│ 选项：☑ 写入哈希清单 ☑ 防篡改 ☑ 压缩         │
│ [开始制作]                        [取消]      │
└──────────────────────────────────────────────┘
```

---

## 7. 正确性保障（这块出错就完蛋，所以逐条列）

1. **规则引擎**用 PCL 的测试向量做单测：顺序覆盖、`!` 取反、目录补 `*`、大小写、`[abc]` 通配、`#`/`=` 注释。
2. **murmur2** 与 PCL 对齐：用 3 个真实 jar（含二进制/空白字节）对比 PCL-CE 的 `MurmurHash2Provider` 输出（同一份测试向量写进单测）。
3. **sha1/sha512** 小写十六进制；与 index 里 Modrinth 的比对要求一致。
4. **CF 命中判定**：`exactMatches[].file.fileFingerprint == 本地 murmur2` 且 `downloadUrl` 非空。
5. **MR 命中判定**：返回值里 `files[]` 逐项找 `hashes.sha1 == 本地 sha1`，找到才用（不能只看 key 命中）。
6. **路径**：一律相对 overrides、`/` 分隔；导出前校验不含 `..`、不越界、大小写冲突只保留一份并告警。
7. **不双份**：命中项不得同时出现在 `files[]` 与 `overrides/`（打包后自检）。
8. **回环测试**：导出的 `.eapack` 用玩家端 `build_plan` 跑一遍（空实例），断言 index 可解析、哈希一致、文件数吻合。
9. **失败降级**：CF 403/429/超时 → 记日志、跳过；MR 同理；**联网失败不阻断导出**，全走本地打包（与 PCL 的「继续=直接打包」一致）。
10. **可复现**：同样的输入 + 同样的缓存 → 输出字节级一致（时间戳/顺序固定）；缓存损坏自动重建。

---

## 8. 里程碑

| 阶段 | 内容 | 验收 |
|---|---|---|
| M1 | `export_rules` + `instance_scan` + 单测（无网络） | 扫描结果与 PCL 导出的规则命中一致（用同一个实例做对照） |
| M2 | `fingerprint` + 缓存 + `lookup`（CF/MR）+ mock 测试 | 指纹与 PCL-CE 一致；429/超时降级正确 |
| M3 | `index_build` + `eapack.export_from_instance` + 回环测试 | 导出的包能被玩家端完整安装；index 字段与 PCL 逐字对齐 |
| M4 | 开发者端 UI + 进度 + 中止 | 本地浏览器 e2e 全绿（新增 30+ 断言） |
| M5 | Windows 真机 e2e（一次） | 打包后的 exe 里跑完整流程 |

---

## 9. 已确认的决策（2026-11-01）

1. **CF key**：代码留默认值 + 本地配置可覆盖（不写进仓库文档/日志）。
2. **不产出 `.mrpack`**：更新包是轻量增量。
3. **策略不裁剪内容**：四档策略全部全量导出，只记录并随包下发。
4. **不做 PCL 配置导入**：用我们自己的预设；预设**只含策略**、可拖入「更新策略」栏应用。

## 10. 联网接口实测（2026-11-01）

用真实文件 `sodium-fabric-0.9.3-beta.1+mc26.3.jar`（1,912,496 B，sha1 `1e6aec74…`）验证：

| 接口 | 结果 |
|---|---|
| `GET https://api.curseforge.com/v1/games`（带 `x-api-key`） | 200 |
| `POST /v1/fingerprints/432`，指纹 `0x43A9A76D`（= 1135191917） | 200，`exactMatches` 命中 `Sodium 0.9.3-beta.1 for Fabric 26.3`，`downloadUrl=https://edge.forgecdn.net/files/9195/226/…` |
| `POST https://api.modrinth.com/v2/version_files`（sha1） | 200，返回同版本，`files[].hashes.sha1` 与本地一致 |

结论：**murmur2 实现（去 `\t\n\r空格`，seed=1）与 CurseForge 完全对得上**；CF 返回的 `edge.forgecdn.net` 链接优先于 Modrinth 的 `cdn.modrinth.com`，符合"CF 优先"的目标。
