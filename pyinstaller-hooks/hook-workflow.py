"""覆盖 PyInstaller 社区 hook-workflow.py（它为 PyPI 上的 workflow 包准备）。

本项目的 ``workflow`` 是仓库内的本地包，只依赖标准库 + agents/core，
不需要任何额外 hidden import 或数据文件；留一个空 hook 即可避免社区 hook 报错。
"""

# 有意留空
