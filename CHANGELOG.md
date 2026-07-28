# Changelog

All notable changes to the paper-knowledge-base-monorepo are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.0] — 2026-07-28

### Added

- **MinerU-GUI:** Initial release of desktop GUI + Python API for MinerU document parsing.
  - CustomTkinter-based GUI with dark/amber Anthropic-inspired theme
  - `mineru_api.py` — standalone Python API (`convert_document`, `batch_convert`)
  - Hybrid-auto-engine and pipeline backends
  - VLM image description (hybrid backend only)
  - Drag-and-drop file input, batch conversion, progress bar

- **Paper Knowledge Base:** Initial release of semantic + text search system for academic papers.
  - Bi-Encoder (paraphrase-multilingual-MiniLM-L12-v2) → ChromaDB HNSW vector search
  - Cross-Encoder (ms-marco-MiniLM-L-6-v2) re-ranking
  - SQLite FTS5 trigram text search (~10ms)
  - Zotero integration — sync PDFs and metadata from local `zotero.sqlite`
  - MinerU extraction pipeline (PDF → clean text → chunk → embed)
  - Dual-search architecture with automatic fallback
  - CLI interactive search (`search.py`), JSON query (`query.py`)
  - Paper ingestion via both Zotero sync and direct `ingest.py`

[1.0.0]: https://github.com/wolfmanster/paper-knowledge-base-monorepo/releases/tag/v1.0.0
