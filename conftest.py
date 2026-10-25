"""pytest 根配置：让 tests/ 能直接 import solutions/ttl_cache.py。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for extra in (ROOT / "solutions", ROOT):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))
