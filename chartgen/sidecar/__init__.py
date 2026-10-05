"""sidecar 包：把 `core/` 包成一个给 Electron 前端用的 HTTP 服务。

**零新增依赖**（只用标准库），因为 `core/` 已经把所有算法做完了：
这里只做「会话状态 + 参数映射 + 视图数据打包」，逻辑与旧 PySide6 UI 对等
（对齐 `docs/18-ui功能清单.md`）。

    python -m sidecar.server --port 8765        # 手动起（调试用）
    # 正常由 app/main.js 起：Electron 自己找空闲端口再传进来
"""
from __future__ import annotations

import os
import sys

#: 仓库根（本文件的上一级）。`core` 就在那里。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

IMPORTS_OK = True

__all__ = ["ROOT", "IMPORTS_OK"]
