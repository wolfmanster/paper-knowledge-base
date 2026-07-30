"""论文摘要与摘要摘要提取。

对三种期刊格式做多模式匹配：
1. 空格字母格式（Elsevier: `A B S T R A C T`）→ `_normalize_spaced_letters` 对照学术词汇表拆分合并
2. 标准英文 `ABSTRACT ... Keywords`
3. 中文 `摘要 ... 关键词`
4. 回退：去除期刊页眉 boilerplate 后取前 500 字符
"""

from __future__ import annotations

import re


# 期刊页眉常见 boilerplate 模式（在回退提取时需要清理）
_JOURNAL_BOILER = re.compile(
    r"^(?:journal of|international|applied|energy|thermal|"
    r"heat and mass|electrochimica|renewable|sustainable|"
    r"©|copyright|published by|elsevier|springer|mdpi|"
    r"http|doi:|vol\.|pp\.|issn|isbn)",
    re.IGNORECASE,
)


def _strip_journal_header(text: str) -> str:
    """移除期刊页眉 boilerplate 行。"""
    lines = text.split("\n")
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _JOURNAL_BOILER.match(stripped):
            continue
        result.append(line)
    return "\n".join(result)


def _clean_abstract(text: str) -> str:
    """清理提取后的摘要文本。"""
    # 移除残留的 "Keywords: ..." 后缀
    text = re.sub(r"\b(?:Keywords?|K E Y W O R D S?|Key words?)\b[:\s]*.*$", "", text, flags=re.IGNORECASE | re.DOTALL)
    # 折叠多余空白
    text = re.sub(r"\s+", " ", text)
    # 修复断词连字符
    text = re.sub(r"(\w)-\s(\w)", r"\1\2", text)
    return text.strip()


# 已知的期刊/DOC 中常见的空格字母单词
_SPACED_WORDS = {
    "ABSTRACT", "INTRODUCTION", "KEYWORDS", "ARTICLE", "INFO",
    "RESEARCH", "PAPER", "REVIEW", "METHODOLOGY", "METHODS",
    "RESULTS", "DISCUSSION", "CONCLUSION", "CONCLUSIONS",
    "NOMENCLATURE", "REFERENCES", "BIBLIOGRAPHY", "ACKNOWLEDGMENT",
    "ACKNOWLEDGEMENTS", "APPENDIX", "HIGHLIGHTS", "GRAPHICAL",
    "SUPPLEMENTARY", "MATERIALS", "EXPERIMENTAL", "EXPERIMENT",
    "BACKGROUND", "RELATED", "WORK",
}


def _normalize_spaced_letters(text: str) -> str:
    """将单字母空格单词（A B S T R A C T 等）合并为普通单词。

    识别连续单大写字母+空白序列，对照已知学术词汇表进行拆分和合并。
    例如 "A R T I C L E   I N F O   A B S T R A C T" → " ARTICLE INFO ABSTRACT ".
    """
    # 匹配连续的单大写字母+空白序列
    pattern = re.compile(r'(?:[A-Z](?:\s+|$)){2,}')

    matches = list(pattern.finditer(text))
    if not matches:
        return text

    # 从后往前替换以保持位置正确
    result = text
    for m in reversed(matches):
        raw = m.group(0)
        letters = ''.join(re.findall(r'[A-Z]', raw))
        tokens = _split_letters_by_known_words(letters)
        if tokens:
            replacement = ' ' + ' '.join(tokens) + ' '
            result = result[:m.start()] + replacement + result[m.end():]

    return result


def _split_letters_by_known_words(letters: str) -> list[str]:
    """将连续的字母序列按照已知词汇表拆分。

    从后往前贪心匹配：优先匹配最长的已知单词。
    例如 "ARTICLEINFOABSTRACT" → ["ARTICLE", "INFO", "ABSTRACT"].
    """
    tokens: list[str] = []
    pos = len(letters)
    while pos > 0:
        found = None
        # 优先匹配长单词（从后往前取 4-12 个字符）
        for length in range(min(12, pos), 3, -1):
            candidate = letters[pos - length:pos]
            if candidate in _SPACED_WORDS:
                found = candidate
                break
        if found:
            tokens.append(found)
            pos -= len(found)
        else:
            # 无法匹配，跳过当前位置
            pos -= 1

    tokens.reverse()
    return tokens


def extract_abstract(full_text: str) -> tuple:
    """从论文全文文本中提取摘要。

    返回值: (abstract_text: str, found_marker: bool)
      - found_marker=True 表示检测到了真正的 Abstract 章节标记
      - found_marker=False 表示使用了回退策略
    """
    if not full_text or len(full_text.strip()) < 50:
        return ("", False)

    # 在前 15000 字符中搜索
    head = full_text[:15000]

    # 将空格字母格式标准化
    normalized = _normalize_spaced_letters(head)
    # 折叠多余空白
    normalized = re.sub(r'\s+', ' ', normalized)

    # ── 尝试多个模式，按优先级 ──

    # 1. ABSTRACT ... KEYWORDS / INTRODUCTION（Elsevier 等主流期刊）
    m = re.search(
        r'\bABSTRACT\b\s*(?:\(.*?\))?\s*(.+?)(?:\n\s*|\s{2,})(?:KEYWORDS?|K\s*E\s*Y\s*W\s*O\s*R\s*D)',
        normalized, re.DOTALL | re.IGNORECASE,
    )
    if not m:
        # 宽松模式：Keywords 前即使没有明显分隔也尝试匹配
        m = re.search(
            r'\bABSTRACT\b\s*(?:\(.*?\))?\s*(.+?)\s*KEYWORDS?\b',
            normalized, re.DOTALL | re.IGNORECASE,
        )
    if m:
        abstract = _clean_abstract(m.group(1).strip())
        if len(abstract) >= 40:
            return (abstract, True)

    # 2. ABSTRACT ... (end of pattern) -- 没有 KEYWORDS 终止的情况
    m = re.search(
        r'\bABSTRACT\b\s*(?:\(.*?\))?\s*(.+?)(?:\bINTRODUCTION\b|\d+\.?\s*(?:Introduction|INTR))',
        normalized, re.DOTALL | re.IGNORECASE,
    )
    if m:
        abstract = _clean_abstract(m.group(1).strip())
        if len(abstract) >= 40:
            return (abstract, True)

    # 3. 中文摘要: 摘要 ... 关键词/关键字
    m = re.search(
        r'摘\s*要\s*[:：]?\s*(.+?)(?:关键词|关键字|第[一二三\d]章|引言|绪论|一、)',
        head, re.DOTALL,
    )
    if m:
        abstract = _clean_abstract(m.group(1).strip())
        if len(abstract) >= 20:  # 中文摘要可能较短
            return (abstract, True)

    # 4. Abstract（首字母大写）... Keywords / Introduction
    m = re.search(
        r'\bAbstract\b\s*\n+(.+?)(?:\bKeywords?\b|\bIntroduction\b)',
        normalized, re.DOTALL,
    )
    if m:
        abstract = _clean_abstract(m.group(1).strip())
        if len(abstract) >= 40:
            return (abstract, True)

    # 5. 未规范化的 A B S T R A C T（兜底）
    m = re.search(
        r'A\s*B\s*S\s*T\s*R\s*A\s*C\s*T\s*(?:\(.*?\))?\s*(.+?)\s*(?:K\s*E\s*Y\s*W\s*O\s*R\s*D\s*S?|Keywords?)',
        head, re.DOTALL | re.IGNORECASE,
    )
    if m:
        abstract = _clean_abstract(m.group(1).strip())
        if len(abstract) >= 40:
            return (abstract, True)

    # ── 回退：取全文前 500 字符（跳过期刊页眉） ──
    cleaned = _strip_journal_header(normalized)
    words = cleaned.split()
    if not words:
        return ("", False)
    # 找到第一个大于 1 个字符的单词作为起点
    start = 0
    for i, w in enumerate(words):
        if len(w) > 1 and re.match(r'^[A-Za-z一-鿿]', w):
            start = i
            break
    fallback = ' '.join(words[start:start + 100])
    return (fallback[:500].strip(), False)


def generate_summary(abstract: str, full_text: str = "") -> str:
    """生成提取式摘要。

    策略：取摘要的前 2-3 句（最多 200 字符）。
    如果摘要太短，回退使用全文前 200 字符。
    """
    source = abstract.strip() if abstract and len(abstract.strip()) >= 30 else ""
    if not source and full_text:
        source = full_text.strip()

    if not source:
        return ""

    # 取前 2-3 句
    sentences = re.split(r'(?<=[.!?。！？])\s+', source)
    summary_parts: list[str] = []
    total = 0
    for s in sentences[:3]:
        s = s.strip()
        if not s:
            continue
        summary_parts.append(s)
        total += len(s)
        if total >= 200:
            break

    return " ".join(summary_parts).strip()
