"""ADOFAI 时间模型（第三方 vendor，MIT）。

来源: https://github.com/... "ADOFAI Macro" V5.0  (E:\\another Another things\\Adofai-Macro-Adofai_Macro_V5.0)
许可: MIT — 见同目录 LICENSE
改动: 仅把 `action['eventType']` 改为 `.get('eventType')`，以容忍真实语料里缺 eventType 的畸形条目
      （语料 521 张中有 4 张会因此 KeyError）。语义未改。

本包只提供「.adofai → 每层绝对时间/按键时间」的解析，不含任何按键注入逻辑。
"""

from .reader import ADOLevelData
from .angle import ADOAngle

__all__ = ["ADOLevelData", "ADOAngle"]
