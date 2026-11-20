# Novel Memory MCP

**Keep long stories within reach. Let your AI look up the past when it needs it.**

A self-hosted story-memory server for **RikkaHub**, long-form fiction, and ongoing roleplay. Maintain curated history in Markdown, manage it through a web console, and choose which stages your MCP client can retrieve.

[中文](README.md) · **English** · [Detailed setup guide (Chinese)](docs/QUICKSTART.md)

## Why use it?

Long-running stories accumulate promises, relationships, secrets, and unfinished threads. This project gives your AI client a way to consult selected historical records while you continue writing.

- **Complete story stages:** retrieve curated blocks with their causal context intact.
- **On-demand retrieval:** browse tags, search metadata, then read the selected stage.
- **Controlled access:** select one active library and explicitly enable its stages.
- **Web console:** create libraries, edit stages, preview Markdown, import files, and filter records.
- **Read-only MCP tools:** the model can search and read; people maintain the source text.
- **Portable storage:** UTF-8 Markdown is the source of truth; SQLite is a rebuildable local index.
- **Lightweight hosting:** Python, SQLite, Streamable HTTP, and STDIO. No embedding setup required.

Two generic fictional demo stages are included. The server does not automatically summarize conversations, update memories, or inject all history into chat.

## v0.1.1

Safer Chinese filename imports, conflict detection for stale edits, history backups before edits and deletes, Unicode management passwords, clearer pending versus saved scope, and fail-closed handling of damaged scope files. Update the code and dependencies, restart both services, and reload existing browser tabs. Existing Markdown, credentials, and scope files remain in use. See the [changelog](CHANGELOG.md).

## Quick start

Requires Python 3.10+ and SQLite with FTS5 support. Installing dependencies requires internet access.

```bash
git clone https://github.com/syydmtl/novel-memory-mcp.git
cd novel-memory-mcp
python3 -m venv .venv
.venv/bin/python -m pip install -e "." -c security-constraints.txt
.venv/bin/python run.py setup
.venv/bin/python run.py check
```

On Windows, use `python` to create the environment and `.venv\Scripts\python.exe` for subsequent commands.

Open the generated `.env` locally. `NOVEL_MEMORY_ADMIN_TOKEN` is your web-console password; `NOVEL_MEMORY_TOKEN` is your separate MCP credential. Setup generates fresh random values and refuses to overwrite an existing `.env`.

Start these in **separate terminals**:

```bash
.venv/bin/python run.py admin
```

```bash
.venv/bin/python run.py mcp
```

Open **http://127.0.0.1:8767/**, sign in, create a story library, and select the stages to expose.

## Connect RikkaHub or another MCP client

| Setting | Value |
| --- | --- |
| Transport | Streamable HTTP |
| URL | `http://127.0.0.1:8766/mcp` |
| Header | `Authorization: Bearer <your NOVEL_MEMORY_TOKEN>` |

When connecting from a phone or another device, use the server machine's reachable address and configure the bind address, exact Host/Origin allowlists, and network access rules. Use HTTPS or a trusted private network for confidential remote access. Unencrypted HTTP exposes credentials and returned text in transit.

STDIO is also available: configure your client to run this project's virtual-environment Python with the absolute path to `run.py` and `stdio` as its two arguments. The launcher resolves the local project configuration automatically.

## Tools

| Tool | Result |
| --- | --- |
| `list_memory_tags` | Stage IDs, date ranges, titles, and tags |
| `search_memory` | Matching stage IDs, date ranges, and titles |
| `get_memory` | One complete selected stage |
| `get_memories` | Up to two complete selected stages |

The suggested workflow is **tags → search → read**. Retrieval quality depends on the records, tags, and the client's tool use. Full stage text still consumes model context.

## Memory format

One UTF-8 Markdown file represents one historical stage:

```markdown
---
id: my_stage_01
date_range: Story days 1-3
title: The travelers agree to repair a bridge
tags: Character A, Character B, harbor, promise
---

Write the curated events, important dialogue, relationship changes,
and unresolved threads here.
```

The filename should match the ID, for example `my_stage_01.md`. Import records through the console or edit the files directly. The search index refreshes on the next tool call.

## Scope and privacy

- One service instance shares one active scope across its clients. It is not a per-user permission system.
- With an active library configured, all four MCP tools are restricted to selected stages. An empty selection exposes no stages.
- `.env`, runtime state, and newly created personal libraries are ignored by Git. Already tracked demo files remain tracked.
- If a client passes retrieved text to a cloud model, that provider receives it.
- Store real stories in a newly created library and check archive contents before sharing your own copy.

See [security notes](SECURITY.md), [memory format details](libraries/README.md), and [contributing](CONTRIBUTING.md). Bug reports and documentation improvements are welcome; use fictional reproductions and remove credentials.

## License

[MIT](LICENSE).
