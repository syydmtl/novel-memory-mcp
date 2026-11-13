# 参与贡献 / Contributing

欢迎改进文档、复现和修复问题、完善 RikkaHub 接入体验，或提出具体的创作需求。

## 报告问题

请说明使用的系统、Python 版本、MCP 客户端版本、连接方式、预期结果和复现步骤。
使用通用虚构示例，不要提交真实密码、`.env`、运行状态、私人剧情或包含个人信息的日志。

## 本地开发

在独立分支上进行修改：

```bash
python -m venv .venv
```

Windows 使用 `.venvScriptspython.exe`，macOS / Linux 使用 `.venv/bin/python`：

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
```

上面后三条命令中的 `python` 请替换为对应虚拟环境的 Python 路径，或先激活虚拟环境。

请优先提交范围清楚的小修改。修改检索、读取或开放范围时，补充能够验证实际行为的测试。
如果改变记忆格式、配置项、工具返回结构或启动方式，也请同步更新中英文文档。

## 设计约定

- Markdown 保持为正文来源；SQLite 是可重建索引。
- MCP 只提供检索与读取，人工管理接口负责修改。
- 完整阶段正文不自动摘要、拆分或改写。
- 搜索结果保持简洁，完整正文通过读取工具获取。
- 已配置活动范围时，所有 MCP 查询和读取都必须遵守该范围。
- 所有文本文件使用 UTF-8。

## 提交 Pull Request

说明具体问题、修改后的行为和完成的验证。请确认提交中只有源码、文档和通用测试样例。

English contributions are welcome. Describe the problem, the resulting behavior, and how you verified it. Keep examples fictional and exclude secrets or personal story data.
