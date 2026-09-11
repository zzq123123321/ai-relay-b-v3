# AI Relay B V3.0

AI Relay B V3.0 是从零重新开发的新工程：剪贴板接收 A 端任务，路由到 OpenChamber/Reasonix 执行端，观察、自动续接并回传结果，配合美观的 Windows 桌面界面与可追溯日志。

## 新旧项目关系

- 新项目（本仓库）：`D:\AIwork\ai_relay_b_v3`
- 旧项目：`D:\AIwork\ai_relay_b`

旧项目仅作为协议、接口经验、测试场景和历史问题的**只读参考**，不作为 V3 源码基线；V3 开发期间不修改旧项目任何文件。

## 开发资料位置

```text
D:\AIwork\ai_relay_b\最新开发01\AI_Relay_B_开发资料包_V3.0
```

（V3.0 完整开发规格、60 张分轮任务卡、149 项验收场景、SQL/配置/主题/提示词附录。）

## 环境

- Python 3.12（`>=3.12,<3.13`），独立虚拟环境 `.venv`（不使用 system-site-packages）。
- 运行依赖锁：`requirements-runtime.lock`；开发依赖锁：`requirements-dev.lock`（均来自实际 venv）。

## 最小开发命令

```powershell
# 自检（不连接执行端、不监听剪贴板、不建库）
.\.venv\Scripts\python.exe main.py --self-check

# 运行测试
.\.venv\Scripts\python.exe -m pytest -q

# 正常启动（最小占位窗口）
.\.venv\Scripts\python.exe main.py
```

## 当前状态

T02 阶段：最小工程骨架 + 独立 venv + 依赖锁。尚未实现协议、数据库、执行端适配、自动续接和正式界面（正式 UI 自 T13 开始）。