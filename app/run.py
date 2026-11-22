#!/usr/bin/env python3
"""隔离模式启动器：仅把本文件所在目录（应用代码目录）加入 sys.path。

用法：python3 -I run.py --sources ../sources --runtime ../runtime analyze ...
数据目录通过参数传入，绝不进入 sys.path，避免 planted 模块风险。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from patrol.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
