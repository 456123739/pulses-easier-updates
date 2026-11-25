# 真实测试标准物：Minerizon beta

以后导出/压缩/增量/加载器相关改动，优先用它跑真实数据，不再用合成 jar 包。

## 资产

- 原始 zip：`/vol2/@appshare/Harness/.dsh/attachments/v1/files/09/09f9033a.../Minerizon beta.zip`
- SHA-256：`09f9033a9f7589cc527d226647faa6a4ff2bda2582d326f0d6b1a879977fa498`
- 解压目录（标准实例根）：`/vol2/@appshare/Harness/fixtures/MinerizonBeta/Minerizon beta`
- 文件清单：`tests/fixtures/minerizon-beta.files.txt`
- 摘要：`tests/fixtures/minerizon-beta.json`
- PCL 可比对夹具（不含 saves/PCL/皮肤缓存）：GitHub release `fixture-minerizon-beta-1`，`minerizon-pcl-fixture.zip`

## 实测基线（本机，2026-11-23）

- 源：2,785 文件 / 1,762,512,837 B，NeoForge 21.1.219 / MC 1.21.1
- 默认导出范围：838 文件 / 352 MB（saves 默认不选，符合 PCL 默认）
- 旧版 ZIP：159,019,122 B
- Osmium EAPK2（super）：141,069,693 B（−11.3%），本机约 61 s，属于 beta 慢速档
- 默认导出（CF 指纹限制 4 MB）：Windows CI 7.48 s；PCL 9.00 s（同机同夹具）
- 完整性：PCL 内层 mrpack 840 个路径，我方 838 个；仅缺 PCL 自身的
  `PCL/Setup.ini`、`PCL/config.json`、`command_history.txt`，多一个
  `CustomSkinLoader/CustomSkinLoader.json`（我方智能配置）。
