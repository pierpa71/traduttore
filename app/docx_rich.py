"""Traduzione dei file Word mantenendo la formattazione a livello di singolo frammento.

Si modificano solo i testi: stili, font, colori, tabelle, immagini, forme, intestazioni,
piè di pagina, note, sezioni e impaginazione restano quelli del file originale.

Per ogni paragrafo:
  * formato unico e nessun elemento in linea  -> il testo tradotto sostituisce quello originale
  * più formati / link / immagini / tab / campi -> i frammenti vengono marcati con tag (vedi richtext)
    e il paragrafo viene ricostruito riusando le proprietà originali di ogni frammento
  * se il modello altera i tag -> si ripiega sulla sostituzione semplice (testo corretto,
    formattazione del primo frammento), senza perdere immagini o campi
"""
from __future__ import annotations

import copy
import io
import logging
import re
from collections import Counter

import docx
from lxml import etree

from . import richtext

log = logging.getLogger("traduttore")

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def w(tag: str) -> str:
    return f"{{{W}}}{tag}"


W_P, W_R, W_T, W_RPR, W_PPR = w("p"), w("r"), w("t"), w("rPr"), w("pPr")
A_P, A_T = f"{{{A}}}p", f"{{{A}}}t"

# contenitori di frammenti che si ricostruiscono (link, revisioni, campi semplici, content control)
WRAPPERS = {w("hyperlink"), w("ins"), w("smartTag"), w("fldSimple"), w("customXml"), w("dir"), w("bdo")}
FLOAT_START = {w("bookmarkStart"), w("commentRangeStart"), w("permStart"),
               w("moveFromRangeStart"), w("moveToRangeStart")}
FLOAT_END = {w("bookmarkEnd"), w("commentRangeEnd"), w("permEnd"),
             w("moveFromRangeEnd"), w("moveToRangeEnd")}
DROP = {w("proofErr"), w("lastRenderedPageBreak")}
# proprietà che non cambiano l'aspetto: non devono generare tag
IGNORED_RPR = {w("lang"), w("noProof"), w("rtl"), w("specVanish"), w("kern"), w("spacing"), w("w"),
               w("position"), w("szCs"), w("bCs"), w("iCs"), w("webHidden")}

SPACE_RUN = re.compile(r"([ \u00a0]{3,})")

# parti del pacchetto che contengono testo
WORD_PARTS = re.compile(r"^/word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$")
DRAWING_PARTS = re.compile(r"^/word/(charts/chart\d*|diagrams/(data|drawing)\d*)\.xml$")


def _own(p, p_tag, t_tag):
    """Elementi di testo che appartengono a questo paragrafo (non a paragrafi annidati)."""
    out = []
    for t in p.iter(t_tag):
        anc = t.getparent()
        while anc is not None and anc.tag != p_tag:
            anc = anc.getparent()
        if anc is p:
            out.append(t)
    return out


def _simple_replace(ts, text):
    ts[0].text = text
    ts[0].set(XML_SPACE, "preserve")
    for t in ts[1:]:
        t.text = ""


def _rpr_key(rpr) -> bytes:
    """Chiave del formato di un frammento: solo ciò che cambia l'aspetto in modo visibile."""
    if rpr is None:
        return b""
    r = copy.deepcopy(rpr)
    for c in list(r):
        if c.tag in IGNORED_RPR:
            r.remove(c)
    fonts = r.find(w("rFonts"))
    if fonts is not None:     # "Times-Roman" e "Times New Roman" sono lo stesso carattere a vista
        fam = (fonts.get(w("ascii")) or fonts.get(w("hAnsi")) or "").lower()
        fam = re.sub(r"[^a-z]", "", fam.split(",")[0].split("-")[0])
        fam = {"times": "timesnewroman", "arialmt": "arial", "helvetica": "arial"}.get(fam, fam)
        for k in list(fonts.attrib):
            del fonts.attrib[k]
        fonts.set(w("ascii"), fam)
    sz = r.find(w("sz"))
    if sz is not None and sz.get(w("val"), "").isdigit():   # mezze dimensioni arrotondate al punto intero
        sz.set(w("val"), str(round(int(sz.get(w("val"))) / 2) * 2))
    return etree.tostring(r, method="c14n")


def _fld_delta(run) -> tuple[int, int]:
    begins = ends = 0
    for fc in run.iter(w("fldChar")):
        t = fc.get(w("fldCharType"))
        begins += t == "begin"
        ends += t == "end"
    return begins, ends


class _DrawingPara:
    """Paragrafo DrawingML (grafici, SmartArt): sostituzione semplice."""

    def __init__(self, p):
        self.ts = _own(p, A_P, A_T)
        self.segment = "".join(t.text or "" for t in self.ts)

    def apply(self, text):
        _simple_replace(self.ts, text)


class _WordPara:
    def __init__(self, p):
        self.p = p
        self.ts = _own(p, W_P, W_T)
        self.plain = "".join(t.text or "" for t in self.ts)
        self.items: list[tuple] = []     # ("text", key, str, rpr, chain) | ("anchor", kind, payload, rpr, chain)
        self.floats_start, self.floats_end = [], []
        self.complex = False             # struttura non gestibile -> sostituzione semplice
        self._field: list | None = None
        self._depth = 0
        for child in p:
            if child.tag != W_PPR:
                self._walk(child, ())
        if self._depth or self._field:
            self.complex = True          # campo che continua in altri paragrafi (es. sommario)
        self._prepare()

    # ---------------------------------------------------------------- analisi
    def _walk(self, el, chain):
        tag = el.tag
        if not isinstance(tag, str):
            return                       # commenti XML / processing instruction
        if self._field is not None:      # dentro un campo: si conserva tutto com'è
            self._field.append(el)
            if tag == W_R or el.find(f".//{w('fldChar')}") is not None:
                b, e = _fld_delta(el)
                self._depth += b - e
                if self._depth <= 0:
                    self.items.append(("anchor", "whole", self._field, None, chain))
                    self._field, self._depth = None, 0
            return
        if tag == W_R:
            b, e = _fld_delta(el)
            if b > e:                    # inizio di un campo (PAGE, REF, DATE...): ancora unica
                self._field, self._depth = [el], b - e
                return
            self._run(el, chain)
        elif tag in WRAPPERS:
            for c in el:
                self._walk(c, chain + (el,))
        elif tag == w("sdt"):
            content = el.find(w("sdtContent"))
            if content is None:
                self.items.append(("anchor", "whole", [el], None, chain))
            else:
                for c in content:
                    self._walk(c, chain + (el, content))
        elif tag in FLOAT_START:
            self.floats_start.append(el)
        elif tag in FLOAT_END:
            self.floats_end.append(el)
        elif tag in DROP:
            pass
        else:                            # formule, revisioni eliminate, oggetti sconosciuti
            self.items.append(("anchor", "whole", [el], None, chain))

    def _run(self, r, chain):
        rpr = r.find(W_RPR)
        key = (_rpr_key(rpr), tuple(id(e) for e in chain))
        for c in r:
            if c.tag == W_RPR or c.tag in DROP or not isinstance(c.tag, str):
                continue
            if c.tag == W_T:
                # le sequenze di 3+ spazi servono a impaginare (colonne, rientri): diventano
                # segnaposto, così il modello non le vede e vengono rimesse identiche
                for part in SPACE_RUN.split(c.text or ""):
                    if not part:
                        continue
                    if SPACE_RUN.fullmatch(part):
                        self.items.append(("anchor", "space", part, rpr, chain))
                    elif self.items and self.items[-1][0] == "text" and self.items[-1][1] == key:
                        k, kk, txt, rp, ch = self.items[-1]
                        self.items[-1] = (k, kk, txt + part, rp, ch)
                    else:
                        self.items.append(("text", key, part, rpr, chain))
            else:
                self.items.append(("anchor", "child", c, rpr, chain))

    def _prepare(self):
        chars = Counter()
        for it in self.items:
            if it[0] == "text":
                chars[it[1]] += len(it[2])
        default = chars.most_common(1)[0][0] if chars else None
        self.formats = {default: 0}      # chiave formato -> id tag
        self.fmt_info = {0: None}        # id -> (rpr, chain)
        self.anchors = []
        pieces = []
        for it in self.items:
            if it[0] == "text":
                if it[1] not in self.formats:
                    self.formats[it[1]] = len(self.formats)
                fid = self.formats[it[1]]
                self.fmt_info[fid] = (it[3], it[4])
                pieces.append(("text", fid, it[2]))
            else:
                self.anchors.append(it)
                pieces.append(("anchor", len(self.anchors)))
        # niente da marcare: testo semplice (massima fedeltà, nessun rischio)
        self.simple = self.complex or (len(self.formats) == 1 and not self.anchors)
        if self.complex and self.anchors:
            log.debug("Paragrafo con campo su più paragrafi: sostituzione semplice")
        self.segment = self.plain if self.simple else richtext.encode(pieces)

    # ---------------------------------------------------------------- ricostruzione
    def apply(self, translated: str):
        if self.simple:
            _simple_replace(self.ts, richtext.strip_tags(translated))
            return
        pieces = richtext.decode(translated, len(self.formats))
        if not richtext.anchors_ok(pieces, len(self.anchors)):
            log.info("Tag alterati dal modello: paragrafo con formattazione semplificata")
            _simple_replace(self.ts, richtext.strip_tags(translated).replace(richtext.LT_ESC, "<"))
            return

        p = self.p
        for child in list(p):
            if child.tag != W_PPR:
                p.remove(child)
        for el in self.floats_start:
            p.append(el)

        stack: list[tuple] = []          # [(originale, clone)] contenitori aperti

        def container(chain):
            k = 0
            while k < len(stack) and k < len(chain) and stack[k][0] is chain[k]:
                k += 1
            del stack[k:]
            for orig in chain[k:]:
                clone = etree.Element(orig.tag, attrib=dict(orig.attrib), nsmap=orig.nsmap)
                for c in orig:           # proprietà del contenitore (es. w:sdtPr)
                    if isinstance(c.tag, str) and (c.tag.endswith("Pr") or c.tag.endswith("EndPr")):
                        clone.append(copy.deepcopy(c))
                (stack[-1][1] if stack else p).append(clone)
                stack.append((orig, clone))
            return stack[-1][1] if stack else p

        def new_run(rpr):
            r = etree.Element(W_R)
            if rpr is not None:
                r.append(copy.deepcopy(rpr))
            return r

        default_info = next((self.fmt_info[f] for f in self.fmt_info if self.fmt_info[f]), (None, ()))
        for pc in pieces:
            if pc[0] == "text":
                rpr, chain = self.fmt_info.get(pc[1]) or default_info
                r = new_run(rpr)
                t = etree.SubElement(r, W_T)
                t.text = pc[2]
                t.set(XML_SPACE, "preserve")
                container(chain).append(r)
            else:
                _, kind, payload, rpr, chain = self.anchors[pc[1] - 1]
                parent = container(chain)
                if kind == "space":
                    r = new_run(rpr)
                    t = etree.SubElement(r, W_T)
                    t.text = payload
                    t.set(XML_SPACE, "preserve")
                    parent.append(r)
                elif kind == "child":
                    r = new_run(rpr)
                    r.append(payload)
                    parent.append(r)
                else:
                    for el in payload:
                        parent.append(el)
        for el in self.floats_end:
            p.append(el)


class DocxDoc:
    kind = "docx"

    def __init__(self, data: bytes):
        self.doc = docx.Document(io.BytesIO(data))
        self.roots = []                  # (part, root, generico?)
        for part in self.doc.part.package.iter_parts():
            name = str(part.partname)
            if not (WORD_PARTS.match(name) or DRAWING_PARTS.match(name)):
                continue
            if hasattr(part, "_element"):
                self.roots.append((part, part._element, False))
            else:
                self.roots.append((part, etree.fromstring(part.blob), True))

        self.paras = []
        for _, root, _ in self.roots:
            for p in root.iter(W_P):
                para = _WordPara(p)
                if para.plain.strip():
                    self.paras.append(para)
            for p in root.iter(A_P):
                para = _DrawingPara(p)
                if para.segment.strip():
                    self.paras.append(para)
        self.segments = [p.segment for p in self.paras]

    def validate(self, idx: int, text: str) -> bool:
        para = self.paras[idx]
        if isinstance(para, _DrawingPara) or para.simple:
            return True
        return richtext.check(text, len(para.formats), len(para.anchors))

    def render(self, tr: list[str], fmt: str):
        from .documents import MIME, _to_txt
        if fmt == "txt":
            return _to_txt([richtext.strip_tags(t) for t in tr]), "txt", MIME["txt"]
        for para, text in zip(self.paras, tr):
            para.apply(text)
        for part, root, generic in self.roots:
            if generic:
                part._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        buf = io.BytesIO()
        self.doc.save(buf)
        return buf.getvalue(), "docx", MIME["docx"]
