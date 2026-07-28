---
name: paper-search
description: >
  Semantic search of the local paper knowledge base. Handles Chinese and English
  queries. Use whenever the user asks about any research topic covered by the
  paper collection.
when_to_use: >
  Triggers when the user asks to search the paper knowledge base, or references
  a specific paper filename or research topic from the collection. Also triggers
  on research-related queries matching the collection's subject matter.
allowed-tools:
  - "Bash: python *"
---

# Paper Knowledge Base Search

You have access to a local paper knowledge base stored as a ChromaDB vector database with Cross-Encoder re-ranking.

## Instructions

1. **Check for a query**. If `$ARGUMENTS` is empty, ask the user what they want to search for and stop.

2. **Choose search mode** based on the user's intent:
