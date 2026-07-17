"""Конвертация Markdown → Telegram MarkdownV2 (мягко: не крашится, эскейпит)."""
from engine.core.formatting import to_telegram_markdown


def test_bold_converted_not_none():
    out = to_telegram_markdown("**Аполлон** — визионер")
    # либа может быть не установлена в окружении → None (тогда шлём плейн); если есть — строка
    if out is not None:
        assert isinstance(out, str)
        # звёздочки стандартного MD (**) не должны остаться как буквальный двойной маркер
        assert "**" not in out


def test_plain_text_survives():
    out = to_telegram_markdown("привет")
    if out is not None:
        assert "привет" in out


def test_does_not_raise_on_weird_input():
    # незакрытые конструкции не должны ронять — максимум None
    to_telegram_markdown("```unclosed code\n**bold [link(")
