"""Marcatura della formattazione inline per l'LLM.

Un paragrafo è una sequenza di "pezzi":
    ("text", fmt, testo)   fmt = 0 formato prevalente (senza tag), >0 altri formati
    ("anchor", n)          elemento non testuale (immagine, tab, campo...) da tenere in posizione

Codifica:  "Il cluster <t1>Nutanix</t1> è pronto<x1/>"
Il modello traduce lasciando i tag; poi si decodifica e si riassegna ogni testo al suo formato.
"""
from __future__ import annotations

import re

TOKEN_RE = re.compile(r"<(/?)t(\d+)\s*>|<x(\d+)\s*/?\s*>", re.IGNORECASE)
# carattere usato per "neutralizzare" un '<' del testo originale che sembrerebbe un tag
LT_ESC = "‹"


def _esc(s: str) -> str:
    return s.replace("<", LT_ESC) if TOKEN_RE.search(s) else s


def encode(pieces: list[tuple]) -> str:
    out = []
    for p in pieces:
        if p[0] == "anchor":
            out.append(f"<x{p[1]}/>")
        elif p[1] == 0:
            out.append(_esc(p[2]))
        else:
            out.append(f"<t{p[1]}>{_esc(p[2])}</t{p[1]}>")
    return "".join(out)


def strip_tags(s: str) -> str:
    return TOKEN_RE.sub("", s)


def decode(s: str, n_formats: int) -> list[tuple]:
    """Tollerante: tag sconosciuti/sbilanciati vengono ignorati, il testo resta."""
    pieces: list[tuple] = []
    stack: list[int] = []
    pos = 0

    def add_text(txt):
        if not txt:
            return
        fmt = stack[-1] if stack else 0
        if pieces and pieces[-1][0] == "text" and pieces[-1][1] == fmt:
            pieces[-1] = ("text", fmt, pieces[-1][2] + txt)
        else:
            pieces.append(("text", fmt, txt))

    for m in TOKEN_RE.finditer(s):
        add_text(s[pos:m.start()])
        pos = m.end()
        if m.group(3):
            pieces.append(("anchor", int(m.group(3))))
        else:
            n = int(m.group(2))
            if m.group(1):          # chiusura
                if n in stack:
                    while stack and stack.pop() != n:
                        pass
            elif 0 < n < n_formats:
                stack.append(n)
    add_text(s[pos:])
    return [p if p[0] == "anchor" else ("text", p[1], p[2].replace(LT_ESC, "<")) for p in pieces]


def anchors_ok(pieces: list[tuple], n_anchors: int) -> bool:
    """Tutti i segnaposto presenti una volta sola e nell'ordine originale."""
    seq = [p[1] for p in pieces if p[0] == "anchor"]
    return seq == list(range(1, n_anchors + 1))


def check(translated: str, n_formats: int, n_anchors: int) -> bool:
    """La traduzione conserva segnaposto (in ordine) e tutti i formati marcati?"""
    pieces = decode(translated, n_formats)
    if not anchors_ok(pieces, n_anchors):
        return False
    used = {p[1] for p in pieces if p[0] == "text"}
    return all(f in used for f in range(1, n_formats))


FMT_TAG_RE = re.compile(r"</?t\d+\s*>", re.IGNORECASE)


def strip_format_tags(s: str) -> str:
    """Toglie i tag di formato <tN> ma lascia i segnaposto <xN/> (a capo, tab, immagini)."""
    return FMT_TAG_RE.sub("", s)
