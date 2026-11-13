# 记忆库目录

每个子目录代表一个小说或世界观：

```text
libraries/
  demo_story/
    library.json
    memories/
      demo_stage_01.md
      demo_stage_02.md
```

`library.json` 保存 `library_id`、`name`、`description`。
`memories` 中的文件使用 UTF-8 Markdown，正文以文件为准。
`demo_story` 仅含通用虚构示例，可以从网页创建自己的记忆库。

每份阶段文件格式：

```markdown
---
id: my_stage_01
date_range: 故事第1天—第3天
title: 阶段标题
tags: 角色甲, 地点甲, 事件甲
---

在这里填写人工整理的完整历史阶段正文。
```

文件名应与 `id` 一致，例如 `my_stage_01.md`。标识只能包含英文字母、数字、下划线和连字符。
元数据每项写在单独一行；当前解析器使用简单的 `key: value` 格式，不支持复杂 YAML 结构。
修改正文后，下一次 MCP 工具调用会自动同步搜索索引。
