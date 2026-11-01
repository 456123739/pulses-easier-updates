# PCL2 整合包导出流程 · 技术规范（供 Easier 开发者端移植）

来源：`/home/admin/NS/pcl_analysis/src/PCL2/Plain Craft Launcher 2/`
主要文件与行号：

| 内容 | 位置 |
|---|---|
| 导出主流程 | `Pages/PageInstance/PageInstanceExport.xaml.vb:421-733`（`StartExport`） |
| 规则引擎 | 同上 `:170-181`（`StandardizeLines`）、`:238-288`（`GetAllRules`） |
| 默认勾选项与规则表 | `Pages/PageInstance/PageInstanceExport.xaml:41-290` |
| 动态子项（资源包/存档/光影） | `PageInstanceExport.xaml.vb:54-93` |
| 资源指纹 | `Modules/Resource/LocalResourceFile.vb:188-266` |
| CurseForge URL 变体 | `Modules/Resource/ResourceVersion.vb:413-421` |
| 安装侧契约 | `Modules/Minecraft/ModModpack.vb:368-503`（`InstallPackModrinth`） |

本文只讲导出（含 index 撰写与联网查询），安装侧仅用于「反向校验格式是否被接受」。

---

## 0. 一句话流程

定位实例 → 按规则把文件**复制**到临时 `modpack/overrides/` → 对其中「可联网资源」
计算指纹并查询 Modrinth / CurseForge → 命中的写进 `modrinth.index.json` 并从 overrides 删掉
→ 未命中的留在 overrides → 打包（`index + overrides/` 直接成 .mrpack；含启动器时再套一层 zip）。

产物永远满足 Modrinth 规范：**根目录 `modrinth.index.json` + 同级 `overrides/` 目录**。

---

## 1. 导出内容：规则引擎

### 1.1 规则语法（必须逐条照抄）

- 一行一条规则，路径相对**实例目录**（`PathIndie`，即 `.minecraft/versions/<实例>/`）。
- `/` 一律归一成 `\`（`StandardizeLines:175`）。
- 以 `!` 开头表示**排除**（`Revert`），否则表示**包含**。
- **靠后的行覆盖靠前的行**（`PageInstanceExport.xaml.vb:517-521`：遍历全部规则，后者覆盖前者）。
- 支持 VB `Like` 通配：`*`、`?`、`[]`；比较前两边都 `Lower`（大小写不敏感）。
- 目录规则以 `\` 结尾时补 `*`（`AddSuffixStarToFolderPath=True`），即 `mods/` 等价于 `mods\*`。
- 空行、`#` 注释、`=` 开头行被忽略（`:173`）。

匹配伪代码（与 PCL 等价）：

```
shouldKeep = False
for rule in rules:                     # 顺序敏感
    if relative_path.lower like rule.trim_start('!').lower:
        shouldKeep = (not rule.startswith('!'))
```

注意：**不是"命中即停"**，而是"最后一条命中的规则决定结果"。

### 1.2 遍历时的硬性跳过项（`:499-508`）

- 仅在实例根目录：`assets`、`versions`、`libraries` 三个目录整棵跳过。
- 任何层级：`structureCacheV1`、`.fabric`、`.git`、`avatar-cache`、`cosmetic-cache`。
- 不做符号链接跟随的特殊处理（依赖 `DirectoryInfo` 行为）。

### 1.3 默认勾选项 → 规则表（XAML 原样）

基础（默认勾选）：
- 游戏本体设置：`options.txt|configureddefaults/`（默认开）
- 游戏本体个人信息：`hotbar.nbt|command_history.txt`（默认关）
- OptiFine 设置：`optionsof.txt|optionsshaders.txt`（需要 OptiFine）
- **Mod**：`mods/|!mods/*.disabled|!mods/*.old|!mods/.connector/|coremods/|lib/`（默认开，需要 Mod 加载器）
  - 子项「已禁用的 Mod」：`mods/*.disabled|mods/*.old`（默认关）
  - 子项「整合包重要数据」：`hotai/|bansoukou/|addons/|multiblocked/|modpack-update-checker/|global_packs/|global_resource_packs/|global_data_packs/|optional_data_packs/|moonlight-global-datapacks/|maps/|icon.png|mods-resourcepacks/|matmos/|resource_assorts/|resource_assorts.json|patchouli_books/|datapacks/|kubejs*/|!kubejs*/probe/|!kubejs*/exported/|!kubejs*/jsconfig.json|!kubejs*/README.txt|openloader/|worldshape/|resources/|scripts/|structures/|fontfiles/|oresources/|packmenu/|craftpresence/|pointblanks/|template*/|!template*/playerdata/|!template*/stats/`（默认开）
  - 子项「Mod 设置」：`config/|!config/jei/world/|!config/worldedit/|config/worldedit/worldedit.properties|!config/spark/|config/spark/config.json|defaultconfigs/|journeymap/config/|journeymap/server/|TrashSlotSaveState.json|customfov.txt|gg.essential.mod/|essential/|!essential/*/|!essential/*.jar*|!essential/screenshot-checksum-caches.json|!essential/microsoft_accounts.json|paragliderSettings.nbt|local/client_config.json|local/ftbl.json|local/client/sidebar_buttons.json|local/client/ftbutilities.cfg|local/client/ftblib.cfg|local/client/xencraft.cfg|liteloader.properties|default_reference.xml|CustomSkinLoader/CustomSkinLoader.json|!config/tacz/custom`（默认开）
  - 子项「TaCZ 枪包」：`tacz/|config/tacz/custom/`（默认开）
  - 子项「已上传的沉浸画」：`immersive_paintings/`（默认开）
  - 子项「已绘制的地图」：`journeymap/data/|xaero/|XaeroWaypoints/|XaeroWorldMap/`（默认关）
  - 子项「JEI 个人信息」：`config/jei/world/`（默认关）
  - 子项「EMI 个人信息」：`emi.json`（默认关）
  - 子项「帕秋莉手册个人信息」：`patchouli_data.json`（默认关）
- 资源包：`resourcepacks/|texturepacks/`（ShowRules，动态子项）
- 光影包：`shaderpacks/`（ShowRules，动态子项，需要 Mod 加载器或 OptiFine）
  - 子项「光影包设置」：动态生成 `shaderpacks/<已勾选光影包名>.txt`
- 截图：`screenshots/`（默认关）
- 导出的结构：`schematics/`（默认关）
- 录像回放：`replay_recordings/|replay_videos/`（默认关）
- 单人存档：`saves/`（ShowRules，动态子项，默认关）
- 协议：`LICEN*`（默认开）
- 多人服务器列表：`servers.dat`（默认关）

固定追加的排除段（`:272-279`，永远在最后，优先级最高）：
```
!*.log
!*.dat_old
!*.BakaCoreInfo
!hmclversion.cfg
!log4j2.xml
```

### 1.4 动态子项（`:54-93`）

对 `resourcepacks`+`texturepacks`、`saves`、`shaderpacks`：
- 目录不存在则跳过；
- 压缩包子项：`*.zip`、`*.rar`，一条规则 `文件夹/文件名`（Like 转义），默认勾选；
- 文件夹子项：非空目录，一条规则 `文件夹/子目录名/`，默认勾选；`saves` 按 `LastWriteTime` 倒序；
- 黑名单（名字含即跳过）：`Quark Programmer Art.zip`、`+ EuphoriaPatches_`、`PCL2 Skin.zip`。

### 1.5 追加内容（ExtraFiles，`:289-308`、`:548-565`）

- 每行一个**完整路径**；以 `\` 结尾代表文件夹，否则代表文件。
- 复制到「包根目录」：不含 PCL 时目标为 `modpack\`，含 PCL 时为 `CacheFolder\`（即外层 zip 根）。
- 找不到只提示，不中断。

---

## 2. 复制与资源探查（最关键的一步）

复制阶段（`:490-540`）遍历实例目录，逐文件：

1. 计算 `RelativePath = FullName.AfterFirst(PathIndie)`（相对实例根）。
2. 走 1.1 的规则判定，不通过就跳过。
3. 复制到 `OverridesFolder + RelativePath`（overrides 下保持原相对路径）。
4. **分类为可联网资源**，条件（`:527-532`）：
   - 扩展名 ∈ `{.zip, .rar, .jar, .disabled, .old}`，且
   - 相对路径包含 `mods`、`packs`、`openloader`、`resource` 任一子串。
   - 命中则 `New LocalResourceFile(TargetPath)`（对**复制后的文件**计算指纹，提前触发两个哈希）。

> 含义：`resourcepacks/`、`shaderpacks/`（含 "packs"）、`mods/`、`openloader/`、`resources/` 下的压缩包都会去查；
> 其余文件（config、脚本、kubejs 等）一律留在 overrides，不上传、不查。

### 2.1 两个指纹（`LocalResourceFile.vb:188-266`）

**ModrinthHash = 文件 SHA1（小写十六进制）**，直接对整文件算。

**CurseForgeHash = MurmurHash2（seed = 1），输入是"去掉空白字节"的文件内容**：

```
data = [b for b in file_bytes if b not in (0x09, 0x0A, 0x0D, 0x20)]   # \t \n \r 空格
length = len(data); h = (1 ^ length) & 0xFFFFFFFF
for i in range(0, length - 3, 4):
    k = data[i] | data[i+1]<<8 | data[i+2]<<16 | data[i+3]<<24
    k = (k * 0x5BD1E995) & 0xFFFFFFFF
    k ^= k >> 24
    k = (k * 0x5BD1E995) & 0xFFFFFFFF
    h = (h * 0x5BD1E995) & 0xFFFFFFFF
    h ^= k
# 收尾 3/2/1 字节（见源码 :216-228），再 h ^= h>>13; h = h*M; h ^= h>>15
```

缓存：`Cache/ModHash.ini`，键 = `stable_hash(启用后完整路径 + LastWriteTime.ToLongTimeString + Length + "-M"/"-C")`。
注意 `.disabled` / `.old` 用「启用后名字」(`EnabledFullName`) 做缓存键，但**哈希算的是文件本身字节**。

---

## 3. 联网查询引擎（重点）

前置开关：
- `CheckHostedAssets = Not CheckAdvancedInclude.Checked` —— 勾了「打包资源文件」就整段跳过联网（全部塞进 overrides）。
- `ModrinthUploadMode = CheckAdvancedModrinth.Checked` —— 勾了「仅从 Modrinth 下载」就不查 CurseForge，未命中的文件直接打包。

两个平台**并行**查询（两个线程），伪代码：

### 3.1 Modrinth（`:592-612`）

```
POST https://api.modrinth.com/v2/version_files
Content-Type: application/json
{"hashes": ["<sha1>", ...], "algorithm": "sha1"}

响应：{ "<sha1>": { ...version 对象... }, ... }
```

对每个本地文件：
1. `if sha1 not in response: continue`（未命中）
2. **校验**：`response[sha1].files[0].hashes.sha1 == 本地 sha1`，不等则跳过。
   （PCL 只看 `files[0]`；更稳的做法是遍历 `files[]` 找 sha1 相同的那一项。）
3. 取 `files[0].url` 作为下载地址。

失败处理：异常只记录；两个平台都异常/失败 → 弹窗询问「是否继续导出？继续=所有文件直接打包」。

### 3.2 CurseForge（`:614-648`）

```
POST https://api.curseforge.com/v1/fingerprints/432
{"fingerprints": [<uint32>, ...]}          # 432 = Minecraft

响应：data.exactMatches[]，每项含 file 对象
```

对每个 `exactMatches[]`：
1. 必须有 `file.downloadUrl`（为空则跳过，CurseForge 对部分作者禁用了第三方下载）。
2. 用 `file.fileFingerprint` 反查本地文件（`FirstOrDefault(CFHash == fingerprint)`），查不到跳过。
3. 把 `downloadUrl` 经**URL 变体展开**后全部加入该文件的下载列表。

URL 变体（`ResourceVersion.vb:413-421`），按顺序、去重：

```
u1 = url.Replace("-service.overwolf.wtf", ".forgecdn.net").Replace("://edge.", "://mediafilez.").Replace("://media.", "://mediafilez.")
u2 = url.Replace("://edge.", "://mediafilez.").Replace("://media.", "://mediafilez.")
u3 = url.Replace("-service.overwolf.wtf", ".forgecdn.net")
u4 = url.Replace("://media.", "://edge.")
u5 = url
```

### 3.3 结果容器

`Dictionary(Of LocalResourceFile, List(Of String))`：文件 → 下载地址列表（Modrinth 结果 + CurseForge 变体都会 Add 进去）。
写 index 时这一层决定了「这个文件是联网下载，还是留在包里」。

---

## 4. 写 `modrinth.index.json`（字段级，必须逐字对齐）

`PageInstanceExport.xaml.vb:673-702`：

```json
{
  "game": "minecraft",
  "formatVersion": 1,
  "versionId": "<用户填的版本号，空则 1.0.0>",
  "name": "<用户填的整合包名，空则实例名>",
  "summary": "<实例显示名 McVersion.Info>",
  "files": [
    {
      "path": "mods/xxx.jar",
      "hashes": { "sha1": "<sha1>", "sha512": "<sha512>" },
      "downloads": [ "https://...", "..." ],
      "fileSize": 1234567
    }
  ],
  "dependencies": {
    "minecraft": "1.20.1",
    "forge": "47.2.0",            // 有才写
    "fabric-loader": "0.15.11",   // 有才写
    "neoforge": "20.6.98"         // 有才写
  }
}
```

细节（都容易踩）：

- `path`：`ModFile.File.FullName` 去掉 overrides 前缀后 `\` → `/`；**相对 overrides**，不带 `overrides/` 前缀。
- `hashes`：`sha1` = 上面算的 ModrinthHash；`sha512` = 现算（`CryptographyUtils.ComputeFileHash(..., Sha512)`）。
- `downloads`：`Pair.Value.OrderBy(Function(u) u.Contains("modrinth.com"))` —— 排序键 False 在前，
  即 **非 Modrinth 的地址排前面**（作者注释：不优先选择 Modrinth）。数组内容为此前收集的全部地址。
- `fileSize`：文件字节数。
- **不写 `env`**（安装端会读，导出端不产出）。
- **不产出 `client-overrides/`**（只有 `overrides/`）。
- 写完这一条后调用 `ModFile.File.Delete()` —— 把该文件从 overrides 里删掉，避免「既在 index 又在包里」双份。
  未命中联网的文件不在这个字典里，因此留在 overrides 中随包分发。
- JSON 缩进输出（`Formatting.Indented`），编码 UTF-8。

写入位置：`<CacheFolder>\modpack\modrinth.index.json`（与 `overrides/` 同级）。

---

## 5. 打包（两种形态）

- **不含 PCL**（`:714-716`）：
  `CreateZipFromDirectory(PackPath, CacheFolder & "modpack\")`
  → 目标 zip 的根就是 `modrinth.index.json` + `overrides/…`；扩展名可选 `.mrpack` 或 `.zip`。
- **含 PCL**（`:705-712`）：
  1. `CreateZipFromDirectory(CacheFolder & "modpack.mrpack", CacheFolder & "modpack\")`
  2. 删掉 `modpack\` 目录
  3. `CreateZipFromDirectory(PackPath, CacheFolder)` —— 外层 zip 含 `modpack.mrpack` + `Plain Craft Launcher.exe` + 可选 `PCL/` 资源
  这是 PCL 自己的「启动器整合包」格式，由 `InstallPackLauncherPack`（`ModModpack.vb:758+`）识别：
  解压后找 exe，按产品名判断是不是 PCL。

压缩实现不在本仓库快照里（`FileUtils.CreateZipFromDirectory` 定义缺失），按标准 `ZipArchive`（Deflate）实现即可；
条目路径一律 `/`，目录条目可选（PCL 的导入端两种都能处理）。

---

## 6. 安装侧契约（用来反向验收）

`ModModpack.vb:368-503`：

1. 读 `modrinth.index.json`（允许位于 `ArchiveBaseFolder` 下，即支持外层套目录）。
2. `dependencies.minecraft` 必须存在，否则拒绝安装。
3. `dependencies` 认得：`minecraft` / `forge` / `neoforge`（含 `neo-forge`）/ `fabric-loader`；其它名字 → 提示「不兼容的加载器」。
4. `files[]`：
   - `env.client == "optional"` → 询问是否下载；`"unsupported"` → 跳过。
   - 下载地址：`downloads[]` 逐个经 `ParseCurseForgeDownloadUrls` 展开 + `DlSourceModGet`（镜像改写）+ Modrinth 追踪参数，
     然后去重。
   - **只按 `hashes.sha1` 校验，不校验文件大小**（源码注释：Modrinth 官方说整合包不应校验大小）。
   - 目标路径必须仍在实例目录内，越界直接报错中止。
5. `overrides/` 与 `client-overrides/` 复制进实例根（`CopyOverrideDirectory`）。

---

## 7. 必须遵守的不变量（写实现前先背下来）

1. **最后一条命中规则决定去留**，不是第一条。
2. 目录规则补 `*`；比较大小写不敏感。
3. 只有「可联网资源」才进 index；其余必须在 overrides 里，**不能两边都丢**。
4. 进 index 的文件要从 overrides 删掉；未进 index 的文件必须留着。
5. `path` 相对 overrides、用 `/`。
6. Modrinth 用 **sha1** 查；CurseForge 用 **murmur2(seed=1, 去空白字节)** 查。
7. Modrinth 命中的前提是「返回对象的 sha1 与本地一致」，不能只信 key 命中。
8. CurseForge 必须有 `downloadUrl`，否则等于没命中（文件会被打包）。
9. `downloads` 顺序：非 Modrinth 在前。
10. `env` 不写；`client-overrides` 不产出。
11. 打包后不允许同一文件既在 `files[]` 又在 `overrides/`。

---

## 8. 与 Easier 现状的差距 & 移植方案

现状（`app/core/eapack.py`）：
- 我们的 `.eapack` 是「**以已有整合包 zip 为输入**」的再封装：沿用源包里的 `modrinth.index.json`，
  加 `ea_hashes.json` / `ea_meta.json` / `changelog.md` / 签名，再写 `overrides/`。
- 也就是说：**我们目前不会自己生成 index，也不会去联网查资源** —— 这正是 PCL 流程要补的两块。

要做成「定位整合包 → 一次点完」，开发者端需要新增（按依赖顺序）：

1. **扫描器**：遍历实例、应用规则（先用 PCL 默认规则集 + 允许用户改），产出「待导出文件清单」。
2. **分类器**：按 2 节的扩展名 + 路径子串判定可联网资源。
3. **指纹与查询**：sha1 / murmur2；Modrinth `version_files`、CurseForge `fingerprints/432`；
   并发、限速、失败降级（不阻断导出，只把文件留在 overrides）。
4. **index 生成**：按第 4 节字段写 `modrinth.index.json`（含 downloads 排序、sha512、fileSize）。
5. **打包**：`index + overrides/` → `.eapack`（我们自己的壳，内部仍是 mrpack 结构）；
   如未来要直接产出 `.mrpack`，同一份 index 直接复用。
6. **验收**：用第 6 节的安装契约做回环测试（导入自己导出的包，断言文件落地、策略、哈希）。

网络与合规注意：
- CurseForge 的 `fingerprints` 接口需要 API Key（PCL 内置了自己的 key；我们不能内置他人的 key）。
  → 方案：默认只走 Modrinth；CurseForge 作为「可选，需自备 API Key」。
- 离线可用性：查询失败不应阻塞导出（与 PCL 的「继续=直接打包」一致）。

---

---

## 9. 源码完整性核对（2026-11-01）

### 9.1 官方 tag 已拉全

- 仓库：`Meloong-Git/PCL`，tag **`2.13.1.1`**（与本项目分析用的版本一致）。
- 下载：`https://gh-proxy.com/https://github.com/Meloong-Git/PCL/archive/refs/tags/2.13.1.1.zip`（11.4 MB）
- 落盘：`pcl_analysis/src/PCL2-full-2.13.1.1/`（解压后 16 MB，112 个 `.vb`），压缩包留在同目录。
- 逐文件校验（sha256 前 12 位）——本仓库原先的裁剪快照与官方 tag **完全一致**：

| 文件 | 结论 |
|---|---|
| `Pages/PageInstance/PageInstanceExport.xaml.vb` | SAME |
| `Modules/Minecraft/ModModpack.vb` | SAME |
| `Modules/Resource/LocalResourceFile.vb` | SAME |
| `Modules/Minecraft/ModDownload.vb` | SAME |
| `Modules/Resource/ResourceVersion.vb` | SAME |

### 9.2 仍然拿不到的部分：MeloongCore

- `.vbproj` 里是 `ProjectReference ..\MeloongCore\Shared\MeloongCore.csproj`，而仓库里的 `MeloongCore/` 目录**为空**（未提交）。
- 因此 `FileUtils`（含 `CreateZipFromDirectory`）、`DirectoryUtils`、`PathUtils.AfterFirst`、`CryptographyUtils.ComputeFileHash`、`StringUtils.LikePatternEscape`
  在**官方源码里也不存在**——不是我们快照缺，是它闭源。
- 这些函数语义简单且从调用点可确定；需要精确到字节的地方（哈希大小写、zip 参数）用 PCL-CE 交叉核对，见 9.3。

### 9.3 PCL-CE 交叉核对（`PCL-Community/PCL-CE`，dev，commit `74c4e2d`，本地已全量：`pcl_analysis/src/PCL2-CE`）

| 待确认点 | CE 实现 | 结论 |
|---|---|---|
| 哈希十六进制大小写 | `Convert.ToHexString(bytes).ToLower()`（`PCL.Core/Utils/Exts/ByteExtension.cs:9`） | **小写**，与 index 里 Modrinth 的比对要求一致 |
| 打包方式 | `ZipFile.CreateFromDirectory(...)`（`PageInstanceExport.xaml.cs:1219/1224/1230`） | 标准 Deflate zip，条目路径 `/`，不额外写目录条目 |
| CurseForge 指纹 | `MurmurHash2Provider.ComputeHash` 先过滤 `9/10/13/32` 字节（`PCL.Core/Utils/Hash/MurmurHash2Provider.cs:17-23`） | 与 2.13.1.1 内联实现**一致** |
| index 字段 | path / hashes{sha1,sha512} / downloads / fileSize + dependencies | 与 2.13.1.1 **完全一致** |
| downloads 排序 | `OrderByDescending(u => u.Contains("modrinth.com"))`（CE 改成 Modrinth 优先） | 与 2.13.1.1 的 `OrderBy`（非 Modrinth 优先）**相反**；移植以 2.13.1.1 为准，或做成开关 |

> 结论：导出链路的 VB 源码已经完整拿到并核对；唯一闭源的是通用工具库，其关键语义已用 CE 钉死。
> 后面移植时可以放心按本文第 1–7 节实现，不需要再猜。
