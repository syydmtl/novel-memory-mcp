# 安装、配置与使用指南

面向长篇小说和角色扮演的历史记忆管理工具。人工维护完整的 Markdown 剧情阶段，通过网页管理记忆库和开放范围，AI 通过 MCP 按需查询和读取。

本包包含完整 Python 源码、管理网页、测试和两个通用虚构示例。使用者首次运行时自行生成独立密码。

## 主要功能

- 多小说 / 多世界观记忆库：新建、编辑和切换。
- 历史阶段管理：新增、编辑、删除、阅读预览、Markdown 导入和网页筛选。
- AI 开放范围：选择一个活动记忆库，勾选允许读取的阶段，再保存挂载。
- 四个只读 MCP 工具：标签索引、关键词搜索、单阶段读取、最多两个阶段的批量读取。
- 支持 STDIO 和 Streamable HTTP，兼容支持这些传输方式的 MCP 客户端。
- Markdown 是正文来源；SQLite FTS5 和关键词匹配用于本地检索，索引可重建。
- MCP HTTP 和网页管理分别使用独立密码。

## 环境要求

Python 3.10 或更新版本，SQLite 支持 FTS5。首次安装依赖需要联网。

下面的命令在解压后的项目目录中运行。项目提供 Python 启动器，Windows 不需要执行 PowerShell 脚本。

## Windows 快速开始

在项目文件夹打开终端：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e "."
.venv\Scripts\python.exe run.py setup
.venv\Scripts\python.exe run.py check
```

`setup` 会创建本机 `.env`，为 MCP 和网页管理分别生成随机密码，并开放两个通用示例阶段。已有 `.env` 时会拒绝覆盖。

用文本编辑器打开 `.env`：

- `NOVEL_MEMORY_ADMIN_TOKEN`：网页登录密码。
- `NOVEL_MEMORY_TOKEN`：MCP 客户端连接密码。

在两个终端分别运行：

```powershell
.venv\Scripts\python.exe run.py admin
```

```powershell
.venv\Scripts\python.exe run.py mcp
```

浏览器打开 `http://127.0.0.1:8767/`，使用网页登录密码进入工作台。

## macOS / Linux 快速开始

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e "."
.venv/bin/python run.py setup
.venv/bin/python run.py check
```

随后在两个终端分别运行 `.venv/bin/python run.py admin` 和 `.venv/bin/python run.py mcp`。

## 连接 MCP 客户端

使用 Streamable HTTP：

```text
URL: http://127.0.0.1:8766/mcp
Authorization: Bearer <本机 .env 中 NOVEL_MEMORY_TOKEN 的值>
```

可发现的工具：

| 工具 | 作用 |
| --- | --- |
| list_memory_tags | 返回阶段 ID、时间范围、标题与标签 |
| search_memory | 按关键词搜索，只返回 ID、时间范围与标题 |
| get_memory | 返回一个指定阶段的完整正文 |
| get_memories | 返回最多两个指定阶段的完整正文 |

推荐流程：先列出标签，再搜索，最后读取所需阶段。已知阶段 ID 时可直接读取。服务不会自动向普通聊天注入全部历史。

使用 STDIO 时，将客户端的 command 设置为此项目虚拟环境的 Python 可执行文件，args 设置为 `run.py` 的绝对路径和 `stdio` 两项。启动器会自动定位项目目录和本机 `.env`。

启动器会用本项目 `.env` 覆盖对应的 `NOVEL_MEMORY_*` 环境配置，避免复用其他项目的路径或密码。若直接运行底层 Python 模块，则需要自行设置完整环境变量。

## 使用自己的故事

1. 在网页中新建小说或世界观。
2. 新增阶段，或直接导入 `.md` 文件。
3. 为阶段填写时间范围、标题和检索标签。
4. 勾选允许 AI 访问的阶段，点击“保存挂载给 AI”。
5. 在 MCP 客户端中要求 AI 检索对应历史。

当前开放范围由所有连接到这台服务的客户端共用，一次选择一个记忆库。保存空选择后，该库没有阶段可供 MCP 读取。未设置活动范围时使用 `memories/` 后备目录。

格式参见 [记忆格式说明](../libraries/README.md) 和 [示例记忆](../libraries/demo_story/memories/)。示例文件可由使用者自行删除或替换。

正文只通过人工编辑或管理操作更新；AI 的 MCP 接口没有写入能力。服务按存储正文返回内容，不进行自动摘要、拆分或改写。

## 配置与运行数据

| 文件 / 目录 | 用途 |
| --- | --- |
| .env.example | 可分享的通用配置模板 |
| .env | 首次初始化后生成的本机配置与密码 |
| state/ | 运行时索引、活动范围及可能更新的网页密码 |
| libraries/ | 用户维护的小说或世界观记忆库 |
| memories/ | 未设置活动范围时的后备记忆目录 |
| web/index.html | 完整管理网页，不依赖外部 CDN |
| novel_memory_mcp/ | MCP、数据管理与网页 API 源码 |

网页中修改管理密码后，新密码保存在 `state/admin_token.txt`，优先于 `.env` 中的管理密码；MCP 密码仍单独配置。

修改端口时，也应更新 MCP 的 `NOVEL_MEMORY_ALLOWED_HOSTS` 和 `NOVEL_MEMORY_ALLOWED_ORIGINS` 中对应的端口。

默认仅监听本机。需要跨设备访问时，使用者自行配置监听地址、准确的 Host/Origin 允许列表、网络访问规则和 HTTPS 或可信私网；原始 HTTP 不会加密密码和正文。

## 开发与检查

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe test_tools.py
```

启动 `run.py mcp` 后可在另一个终端执行：

```powershell
.venv\Scripts\python.exe http_test.py
```

此检查只使用本机 `.env`，不会显示密码。

## 分享时注意

发行包不包含实际运行配置、运行数据库或旧项目的 Git 历史。

自行使用后再次分享时，请排除 `.env`、`state/`、`.venv/`、缓存、日志和自己的小说正文。`.gitignore` 只影响 Git，不会替你过滤手动压缩的文件。

源码采用 MIT 许可证，见 `LICENSE`。
