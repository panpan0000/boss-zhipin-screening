#!/usr/bin/env python3
"""不安装也能跑的启动器。

    python3 zhipin.py doctor --stdout
    python3 zhipin.py run --jd "……" --limit 5

安装后（pip install -e .）可直接用 `zhipin-screening` 命令。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from zhipin_screening.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
