---
name: paper-search
description: >
  Semantic search of the local paper knowledge base. Handles Chinese and English
  queries. Use whenever the user asks about any research topic covered by the
  paper collection.
when_to_use: >
  Triggers when the user asks to search the paper knowledge base, or references
  a specific paper filename or research topic from the collection.
  To check whether a topic is covered, read the kb/collection_info.json file
  which contains the collection's keywords and language.
allowed-tools:
  - "Bash: python *"
---

# Paper Knowledge Base Search

You have access to a local paper knowledge base stored as a ChromaDB vector database with Cross-Encoder re-ranking.

## Dynamic Collection Info

Read `kb/collection_info.json` to learn about the current paper collection:

```bash
cat "${CLAUDE_PROJECT_DIR}/kb/collection_info.json"
```

This file is auto-generated after each Zotero sync. It contains the paper count, keywords extracted from titles, and the dominant language (en/zh). Use it to understand what topics the collection covers.

**Important: Working Directory**
All search commands must be run from the `paper-knowledge-base/` subdirectory.
Prefer `cd paper-knowledge-base && python scripts/query.py ...` when running manually.
From the monorepo root, use `python pkb.py ...` for a quick search.

## Instructions

1. **Set the correct working directory.** Always `cd paper-knowledge-base/` before running
   commands. If you see ImportError, check your CWD first.

2. **Check for a query**. If `$ARGUMENTS` is empty, ask the user what they want to search for and stop.

3. **Choose search mode** based on the user's intent:
