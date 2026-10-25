# DEC-010 · GitHub 上传工具固化到固定目录（REST API，不装 git）

- **状态**：已采纳
- **日期**：2026-10-25
- **范围**：环境级（可复用工具）
- **关联**：README「固定工具」

## 背景

这台机器 `git` 不在 PATH 里，也没有 `gh`。此前每次上传 GitHub 都是当场拼一段临时脚本，
用完就散：路径不固定、参数不一致、Token 每回重新找、出问题不知道是哪次跑的。
用户明确要求「在一个固定位置放工具」。

## 可选项

1. **下载便携版 git 到固定目录**：之后走标准 `clone/commit/push`，但要额外 ~50 MB 下载，
   且 Windows 上还要处理证书、换行、凭据管理器。
2. **固定目录 + GitHub REST API 脚本（Git Data API）**：不依赖 git，
   一次调用即产生一个原子 commit，只需要 Token。
3. **继续每次临时写脚本**：已经证明会重复劳动、留不下痕迹。
4. **装 gh CLI**：交互体验好，但同样要下载安装，且 Windows 安装包需要管理员权限。

## 决定

选 **方案 2**，固化到 `C:\Users\47431\.dsh\tools\github-push\`：

| 文件 | 职责 |
| --- | --- |
| `ghpush.py` | 主程序：blob → tree → commit → update ref |
| `.env` | `GITHUB_TOKEN` / `DEFAULT_REPO`（已被上传工具自身排除，不入库） |
| `push.ps1` / `push.cmd` | 包装，省去记参数 |
| `logs/push.jsonl` | 每次推送的落盘记录（时间/仓库/commit/文件数） |
| `README.md` | 用法与安全提醒 |

关键实现选择：
- 用 **git blob sha1** 在本地算内容指纹，与远端 tree 比对，只上传真正变化的文件；
- 一次调用只产生**一个 commit**（Git Data API），而不是逐个文件 Contents API 提交，
  避免仓库出现十几个 "Update xxx" 的噪音提交；
- 默认**不删除**远端多余文件（`--delete-missing` 才删），防止误删仓库内容；
- 默认忽略 `.env`、`runs/`、`__pycache__/`、`.venv/` 等，密钥类文件天然上不去。

## 理由

- 零安装：只要 Python + `requests`，这台机器已有（`D:\python\python.exe`，3.13.9）。
- 原子提交：一次上传一个 commit，历史干净，出问题好回滚。
- 有留痕：`logs/push.jsonl` 记录每次推送，解决「不知道上次推了什么」。
- 路径固定：以后任何项目都复用它，不再产生临时脚本。

## 代价与后果

- 不支持 `git` 的高级能力：分支合并、rebase、submodule、LFS、签名提交。作业场景不需要。
- 大仓库（>100 MB / >10 万文件）会受 API 速率与 tree 截断限制；
  当前仓库只有几十个文件，无影响。
- Token 是长期凭据：已提醒用户 **该 Token 曾在对话中明文出现，应 revoke 后重新生成**，
  并写回 `.env`；`.env` 永不上传。
- Token 权限一旦过宽（如 admin），工具本身没有问题但风险面变大，README 里要求最小权限
  （Fine-grained：`Contents: Read and write`）。

## 复核条件

若将来需要多人协作、分支保护、PR 流程，或需要管理仓库设置/Issues，
则安装 `gh` CLI 更合适，本工具保留为「快速同步」用途。
