"""Estrazione dei segmenti dai documenti e ricostruzione del documento tradotto.

Ogni formato è gestito con lo stesso schema:
    doc = load_xxx(bytes)            -> oggetto con .segments (list[str])
    out = doc.render(translations, fmt) -> (bytes, estensione, mime)
"""
from __future__ import annotations

import io
import re

import docx
from docx.shared import Pt

MIME = {
    "txt": "text/plain; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}

SUPPORTED = {".txt", ".md", ".docx", ".pdf"}


# --------------------------------------------------------------------------- helper output
def _segments_to_docx(paragraphs: list[str | None]) -> bytes:
    """None = interruzione di pagina."""
    d = docx.Document()
    d.styles["Normal"].font.size = Pt(11)
    for p in paragraphs:
        if p is None:
            d.add_page_break()
        else:
            d.add_paragraph(p)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _to_txt(paragraphs: list[str | None]) -> bytes:
    return "\n\n".join("\n" + "-" * 40 if p is None else p for p in paragraphs).encode("utf-8")


# --------------------------------------------------------------------------- TXT / MD
class TextDoc:
    kind = "txt"

    def __init__(self, data: bytes, ext: str):
        self.ext = ext
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        text = text.replace("\r\n", "\n")
        # separa in paragrafi conservando i separatori (righe vuote)
        self.parts = re.split(r"(\n[ \t]*\n+)", text)
        self.segments = [p if i % 2 == 0 else "" for i, p in enumerate(self.parts)]

    def render(self, tr: list[str], fmt: str):
        merged = [tr[i] if i % 2 == 0 else p for i, p in enumerate(self.parts)]
        if fmt == "docx":
            paras = [tr[i] for i in range(0, len(self.parts), 2) if tr[i].strip()]
            return _segments_to_docx(paras), "docx", MIME["docx"]
        return "".join(merged).encode("utf-8"), self.ext.lstrip("."), MIME["txt"]


# --------------------------------------------------------------------------- entry point
def pdf_to_docx(data: bytes) -> bytes:
    """Converte un PDF in Word mantenendo impaginazione, rientri, font, tabelle e immagini."""
    import logging as _lg
    import os
    import tempfile
    from pdf2docx import Converter
    _lg.getLogger("pdf2docx").setLevel(_lg.ERROR)
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = os.path.join(tmp, "in.pdf"), os.path.join(tmp, "out.docx")
        with open(src, "wb") as f:
            f.write(data)
        cv = Converter(src)
        try:
            cv.convert(dst, multi_processing=False)
        finally:
            cv.close()
        with open(dst, "rb") as f:
            return f.read()


def load(filename: str, data: bytes):
    from .docx_rich import DocxDoc
    from .pdf_rich import PdfDoc

    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext in (".txt", ".md"):
        return TextDoc(data, ext)
    if ext == ".docx":
        return DocxDoc(data)
    if ext == ".pdf":
        return PdfDoc(data)
    if ext == ".doc":
        raise ValueError("Il formato .doc (Word 97-2003) non è supportato: salvalo come .docx.")
    raise ValueError(f"Formato non supportato: {ext or 'sconosciuto'}. Usa .txt, .md, .docx o .pdf.")
