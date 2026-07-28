---
name: paper-search
description: >
  Semantic search of the local paper knowledge base (116 papers on battery thermal
  management, PCM, liquid cooling, BTMS, thermal runaway, heat transfer simulation,
  topology optimization). Handles Chinese and English queries. Use whenever the user
  asks about battery cooling, phase change materials, thermal management of
  batteries, COMSOL battery simulation, liquid cold plates, heat pipes, immersion
  cooling, thermal runaway, or any research topic covered by the paper collection.
when_to_use: >
  Triggers on: "battery thermal management", "BTMS", "PCM", "phase change",
  "liquid cooling", "cold plate", "heat pipe", "topology optimization", "thermal
  runaway", "immersion cooling", "battery safety", "heat transfer", "COMSOL
  simulation", "battery cooling", "锂电池热管理", "相变材料", "液冷", "热失控",
  "电池安全". Also triggers when the user references a specific paper filename
  or research topic from the collection.
allowed-tools:
  - "Bash: python *"
---

# Paper Knowledge Base Search

You have access to a local knowledge base of **116 papers** on battery thermal management stored as a ChromaDB vector database with Cross-Encoder re-ranking. Use this skill whenever the user asks about battery thermal management topics.

## Instructions

1. **Check for a query**. If `$ARGUMENTS` is empty, ask the user what they want to search for and stop.

2. **Choose search mode** based on the user's intent:

   - **Semantic search** (default) — for research questions, concept search, "find papers about X":
     ```bash
     python "${CLAUDE_PROJECT_DIR}/scripts/query.py" "$ARGUMENTS" 5
     ```

   - **Text/keyword search** — for title lookups, finding papers by name, searching for specific terms/acronyms/materials:
     ```bash
     python "${CLAUDE_PROJECT_DIR}/scripts/query.py" --mode text "$ARGUMENTS" 10
     ```

   **When to use text search:**
   - User asks for papers with a specific keyword in the title (e.g. "papers about PCM composite")
   - User wants to quickly find a paper by name or author
   - User asks for all papers mentioning a specific material, method, or acronym
   - The query is short keyword(s) rather than a natural language question
   - User explicitly asks for "quick search" or "keyword search"

   **When to use semantic search (default):**
   - User asks a research question or concept-level question
   - Natural language query like "how does immersion cooling compare to cold plate?"
   - Broad topic exploration where exact keyword match would be too narrow

3. **Parse the JSON output** and display the results to the user. Each result from semantic search has:
   - `score` (0-1, higher is more relevant)
   - `title` (full paper title)
   - `section` (which section of the paper the match came from)
   - `filename` (raw filename in the `Papers/` directory)
   - `preview` (first 300 characters of the matching text)

   Each result from text search has:
   - `title` (paper title)
   - `filename` (raw filename in the `Papers/` directory)
   - `abstract_preview` (first 300 chars of extracted abstract, with **match highlights**)
   - `summary` (AI extractive summary, first ~200 chars)
   - `match_type` (which field matched: title, abstract, summary, or a combination)
   - `score` (relevance score, 0-10)

4. **Present results clearly**:
   - Show the top results ranked by score with paper titles and relevance scores
   - For the #1 result, summarize the key finding from the preview text
   - If results are in Chinese, respond in Chinese; if in English, respond in English
   - If no results match, say so clearly
   - Note: The `filename` field is a unique identifier for the paper in the knowledge base, not a guarantee the PDF still exists in `Papers/`. Use `--get-paper-chunks` to get full text regardless.

5. **Getting more detail on a paper** — When the user asks about a specific paper's details (methodology, results, figures, etc.) and the search previews aren't enough:

   ```bash
   python "${CLAUDE_PROJECT_DIR}/scripts/query.py" --get-paper-chunks "filename.pdf"
   ```

   This retrieves all text chunks for that paper directly from ChromaDB — **no PDF file needed**. The output includes:
   - `chunk_count` — how many chunks the paper has
   - `full_text` — all chunks concatenated in order (up to ~100K chars), with section labels

   ⚠️ **IMPORTANT**: Never try to read PDF files from `Papers/` directory with Python or `extract_text_from_pdf()`. Zotero-synced papers may have their PDFs stored externally. Always use `--get-paper-chunks` for full paper text.

## Examples

**Semantic search:**

| User says | Command |
|-----------|---------|
| "Show me papers on liquid cold plate topology optimization" | `python "${CLAUDE_PROJECT_DIR}/scripts/query.py" "liquid cold plate topology optimization" 5` |
| "液冷板拓扑优化设计" | `python "${CLAUDE_PROJECT_DIR}/scripts/query.py" "液冷板拓扑优化设计" 5` |
| "PCM composite battery cooling" | `python "${CLAUDE_PROJECT_DIR}/scripts/query.py" "PCM composite phase change material battery cooling" 5` |
| "锂电池热失控防护" | `python "${CLAUDE_PROJECT_DIR}/scripts/query.py" "锂电池热失控 thermal runaway" 5` |

**Text/keyword search:**

| User says | Command |
|-----------|---------|
| "Find papers with 'topology optimization' in the title" | `python "${CLAUDE_PROJECT_DIR}/scripts/query.py" --mode text "topology optimization" 10` |
| "搜索关于相变材料的论文" | `python "${CLAUDE_PROJECT_DIR}/scripts/query.py" --mode text "相变材料" 10` |
| "What papers mention immersion cooling?" | `python "${CLAUDE_PROJECT_DIR}/scripts/query.py" --mode text "immersion cooling" 10` |
| "Show me papers about Tesla" | `python "${CLAUDE_PROJECT_DIR}/scripts/query.py" --mode text "Tesla" 10` |

## Notes

- The `allowed-tools` frontmatter pre-approves `python *` commands so the search runs without a permission prompt
- Use `${CLAUDE_PROJECT_DIR}` to reference the project root
- The query script always outputs valid JSON to stdout; errors also appear as JSON with an `"error"` key
- **Semantic search** (default, no flag) is best for research questions and concepts; scores are 0-1
- **Text search** (`--mode text`) is faster (~10ms vs ~5s) and best for keyword lookups, title search, and finding specific terms; scores are 0-10
- Increase the result count to 8-10 for broad survey questions, decrease to 3 for precise lookups
- Text search requires the index to be built first: `python scripts/build_index.py` (re-run after ingesting new papers)
