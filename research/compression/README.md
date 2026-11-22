# 更新包压缩引擎研究（2026-11-22）

目标：在不改变旧 ZIP 兼容路径、不引入新依赖的前提下，给「超级压缩（beta）」
找真实、稳定的体积收益；所有结论以真实素材回读 sha256 一致为准。

## 素材与方法

* 素材：`/tmp/mcc-bench/src2`，226.6 MB / 450 文件（29 个 jar 172.3 MB、
  22 个 mca 25.6 MB、4 个 zip 16.0 MB、385 个 json 12.0 MB）。只读使用。
* 基线统一先做 mca 可逆变换（逐文件 `restore_region(blob) == 原字节` 自检），
  再按路径排序进入容器；jar 去壳实验则在同一份数据上替换 jar 的表示。
* 测量：输出体积、墙钟时间、`ru_maxrss` 峰值、全量回读 sha256（450 文件）。
* zstd 统一 `window_log=27 / LDM=1`（除非表中注明），块按表。
* 硬件/环境：本机 16 GB，Python 3.11，zstandard（venv）。

## 结果（同一份预变换数据、同排序、同机）

| 配置 | 体积 | 导出 | 峰值 RSS | 回读 |
|---|---:|---:|---:|---|
| 基线 mca B64 L17 | 185.52 MB | 62 s | 429 MB | — |
| mca B128 L17 | 184.82 MB | 68 s | 700 MB | — |
| mca B256 L17 | 183.53 MB | 75 s | 873 MB | — |
| mca B256 L19 | 182.64 MB | 126 s | 875 MB | — |
| mca B256 L22 | 182.57 MB | 174 s | 1235 MB | — |
| mca B256 L17 w29 | 181.07 MB | 61 s | 873 MB | — |
| mca B64 L17 无 LDM | 185.58 MB | 56 s | 412 MB | — |
| mca B256 按类型排序 | 183.51 MB | 109 s | 818 MB | — |
| **jar 去壳 B64 L17** | **154.16 MB (-16.9%)** | 95 s | 357 MB | 15 s，0 坏 |
| jar 去壳 B64 L19 | 152.44 MB | 186 s | 389 MB | 15 s，0 坏 |
| jar 去壳 B64 L22 | 150.64 MB | 262 s | 948 MB | 15 s，0 坏 |
| jar 去壳 B128 L17 w29 | 154.07 MB | 97 s | 506 MB | 15 s，0 坏 |
| jar 去壳 B128 L22 w29 | 149.89 MB | 301 s | 1162 MB | 15 s，0 坏 |
| xz preset6 + jar + mca | 150.35 MB | 180 s | 390 MB | 未计时（解码显著更慢） |

结论：

1. **块大小/LDM/排序的收益很小**：64→256 MB 省 1.1%，但内存 2 倍；
   window 29 在 256 MB 块上省 1.3%；按类型排序无收益。等级 19/22 在
   纯 mca 数据上只省 0.5%，与黑盒结论一致。
2. **JAR 去壳是唯一数量级收益**：-16.9%（L17）到 -18.8%（L22）。
3. L22 相对 L17 只再省 2.3%，但导出慢 2.7 倍、内存 948 MB；不建议正式档
   默认 22。L19 是中间点（再省 1.1%，慢 2 倍，389 MB）。
4. xz 只比 zstd L17+jar 小 2.5%，但解码慢、且多一个依赖/格式分支，不值得。
5. 无 LDM 与有 LDM 差 0.03%，排序无收益——不需要为这两项增加复杂度。

## JAR 可逆去壳

做法（与 mca 同思路）：

* 每个 entry 保留**原始 local header + 前置 gap**，去掉 deflate 外壳存 raw；
  末尾原样保留 central directory / EOCD。
* 还原时用 `zlib.compressobj(6, DEFLATED, -15)` 重压 raw，拼回原文件。
* **导出时逐 entry 验证重压结果与原始 deflate 字节完全一致**；任一 entry
  不一致（不同 zlib 版本/参数、加密、非 deflate）→ 整个 jar 原样存储。
* 实测 29 个 jar / 47,100 个 deflated entry：47,100/47,100 逐字节一致；
  其中 1 个签名 jar 还原后整文件 sha256 不变（签名不受影响）。
* 回读：29/29 jar sha256 一致，450 文件全量 sha256 0 坏；还原 29 个 jar 约 11–15 s。
* 稳定性：L17 候选重复两次均为 154.16 MB（95.2 s / 97.8 s，RSS 357/377 MB），
  体积无抖动；基线重复 62.4/78.7 s（缓存差异）。
* 集成：`PackWriter(level=22)` 走正常 `add_file` 路径自动产生 `transform="jar"`，
  回读 sha256 一致（小 jar + config 手工验证）。
* 风险：依赖 zlib 重压的可复现性——已用「导出时验证 + 整包 fallback」兜底；
  还原 CPU 增加（226 MB 包约 +15 s）；旧客户端不认识 `transform="jar"`，
  需要新版客户端（0.13.0 未发布，可在发布说明中标注）。

## 读取器加固（独立 commit 1561d2a）

* 索引/块 comp/raw 上限、header 偏移/长度、block_size、块数、条目数、
  逻辑大小、内联总量上限。
* 未知 transform、重复条目、inline 缺数据/长度不符、chunks 越界/总长与
  stored 不符 → `ContainerError`。
* zstd 帧 `content_size` 预检查 + `max_window_size`；解压后长度必须等于声明。
* 拒绝前不解压、不按声明分配内存；错误路径关闭文件句柄。
* `tests/test_container_validation.py` 19 项（伪造 header 均为小数据）。

## 建议

* 正式「超级压缩」：`jar_transform=True + level=17/19 + block 64 MB + w27`；
  体积约 -17%，导出约 1.5–2x，内存 357–389 MB。L22 仅作研究档。
* 若要接 switch：packwriter 的 super 分支传 `jar_transform=True` 并把 level
  定在 17/19；container 目前对 level>=20 自动开启 jar 变换，便于先验证。
* 不引入 xz/brotli；不改变旧 ZIP 路径；mca 仍由现有代码负责。

## 复现

* worktree：`/vol2/@appshare/Harness/easier-compression-lab/engine/worktree`
* 脚本：同目录 `research/compression/bench_container.py`（容器级）
* 原始日志：`/vol2/@appshare/Harness/easier-compression-lab/engine/out/batch*.log`
* 测试：`TMPDIR=/vol2/.../tmp PYTHONPATH=. pytest -q` → 725 passed, 1 skipped
