"""Text normalization / cleaning pipeline (Sprint 3, non-destructive).

Order (docs/architecture/data-pipeline.md):
    raw text -> HTML entity handling -> invisible-character handling
             -> Unicode NFC + whitespace collapse -> normalized text

Deliberately PRESERVES everything a future sentiment engine needs:
    - emojis (😂) and variation selectors / ZWJ sequences (family emoji)
    - punctuation runs (!!! ???)
    - hashtags (#tag), mentions (@user)
    - accented characters, non-Latin scripts, mixed-language text
    - ZWNJ (U+200C): orthographically meaningful in Persian/Indic text

`raw_text` in the database is NEVER touched by this pipeline - it always
holds exactly what YouTube returned; only `normalized_text` is produced here.
"""
import html
import unicodedata

from app.utils.normalize import collapse_whitespace

# Presentation-only invisibles that break text comparison/analysis. ZWJ
# (U+200D), ZWNJ (U+200C) and variation selectors are intentionally absent:
# they carry meaning inside emoji sequences and Indic/Persian orthography.
_INVISIBLE_CHARS = (
    "\u00ad",  # soft hyphen
    "\u200b",  # zero-width space
    "\u200e",  # left-to-right mark
    "\u200f",  # right-to-left mark
    "\u202a",  # left-to-right embedding
    "\u202b",  # right-to-left embedding
    "\u202c",  # pop directional formatting
    "\u202d",  # left-to-right override
    "\u202e",  # right-to-left override
    "\u2060",  # word joiner
    "\ufeff",  # zero-width no-break space / BOM
)
# str.translate needs ordinal keys.
_INVISIBLE = dict.fromkeys(ord(ch) for ch in _INVISIBLE_CHARS)


def strip_invisible(text: str) -> str:
    """Remove presentation-only invisible characters (emoji-safe)."""
    return text.translate(_INVISIBLE)


def normalize_comment_text(raw: str) -> str:
    """Full cleaning pipeline: raw YouTube text -> normalized text.

    Pure function; returns '' only when the input carries no real content
    (the ingestion layer treats that as a rejection, never as an empty row).
    """
    if not raw:
        return ""
    # 1. HTML entities YouTube sometimes returns escaped (&amp; &quot; ...).
    unescaped = html.unescape(raw)
    # 2. Presentation-only invisible characters.
    cleaned = strip_invisible(unescaped)
    # 3. Unicode NFC + whitespace collapse (shared with Sprint 2 helpers).
    normalized = collapse_whitespace(cleaned)
    # 4. Defensive: an all-invisible input collapses to '' - surface it as-is;
    #    the caller decides (rejection), we never invent replacement text.
    return normalized


def contains_multilingual_signal(text: str) -> bool:
    """True when the text contains non-Latin script letters (test/debug aid)."""
    return any(
        unicodedata.category(ch) == "Lo" and ord(ch) > 0x2E00 for ch in text
    )
