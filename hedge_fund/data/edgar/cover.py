"""Per-class cover-page share counts from a filing's rendered cover (R1.htm).

SEC's companyfacts API omits dimensioned facts, so a registrant that reports
shares outstanding per class (Berkshire A/B) has no usable total there. The
filing's own rendered cover page lists each class with its count; it is
small, immutable once filed, and fetched at most once per filing.
"""

from __future__ import annotations

import html
import re

_LABEL = "Entity Common Stock, Shares Outstanding"
_MEMBER = re.compile(r"([^\[\]]{1,80}?)\s*\[Member\]")
_CLASS = re.compile(r"\bClass\s+([A-Z])\b")
_NUMBER = re.compile(r"^\s*([\d,]+)")


def _text(document: str) -> str:
    text = re.sub(r"<[^>]+>", " ", document)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text)


def parse_cover_shares(document: str) -> dict[str | None, float]:
    """{class letter (or member label, or None if entity-wide): shares}."""
    text = _text(document)
    out: dict[str | None, float] = {}
    cursor = 0
    while True:
        i = text.find(_LABEL, cursor)
        if i < 0:
            break
        after = text[i + len(_LABEL):]
        number = _NUMBER.match(after)
        segment = text[cursor:i]
        cursor = i + len(_LABEL)
        if not number:
            continue
        members = _MEMBER.findall(segment)
        key: str | None = None
        if members:
            label = members[-1].strip()
            letters = _CLASS.findall(label)  # last one: nearest the [Member] tag
            key = letters[-1] if letters else label
        out[key] = out.get(key, 0.0) + float(number.group(1).replace(",", ""))
    return out


_SYMBOL = re.compile(r"Trading Symbol\s+([A-Za-z][A-Za-z0-9.\-]{0,9})\b")


def parse_trading_symbols(document: str) -> list[str]:
    """Trading symbols on a rendered cover page (dei:TradingSymbol), in
    order, upper-cased and de-duplicated. Empty if the filer tagged none."""
    out: list[str] = []
    for sym in _SYMBOL.findall(_text(document)):
        sym = sym.upper().rstrip(".-")
        if sym and sym not in out and sym not in ("NONE", "N"):
            out.append(sym)
    return out


# Pre-2019 filings rarely tag dei:TradingSymbol, but the 10-K text names the
# listing in Item 5 ("listed on the New York Stock Exchange under the ticker
# symbol "DIS"", "(NYSE: XYZ)"). These patterns read that sentence only.
_TEXT_SYMBOL = [
    re.compile(r"(?:under|using)\s+the\s+(?:ticker\s+|trading\s+)?symbols?\s*[:\-]?\s*[\"\u201c\u201d'\u2018\u2019(]*\s*"
               r"([A-Z]{1,5}(?:[.\-][A-Z])?)\b"),
    re.compile(r"\(\s*(?:NYSE|NASDAQ|Nasdaq|NYSE\s+MKT|NYSE\s+Amex|AMEX)\s*:\s*([A-Z]{1,5}(?:\.[A-Z])?)\s*\)"),
]
_NOT_SYMBOLS = {"A", "I", "THE", "AND", "OF", "NYSE", "NASDAQ", "AMEX", "MKT", "US", "USA", "INC", "CO", "LLC", "PLC"}


def parse_text_symbols(document: str) -> list[str]:
    """Ticker symbols stated in a filing's text (Item 5 listing sentence), in order."""
    text = _text(document)
    out: list[str] = []
    for pattern in _TEXT_SYMBOL:
        for sym in pattern.findall(text):
            sym = sym.upper().rstrip(".-")
            if sym and sym not in _NOT_SYMBOLS and sym not in out:
                out.append(sym)
    return out


def primary_document(index_html: str, forms: tuple[str, ...]) -> str | None:
    """File name of the first document whose type is one of *forms* in an EDGAR filing index."""
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", index_html, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) >= 4 and cells[3] in forms and cells[2].lower().endswith((".htm", ".html", ".txt")):
            return cells[2].split()[0]
    return None
