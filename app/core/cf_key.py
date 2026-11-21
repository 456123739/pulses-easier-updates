"""
cf_key.py — CurseForge API Key 的默认值与解析
------------------------------------------------
默认用 **PCL2 内置的那把**（NS 2026-11-08 明确要求：以前也是这么做的）。
用户自己在首选项里填了，就用用户的；没填就用内置，开箱即用。

⚠️ 两句实话：
1. 主仓库是 public，这把 key 写在这里等于公开（PCL 的 key 本身就随客户端分发，
   公开程度差不多）。要轮换/隐藏就走环境变量 `PULSES_CF_KEY`，不用改代码。
2. 允许用户覆盖，是因为内置 key 可能被限流；覆盖后立刻生效，清空即回到内置。

解析顺序：
    环境变量 PULSES_CF_KEY（显式给了就用它，给空串 = 禁用 CF 查询）
    → 用户保存的 key（首选项「CurseForge API Key」）
    → 内置默认 key
"""

from __future__ import annotations

import os

#: PCL2 内置的 CurseForge API Key（2026-11-08 实测：指纹查询命中 Sodium 394468）
DEFAULT_CF_API_KEY = ("$2a$10$fdQjum78EUUUcJIw2a6gb.m1DNZCQzwvf0EBcfm"
                      ".YgwIrmFX/1K3m")

ENV_NAME = "PULSES_CF_KEY"

SOURCE_USER = "user"
SOURCE_BUILTIN = "builtin"
SOURCE_OFF = "off"


def resolve(saved: str | None = None) -> str:
    """算出这次真正要用的 key；空串 = 不查 CurseForge。"""
    env = os.environ.get(ENV_NAME)
    if env is not None:                 # 显式设置（哪怕是空串）优先
        return env.strip()
    text = str(saved or "").strip()
    if text:
        return text
    return DEFAULT_CF_API_KEY


def source(saved: str | None = None) -> str:
    """key 从哪来：user（用户填的）/ builtin（内置）/ off（被环境变量禁掉）。"""
    env = os.environ.get(ENV_NAME)
    if env is not None:
        return SOURCE_USER if env.strip() else SOURCE_OFF
    return SOURCE_USER if str(saved or "").strip() else SOURCE_BUILTIN
