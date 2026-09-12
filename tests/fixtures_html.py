"""Строковые фикстуры HTML: читаются из tests/fixtures (TRA означает файл)."""

from pathlib import Path

_FIXTURES = Path(__file__).parent / "fixtures"

ARTICLE_HTML = (_FIXTURES / "article.html").read_text(encoding="utf-8")
TINY_HTML = (_FIXTURES / "tiny.html").read_text(encoding="utf-8")
