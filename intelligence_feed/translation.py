"""Offline English-to-Chinese title translation using CTranslate2."""
from __future__ import annotations

import os
import re
from pathlib import Path

from .paths import data_dir
from .repository import _maybe_transaction

_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_MODEL_NAME = "opus-mt-en-zh-ctranslate2"
_ATTRIBUTION_RE = re.compile(r"\s*,?\s*Says\s+TrendForce\s*$", re.IGNORECASE)
_KNOWN_TERMS = (
    "OpenAI", "Anthropic", "Claude", "Mistral", "NVIDIA", "GitHub", "Copilot",
    "TechCrunch", "TrendForce", "ChatGPT", "DeepSeek", "Gemini", "Llama", "Sora",
    "Codex", "DevDay", "AlphaFold", "LangChain", "Nature", "Meta", "Microsoft",
    "Google", "AMD", "Intel", "TSMC", "ASML", "CUDA", "Sol",
)
_PROTECTED_RE = re.compile(
    r"\b(?:GPT-\d+(?:\.\d+)*(?:\s+Sol)?|"
    + "|".join(re.escape(term) for term in _KNOWN_TERMS)
    + r"|[A-Z]{2,}(?:[-.][A-Z0-9]+)*|[A-Z][a-z]+[A-Z][A-Za-z0-9]*)\b"
)


def model_dir() -> Path:
    override = os.getenv("INTELLIGENCE_FEED_TRANSLATION_MODEL_DIR")
    return Path(override) if override else data_dir() / "models" / _MODEL_NAME


def should_translate(title: str | None) -> bool:
    text = (title or "").strip()
    return bool(text and _LATIN_RE.search(text) and not _CJK_RE.search(text))


def _prepare_title(title: str) -> tuple[str, list[str]]:
    text = _ATTRIBUTION_RE.sub("", title.strip())
    text = re.sub(r"\bAI\b", "artificial intelligence", text)
    terms: list[str] = []

    def protect(match):
        terms.append(match.group(0))
        return f"ZXQ{len(terms) - 1}ZX"

    return _PROTECTED_RE.sub(protect, text), terms


def _restore_terms(text: str, terms: list[str]) -> str:
    for index, term in enumerate(terms):
        text = text.replace(f"ZXQ{index}ZX", term)
    return text


class LocalTranslator:
    """Lazy process-local wrapper around a Marian CTranslate2 model."""

    def __init__(self, path: str | Path | None = None):
        path = Path(path) if path else model_dir()
        required = (path / "model.bin", path / "source.spm", path / "target.spm")
        if not all(item.is_file() for item in required):
            raise FileNotFoundError("local translation model is not installed")

        import ctranslate2
        import sentencepiece as spm

        self._source = spm.SentencePieceProcessor(model_file=str(path / "source.spm"))
        self._target = spm.SentencePieceProcessor(model_file=str(path / "target.spm"))
        self._translator = ctranslate2.Translator(
            str(path), device="cpu", compute_type="int8", inter_threads=1,
        )

    def translate_many(self, titles: list[str]) -> list[str]:
        if not titles:
            return []
        prepared = [_prepare_title(title) for title in titles]
        token_batches = [
            self._source.encode(text, out_type=str) + ["</s>"]
            for text, _terms in prepared
        ]
        results = self._translator.translate_batch(
            token_batches,
            beam_size=4,
            max_decoding_length=256,
        )
        decoded = [
            self._target.decode([
                token for token in result.hypotheses[0] if token not in {"<pad>", "</s>"}
            ]).strip()
            for result in results
        ]
        return [
            _restore_terms(text, terms)
            for text, (_prepared_text, terms) in zip(decoded, prepared)
        ]


def translate_pending(conn, *, translator=None, limit: int = 100) -> int:
    if limit <= 0:
        return 0
    rows = conn.execute(
        "SELECT id, title FROM articles WHERE translated_title IS NULL ORDER BY id"
    ).fetchall()
    candidates = [(int(row["id"]), row["title"]) for row in rows if should_translate(row["title"])][:limit]
    if not candidates:
        return 0

    engine = translator or LocalTranslator()
    translated = engine.translate_many([title for _article_id, title in candidates])
    if len(translated) != len(candidates):
        raise ValueError("translation result count mismatch")

    updates = [
        (text.strip(), article_id)
        for (article_id, _title), text in zip(candidates, translated)
        if text and text.strip()
    ]
    with _maybe_transaction(conn):
        conn.executemany(
            "UPDATE articles SET translated_title=? WHERE id=? AND translated_title IS NULL",
            updates,
        )
    return len(updates)
