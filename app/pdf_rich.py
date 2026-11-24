"""Traduzione dei PDF mantenendo l'impaginazione.

Immagini, disegni, tabelle (linee e sfondi), link e tutto ciò che non è testo restano intatti.
Per ogni blocco di testo si cancella solo il testo originale (senza coprire lo sfondo) e si
reinserisce la traduzione nello stesso riquadro con:
  * il font originale incorporato nel PDF, se contiene tutti i caratteri necessari,
    altrimenti un font della stessa famiglia (con grazie / senza grazie / monospaziato);
  * dimensione, colore, grassetto, corsivo, apice delle singole parti di testo;
  * allineamento (sinistra, destra, centrato, giustificato), interlinea e rotazione.
Se la traduzione è più lunga il carattere viene ridotto quanto basta per farla stare nel riquadro.
I blocchi che non cambiano (numeri, codici) non vengono toccati.
"""
from __future__ import annotations

import html
import logging
import re
import statistics
from collections import Counter

import pymupdf

from . import richtext

log = logging.getLogger("traduttore")
logging.getLogger("fontTools").setLevel(logging.ERROR)

SERIF = re.compile(r"times|serif|georgia|garamond|cambria|roman|minion|palatino|baskerville|"
                   r"century|bodoni|caslon|didot|charter|constantia|libre ?baskerville|merriweather", re.I)
MONO = re.compile(r"courier|mono|consol|menlo|lucida ?console|code", re.I)
BOLD = re.compile(r"bold|black|heavy|semibold|demi", re.I)
ITALIC = re.compile(r"italic|oblique", re.I)
USABLE_FONT_EXT = {"ttf", "otf", "cff"}
# sostituti metricamente compatibili (stessa larghezza dei caratteri)
METRIC_ALIASES = {
    "calibri": "Carlito", "cambria": "Caladea", "arial": "Liberation Sans", "helvetica": "Liberation Sans",
    "arialmt": "Liberation Sans", "timesnewroman": "Liberation Serif", "times": "Liberation Serif",
    "couriernew": "Liberation Mono", "courier": "Liberation Mono", "verdana": "DejaVu Sans",
    "tahoma": "DejaVu Sans", "segoeui": "Liberation Sans", "georgia": "Liberation Serif",
}


def _with_cmap(buf: bytes, mapping: dict[int, int]) -> bytes | None:
    """Aggiunge a un font incorporato (sottoinsieme senza cmap) la tabella Unicode -> glifo,
    ricostruita da come il PDF usa i glifi. Così il font originale diventa riutilizzabile."""
    try:
        import io as _io
        from fontTools.ttLib import TTFont
        from fontTools.ttLib.tables._c_m_a_p import CmapSubtable, table__c_m_a_p
        font = TTFont(_io.BytesIO(buf), lazy=False)
        order = font.getGlyphOrder()
        cmap = {}
        if "cmap" in font:
            try:
                cmap.update(font["cmap"].getBestCmap() or {})
            except Exception:
                pass
        for uni, gid in mapping.items():
            if 32 <= uni and 0 <= gid < len(order) and gid != 0:
                cmap.setdefault(uni, order[gid])
        if not cmap:
            return None
        table = table__c_m_a_p()
        table.tableVersion = 0
        table.tables = []
        bmp = {u: g for u, g in cmap.items() if u <= 0xFFFF}
        for pid, eid in ((3, 1), (0, 3)):
            sub = CmapSubtable.newSubtable(4)
            sub.platformID, sub.platEncID, sub.language, sub.cmap = pid, eid, 0, dict(bmp)
            table.tables.append(sub)
        if len(bmp) < len(cmap):
            sub = CmapSubtable.newSubtable(12)
            sub.platformID, sub.platEncID, sub.language, sub.cmap = 3, 10, 0, dict(cmap)
            table.tables.append(sub)
        font["cmap"] = table
        out = _io.BytesIO()
        font.save(out)
        return out.getvalue()
    except Exception as e:
        log.debug("cmap non ricostruibile: %s", e)
        return None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def _family_style(base: str, flags: int) -> tuple[str, bool, bool]:
    """'TimesNewRomanPS-BoldItalicMT' -> ('Times New Roman', True, True)"""
    fam = re.split(r"[-,]", base, maxsplit=1)[0]
    fam = re.sub(r"(PS|MT|PSMT)$", "", fam)
    fam = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", fam).strip()
    bold = bool(flags & 16 or BOLD.search(base))
    italic = bool(flags & 2 or ITALIC.search(base))
    return fam, bold, italic


class FontResolver:
    """Trova un file di font di sistema adatto (via fontconfig) che contenga tutti i caratteri."""

    def __init__(self):
        import shutil
        self.available = shutil.which("fc-match") is not None
        self._match: dict[str, list[str]] = {}
        self._fonts: dict[str, pymupdf.Font | None] = {}

    def _fc(self, pattern: str) -> list[str]:
        if pattern not in self._match:
            import subprocess
            try:
                out = subprocess.run(["fc-match", "-s", "-f", "%{file}|%{family}\n", pattern],
                                     capture_output=True, text=True, timeout=10).stdout
                self._match[pattern] = [ln for ln in out.splitlines() if ln.split("|")[0].endswith((".ttf", ".otf", ".ttc"))][:40]
            except Exception:
                self._match[pattern] = []
        return self._match[pattern]

    def font(self, path: str):
        if path not in self._fonts:
            try:
                self._fonts[path] = pymupdf.Font(fontfile=path)
            except Exception:
                self._fonts[path] = None
        return self._fonts[path]

    def _candidates(self, base: str, flags: int) -> tuple[list[str], list[str]]:
        """(stessa famiglia o metricamente compatibile, famiglia generica)"""
        fam, bold, italic = _family_style(base, flags)
        key = _norm(fam)
        generic = "monospace" if (MONO.search(base) or flags & 8) else \
                  "serif" if (SERIF.search(base) or flags & 4) else "sans-serif"
        style = (":weight=bold" if bold else "") + (":slant=italic" if italic else "")
        wanted = METRIC_ALIASES.get(key) or METRIC_ALIASES.get(_norm(fam.split()[0]) if fam else "")
        close = []
        if wanted:
            close += self._fc(wanted + style)[:1]
        first = self._fc(fam + style)[:1] if fam else []
        # fontconfig restituisce sempre qualcosa: si accetta solo se la famiglia corrisponde
        if first and _norm(first[0].split("|")[1].split(",")[0])[:5] == key[:5]:
            close += first
        rest = sorted(self._fc(generic + style), key=lambda c: "unifont" in c.lower())
        return [c.split("|")[0] for c in close], [c.split("|")[0] for c in rest]

    def covers(self, path: str, chars) -> bool:
        f = self.font(path)
        return bool(f) and all(f.has_glyph(ord(ch)) for ch in chars)

    def resolve(self, base: str, flags: int, text: str, close_only: bool = False) -> str | None:
        if not self.available:
            return None
        close, rest = self._candidates(base, flags)
        chars = {c for c in text if not c.isspace()}
        for path in close + ([] if close_only else rest):
            if self.covers(path, chars):
                return path
        return None


def _base(name: str) -> str:
    return name.split("+", 1)[1] if re.match(r"^[A-Z]{6}\+", name or "") else (name or "")


LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def _virtual_spans(line, columns=()) -> list[dict]:
    """Span ricostruiti dai singoli caratteri (get_text("rawdict")).

    Molti generatori (Reporting Services, gestionali, vecchi Word) allineano colonne e rientri
    con sequenze di spazi invece che con la posizione del testo. Qui ogni sequenza di spazi più
    larga di 1,5 volte il corpo del carattere separa due frammenti; gli spazi in testa diventano
    rientro e il riquadro di ogni frammento copre solo i caratteri visibili.
    """
    out = []
    for s in line["spans"]:
        chars = s.get("chars")
        if chars is None:                 # dict normale (senza caratteri): si usa così com'è
            out.append(s)
            continue
        size = s["size"] or 10
        seg: list = []

        def flush():
            if not seg:
                return
            vis = [c for c in seg if not c["c"].isspace()]
            ref = vis or seg
            r = pymupdf.Rect(ref[0]["bbox"])
            for c in ref[1:]:
                r |= c["bbox"]
            v = {k: val for k, val in s.items() if k != "chars"}
            v["text"] = "".join(c["c"] for c in seg)
            v["bbox"] = tuple(r)
            v["origin"] = ref[0]["origin"]
            out.append(v)
            seg.clear()

        i, n = 0, len(chars)
        while i < n:
            if chars[i]["c"].isspace():
                j = i
                while j < n and chars[j]["c"].isspace():
                    j += 1
                x0 = chars[i]["bbox"][0]
                x1 = chars[j]["bbox"][0] if j < n else chars[j - 1]["bbox"][2]
                at_column = j < n and j - i >= 2 and any(abs(x1 - c) <= 3 for c in columns)
                if x1 - x0 > 1.5 * size or at_column:   # spazio "di impaginazione": separa i frammenti
                    flush()
                else:
                    seg.extend(chars[i:j])
                i = j
            else:
                seg.append(chars[i])
                i += 1
        flush()
    return out


def _units_from_block(block) -> list[list[tuple]]:
    """Divide un blocco in unità di testo e le raggruppa in paragrafi.

    Unità = sequenza di span sulla stessa riga senza spazi larghi (tab, colonne di tabella,
    immagini in mezzo). Due righe vengono unite nello stesso paragrafo solo se la prima
    "va a capo" davvero (la prima parola della riga successiva non ci stava), oppure se
    sono centrate / allineate a destra sullo stesso asse. Così rientri di prima riga,
    rientri sporgenti, elenchi e paragrafi consecutivi vengono riconosciuti correttamente.
    Ritorna una lista di paragrafi; ogni paragrafo è una lista di righe [(line, [span...]), ...].
    """
    # colonne: posizioni in cui, su almeno due righe, il testo riparte dopo una spaziatura larga
    starts = Counter()
    for ln in block["lines"]:
        vs = [v for v in _virtual_spans(ln) if v["text"].strip()]
        for prev_v, v in zip(vs, vs[1:]):
            if v["bbox"][0] - prev_v["bbox"][2] > v["size"]:
                starts[round(v["bbox"][0])] += 1
    columns = [x for x, c in starts.items() if c >= 2]
    lines = []
    for ln in block["lines"]:
        spans = [s for s in _virtual_spans(ln, columns) if s["text"]]
        if not "".join(s["text"] for s in spans).strip():
            continue
        units, cur = [], []
        for s in spans:
            if cur:
                gap = s["bbox"][0] - cur[-1]["bbox"][2]
                at_column = gap > 0.4 * s["size"] and any(abs(s["bbox"][0] - c) <= 3 for c in columns)
                if gap > max(cur[-1]["size"], s["size"]) * 0.9 or at_column:
                    units.append(cur)
                    cur = []
            cur.append(s)
        if cur:
            units.append(cur)
        units = [u for u in units if "".join(s["text"] for s in u).strip()]
        lines.append((ln, units))

    def text_of(u):
        return "".join(s["text"] for s in u)

    def rect(u):
        r = pymupdf.Rect(u[0]["bbox"])
        for s in u[1:]:
            r |= s["bbox"]
        return r

    def first_word_width(u):
        s0 = u[0]
        t = s0["text"].lstrip()
        if not t:
            return 0
        word = re.split(r"[ \t]+", t)[0]   # gli spazi non separabili tengono unite le parole
        return (s0["bbox"][2] - s0["bbox"][0]) * len(word) / max(1, len(s0["text"]))

    # bordo destro del testo del blocco (righe con una sola unità di testo)
    singles = [rect(us[0]) for _, us in lines if len([u for u in us if LETTER.search(text_of(u))]) == 1]
    block_right = max((r.x1 for r in singles), default=block["bbox"][2])

    paras: list[list[tuple]] = []
    prev = None                         # (paragrafo, ultima unità, rect, n. unità "vere" sulla riga)
    for ln, units in lines:
        real = [u for u in units if LETTER.search(text_of(u))]
        for u in units:
            r = rect(u)
            size = statistics.median([s["size"] for s in u])
            merged = False
            if prev and u is (real[0] if real else None) and len(real) == 1 and prev[3] == 1:
                para, pu, pr = prev[0], prev[1], prev[2]
                psize = statistics.median([s["size"] for s in pu])
                baseline_gap = u[0]["origin"][1] - pu[0]["origin"][1]
                tol = max(1.5, 0.2 * size)
                same_flow = (0 < baseline_gap <= 1.8 * max(size, psize) and abs(size - psize) <= 0.6
                             and min(r.x1, pr.x1) - max(r.x0, pr.x0) > 0 and ln["dir"] == para[-1][0]["dir"])
                wraps = pr.x1 + first_word_width(u) + 0.25 * size >= block_right - tol
                centered = abs((r.x0 + r.x1) / 2 - (pr.x0 + pr.x1) / 2) <= tol and abs(r.x0 - pr.x0) > tol
                righted = abs(r.x1 - pr.x1) <= tol and abs(r.x0 - pr.x0) > tol
                if len(para) == 1:      # seconda riga: rientro di prima riga o sporgente ammesso
                    aligned = abs(r.x0 - pr.x0) <= 6 * size
                else:                   # righe successive: allineate alla seconda
                    r2 = rect(para[1][1])
                    aligned = abs(r.x0 - r2.x0) <= tol or centered or righted
                if same_flow and aligned and (wraps or centered or righted):
                    para.append((ln, u))
                    merged = True
            if not merged:
                paras.append([(ln, u)])
            if LETTER.search(text_of(u)):
                prev = (paras[-1], u, r, len(real))
            elif not real:
                prev = None
    return paras


class _Block:
    """Paragrafo di testo PDF (una o più righe) con i pezzi formattati."""

    def __init__(self, pno, rows, hlines=()):
        self.pno = pno
        self.decor_lines = []            # sottolineature/barrature originali (da rimuovere)
        for _, u in rows:
            vis = [sp for sp in u if sp["text"].strip()]
            extent = (min((sp["bbox"][0] for sp in vis), default=0), max((sp["bbox"][2] for sp in vis), default=0))
            for sp in u:
                sp["deco"] = self._find_deco(sp, hlines, extent)
        self.rows = rows                 # [(line, [span...])]
        self.rect = pymupdf.Rect(rows[0][1][0]["bbox"])
        for _, u in rows:
            for s in u:
                self.rect |= s["bbox"]
        # --- pezzi formattati (con unione delle righe e de-sillabazione)
        items: list[list] = []          # [chiave, testo, span]
        self.all_spans = [(sp, self._key(sp)) for _, u in rows for sp in u]
        for li, (_ln, spans) in enumerate(rows):
            first = True
            for s in spans:
                txt = s["text"]
                if first and li > 0 and items:
                    prev = items[-1]
                    prev[1] = prev[1].rstrip()
                    if prev[1].endswith("-") and txt.lstrip()[:1].islower():
                        prev[1] = prev[1][:-1]
                        txt = txt.lstrip()
                    else:
                        prev[1] += " "
                        txt = txt.lstrip()
                first = False
                key = self._key(s)
                if items and items[-1][0] == key:
                    items[-1][1] += txt
                else:
                    items.append([key, txt, s])
        # gli spazi tra parole con formato diverso non devono generare tag a sé
        merged_items: list[list] = []
        for it in items:
            if not it[1].strip() and merged_items:
                merged_items[-1][1] += it[1]
            else:
                merged_items.append(it)
        items = merged_items
        chars = Counter()
        for k, t, _ in items:
            chars[k] += len(t.strip())
        default = chars.most_common(1)[0][0] if chars else None
        self.formats = {default: 0}
        self.spans = {}                 # id formato -> span di riferimento
        pieces = []
        for k, t, s in items:
            if k not in self.formats:
                self.formats[k] = len(self.formats)
            fid = self.formats[k]
            self.spans.setdefault(fid, s)
            pieces.append(("text", fid, t))
        if pieces:
            pieces[0] = ("text", pieces[0][1], pieces[0][2].lstrip())
            pieces[-1] = ("text", pieces[-1][1], pieces[-1][2].rstrip())
        self.segment = richtext.encode([p for p in pieces if p[2]])
        self.plain = richtext.strip_tags(self.segment)
        self.size = statistics.median([s["size"] for _, u in rows for s in u if s["text"].strip()] or [10])
        self.single_align = "left"
        self.indent = self._indent()
        self.align = self._alignment()
        self.line_height = self._line_height()
        self.rotate = self._rotation()

    def _find_deco(self, sp, hlines, extent) -> str:
        """Sottolineatura o barratura disegnata sotto/sopra la span (vale per tutta la span)."""
        if not sp["text"].strip():
            return ""
        sb = pymupdf.Rect(sp["bbox"])
        base, size = sp["origin"][1], sp["size"]
        found = set()
        for ln, half in hlines:
            if min(ln.x1, sb.x1) - max(ln.x0, sb.x0) < sb.width * 0.5:
                continue
            # una sottolineatura non va oltre il testo della riga (i bordi di cella sì)
            if ln.x0 < extent[0] - size * 0.6 or ln.x1 > extent[1] + size * 0.6:
                continue
            if base - size * 0.15 <= ln.y0 <= base + size * 0.35:
                found.add("underline")
                self.decor_lines.append((ln, half))
            elif base - size * 0.6 <= ln.y0 < base - size * 0.15:
                found.add("line-through")
                self.decor_lines.append((ln, half))
        return " ".join(sorted(found))

    @staticmethod
    def _key(s):
        # famiglia visiva (Times-Roman = Times New Roman), stile e corpo al mezzo punto
        fam, bold, italic = _family_style(_base(s["font"]), s["flags"])
        fam = _norm(fam)
        fam = {"times": "timesnewroman", "arial": "arial", "helvetica": "arial"}.get(fam, fam)
        return (fam, bold, italic, round(s["size"] * 2) / 2, s["color"], s["flags"] & 1, s.get("deco", ""))

    def redact_rects(self):
        """Area dei soli glifi di ogni riga (dalla linea di base), per non toccare le righe vicine."""
        out = []
        for _, u in self.rows:
            vis = [s for s in u if s["text"].strip()]
            if not vis:
                continue
            x0 = min(s["bbox"][0] for s in vis)
            x1 = max(s["bbox"][2] for s in vis)
            base = statistics.median([s["origin"][1] for s in vis])
            size = max(s["size"] for s in vis)
            out.append(pymupdf.Rect(x0 - 0.3, base - size * 0.78, x1 + 0.3, base + size * 0.22))
        return out

    def _line_rects(self):
        out = []
        for _, u in self.rows:
            r = pymupdf.Rect(u[0]["bbox"])
            for s in u[1:]:
                r |= s["bbox"]
            out.append(r)
        return out

    def _alignment(self) -> str:
        ls = self._line_rects()
        if len(ls) < 2:
            return self.single_align
        tol = max(1.5, self.size * 0.15)
        same = lambda v: max(v) - min(v) <= tol
        body = ls[1:]                    # la prima riga può avere un rientro
        rights = [r.x1 for r in ls]
        centers = [(r.x0 + r.x1) / 2 for r in ls]
        if same(centers) and not same([r.x0 for r in ls]):
            return "center"
        if same(rights) and not same([r.x0 for r in body] + [ls[0].x0]) and abs(self.indent) < tol:
            return "right"
        if len(ls) >= 3 and same(rights[:-1]):
            return "justify"
        return "left"

    def _indent(self) -> float:
        """Rientro della prima riga rispetto alle successive (>0 rientro, <0 sporgente)."""
        ls = self._line_rects()
        if len(ls) < 2:
            return 0.0
        rest = statistics.median([r.x0 for r in ls[1:]])
        d = ls[0].x0 - rest
        return d if abs(d) > max(1.5, self.size * 0.15) else 0.0

    def _line_height(self) -> float:
        ys = [u[0]["origin"][1] for _, u in self.rows]
        gaps = [b - a for a, b in zip(ys, ys[1:]) if b > a]
        if not gaps:
            return 1.2
        return max(1.0, min(2.2, statistics.median(gaps) / self.size))

    def _rotation(self) -> int:
        dx, dy = self.rows[0][0]["dir"]
        if abs(dx) >= abs(dy):
            return 0 if dx > 0 else 180
        return 90 if dy < 0 else 270


class PdfDoc:
    kind = "pdf"

    def __init__(self, data: bytes):
        self.data = data
        self.blocks: list[_Block] = []
        with pymupdf.open(stream=data, filetype="pdf") as pdf:
            if pdf.needs_pass:
                raise ValueError("Il PDF è protetto da password.")
            self._glyph_maps: dict[str, dict[int, int]] = {}
            for pno, page in enumerate(pdf):
                try:      # come il PDF usa i glifi di ogni font: Unicode -> id del glifo
                    for tr_span in page.get_texttrace():
                        gm = self._glyph_maps.setdefault(_base(tr_span["font"]), {})
                        for ch in tr_span["chars"]:
                            if ch[0] > 0 and ch[1] >= 0:
                                gm.setdefault(ch[0], ch[1])
                except Exception:
                    pass
                page_blocks: list[_Block] = []
                hlines = []
                try:
                    for dr in page.get_drawings():
                        r = pymupdf.Rect(dr["rect"])
                        if r.height <= 1.6 and r.width > 2:
                            hlines.append((r, (dr.get("width") or 0) / 2))
                except Exception:
                    pass
                d = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES)
                for b in d["blocks"]:
                    if b.get("type") != 0:
                        continue
                    for rows in _units_from_block(b):
                        blk = _Block(pno, rows, hlines)
                        if blk.plain.strip():
                            page_blocks.append(blk)
                self._single_line_alignment(page_blocks, page.rect.width)
                self.blocks += page_blocks
        if not self.blocks:
            raise ValueError("Nessun testo estraibile: il PDF sembra una scansione (serve OCR).")
        self.segments = [b.segment for b in self.blocks]

    @staticmethod
    def _single_line_alignment(blocks, page_width):
        """Righe isolate: centrate o a destra rispetto ai margini del testo della pagina."""
        multi = [b for b in blocks if len(b.rows) > 1] or blocks
        if not multi:
            return
        left = min(b.rect.x0 for b in multi)
        right = max(b.rect.x1 for b in multi)
        tol = max(2.0, page_width * 0.006)
        for b in blocks:
            if len(b.rows) != 1:
                continue
            r = b.rect
            gap_l, gap_r = r.x0 - left, right - r.x1
            if gap_l > 3 * b.size and abs(gap_l - gap_r) <= tol:
                b.align = b.single_align = "center"
            elif gap_r <= tol and gap_l > (right - left) * 0.25:
                b.align = b.single_align = "right"

    def validate(self, idx: int, text: str) -> bool:
        return richtext.check(text, len(self.blocks[idx].formats), 0)

    # ------------------------------------------------------------------ uscite alternative
    def _paragraphs(self, tr):
        out, last = [], 0
        for blk, t in zip(self.blocks, tr):
            if blk.pno != last:
                out.append(None)
                last = blk.pno
            out.append(richtext.strip_tags(t))
        return out

    def render(self, tr: list[str], fmt: str):
        from .documents import MIME, _segments_to_docx, _to_txt
        if fmt == "docx":
            return _segments_to_docx(self._paragraphs(tr)), "docx", MIME["docx"]
        if fmt == "txt":
            return _to_txt(self._paragraphs(tr)), "txt", MIME["txt"]
        return self._render_pdf(tr), "pdf", MIME["pdf"]

    # ------------------------------------------------------------------ PDF
    def _font_table(self, pdf, page):
        """nome base del font -> xref, per i font incorporati riutilizzabili (TrueType/OpenType)."""
        table = {}
        for xref, ext, _type, basefont, *_ in page.get_fonts(full=True):
            if ext not in USABLE_FONT_EXT:
                continue
            if xref not in self._font_cache:
                try:
                    _n, fext, _t, buf = pdf.extract_font(xref)
                    entry = None
                    if buf:
                        font = pymupdf.Font(fontbuffer=buf)
                        gmap = self._glyph_maps.get(_base(basefont), {})
                        # sottoinsiemi senza cmap: si ricostruisce la tabella dai glifi usati nel PDF
                        if fext in ("ttf", "otf") and gmap and not all(font.has_glyph(u) for u in list(gmap)[:20]):
                            fixed = _with_cmap(buf, gmap)
                            if fixed:
                                buf, font = fixed, pymupdf.Font(fontbuffer=fixed)
                        entry = (font, buf, fext)
                    self._font_cache[xref] = entry
                except Exception:
                    self._font_cache[xref] = None
            if self._font_cache[xref]:
                table[_base(basefont)] = xref
        return table

    def _register(self, archive, used, fam, data, ext):
        if fam not in used:
            archive.add(data, f"{fam}.{ext}")
            used[fam] = f"{fam}.{ext}"

    def _styled_runs(self, span, text, fonts, archive, used) -> list[tuple[str, str]]:
        """Divide un pezzo di testo in sotto-parti (css, testo) scegliendo il font.

        1) font incorporato nel PDF se ha tutti i caratteri;
        2) stessa famiglia installata o font metricamente compatibile (es. Calibri -> Carlito);
        3) font incorporato + solo i caratteri mancanti da un font simile;
        4) famiglia generica (con grazie / senza grazie / monospaziato).
        """
        name = _base(span["font"])
        r, g, b = (span["color"] >> 16) & 255, (span["color"] >> 8) & 255, span["color"] & 255
        base = [f"font-size:{span['size']:.2f}px", f"color:rgb({r},{g},{b})"]
        exact = lambda fam: ";".join(base + [f"font-family:{fam}", "font-weight:normal", "font-style:normal"])
        chars = {c for c in text if not c.isspace()}

        xref = fonts.get(name)
        emb = self._font_cache.get(xref) if xref else None      # (Font, bytes, ext)
        if emb and all(emb[0].has_glyph(ord(c)) for c in chars):
            self._register(archive, used, f"f{xref}", emb[1], emb[2])
            return [(exact(f"f{xref}"), text)]

        def system(path):
            fam = "s" + _norm(path.rsplit("/", 1)[-1])
            self._register(archive, used, fam, open(path, "rb").read(), path.rsplit(".", 1)[-1])
            return exact(fam)

        path = self._resolver.resolve(name, span["flags"], text, close_only=True)
        if path:
            return [(system(path), text)]

        if emb:   # il font originale resta, solo i caratteri mancanti da un font simile
            missing = "".join(c for c in chars if not emb[0].has_glyph(ord(c)))
            fb = self._resolver.resolve(name, span["flags"], missing)
            if fb:
                self._register(archive, used, f"f{xref}", emb[1], emb[2])
                orig_css, fb_css = exact(f"f{xref}"), system(fb)
                runs: list[tuple[str, str]] = []
                for ch in text:
                    css = orig_css if (ch.isspace() or emb[0].has_glyph(ord(ch))) else fb_css
                    if runs and runs[-1][0] == css:
                        runs[-1] = (css, runs[-1][1] + ch)
                    else:
                        runs.append((css, ch))
                return runs

        path = self._resolver.resolve(name, span["flags"], text)
        if path:
            return [(system(path), text)]
        generic = "monospace" if (MONO.search(name) or span["flags"] & 8) else \
                  "serif" if (SERIF.search(name) or span["flags"] & 4) else "sans-serif"
        bold = span["flags"] & 16 or BOLD.search(name)
        italic = span["flags"] & 2 or ITALIC.search(name)
        return [(";".join(base + [f"font-family:{generic}", f"font-weight:{'bold' if bold else 'normal'}",
                                  f"font-style:{'italic' if italic else 'normal'}"]), text)]

    # ------------------------------------------------------------------ geometria della pagina
    @staticmethod
    def _obstacles(page, blocks):
        obs = [b.rect for b in blocks]
        try:
            obs += [pymupdf.Rect(i["bbox"]) for i in page.get_image_info()]
        except Exception:
            pass
        lines = []
        for d in page.get_drawings():
            r = pymupdf.Rect(d["rect"])
            obs.append(r)
            if r.height <= 1.6 and r.width > 2:
                lines.append((r, (d.get("width") or 0) / 2))   # (riquadro, metà spessore del tratto)
        # margini del testo della pagina: il testo tradotto non deve uscirne
        if blocks:
            obs_margin = (min(b.rect.x0 for b in blocks), max(b.rect.x1 for b in blocks))
        else:
            obs_margin = (28, page.rect.width - 28)
        return obs, lines, obs_margin

    @staticmethod
    def _free_box(blk, page, obstacles, margins):
        """Riquadro in cui scrivere: quello originale, allargato solo verso spazio libero."""
        r = blk.rect
        others = [o for o in obstacles if not (o.contains(r) or r.contains(o) or o == r)]
        vert = lambda o: min(o.y1, r.y1) - max(o.y0, r.y0) > 0.5
        horiz = lambda o: min(o.x1, r.x1) - max(o.x0, r.x0) > 0.5
        right = min([o.x0 for o in others if o.x0 >= r.x1 - 0.5 and vert(o)] + [max(margins[1], r.x1)])
        left = max([o.x1 for o in others if o.x1 <= r.x0 + 0.5 and vert(o)] + [min(margins[0], r.x0)])
        below = [o for o in others if horiz(o) and o.y0 > r.y1 - blk.size * 0.7 and o.y0 > r.y0 + 1]
        bottom = min([o.y0 for o in below] + [page.rect.height - 20])
        x0, x1 = r.x0, r.x1 + 1
        if blk.rotate == 0 and len(blk.rows) == 1:
            if blk.align == "center":
                grow = max(0, min(r.x0 - left, right - r.x1) - 1)
                x0, x1 = r.x0 - grow, r.x1 + grow
            elif blk.align == "right":
                x0 = max(left + 1, r.x0 - r.width)
            else:
                x1 = max(x1, right - 1)
        if len(blk.rows) == 1:
            # riga singola: niente a capo, se serve il carattere si riduce leggermente
            y1 = r.y1 + min(1.0, max(0, bottom - r.y1 - 0.5))
        else:
            y1 = max(r.y1 + 0.5, min(bottom - 0.5, r.y1 + max(blk.size * 1.2, r.height * 0.5)))
        return pymupdf.Rect(x0, r.y0, x1, y1)

    @staticmethod
    def _align_baseline(page, blk, box, body, css, archive):
        """Sposta il riquadro in verticale perché la prima riga cada sulla linea di base originale.

        Il motore HTML posiziona il testo con le metriche del font usato, diverse da quelle del PDF:
        si compone il blocco su una pagina di prova, si misura la prima linea di base e si corregge.
        """
        if blk.rotate:
            return box
        target = min(sp["origin"][1] for sp in blk.rows[0][1] if sp["text"].strip())
        tmp = pymupdf.open()
        try:
            tp = tmp.new_page(width=page.rect.width, height=page.rect.height)
            tp.insert_htmlbox(box, body, css=css, archive=archive, scale_low=0)
            lines = [ln for b in tp.get_text("dict")["blocks"] for ln in b.get("lines", [])
                     if "".join(s["text"] for s in ln["spans"]).strip()]
            if not lines:
                return box
            first = min(s["origin"][1] for s in lines[0]["spans"])
        finally:
            tmp.close()
        dy = target - first
        if abs(dy) < 0.2 or abs(dy) > blk.size:
            return box
        return pymupdf.Rect(box.x0, box.y0 + dy, box.x1, box.y1 + dy)

    def _render_pdf(self, tr: list[str]) -> bytes:
        pdf = pymupdf.open(stream=self.data, filetype="pdf")
        self._font_cache = {}
        self._resolver = FontResolver()
        by_page: dict[int, list] = {}
        page_blocks: dict[int, list] = {}
        unchanged: dict[int, list] = {}
        for blk, t in zip(self.blocks, tr):
            page_blocks.setdefault(blk.pno, []).append(blk)
            if richtext.strip_tags(t).strip() == blk.plain.strip():
                # invariato (numeri, nomi, codici): si lascia il testo originale
                unchanged.setdefault(blk.pno, []).append((blk, richtext.decode(blk.segment, len(blk.formats))))
                continue
            by_page.setdefault(blk.pno, []).append((blk, richtext.decode(t, len(blk.formats))))
        # un blocco invariato toccato dalla cancellazione di un vicino va ridisegnato
        # (a catena: anche la cancellazione di un blocco ridisegnato può toccarne altri)
        for pno, items in by_page.items():
            areas = [r for blk, _ in items for r in blk.redact_rects()]
            pending = unchanged.get(pno, [])
            while True:
                hit, keep = [], []
                for blk, pieces in pending:
                    full = [pymupdf.Rect(sp["bbox"]) for _, u in blk.rows for sp in u if sp["text"].strip()]
                    (hit if any(r.intersects(a) for r in full for a in areas) else keep).append((blk, pieces))
                if not hit:
                    break
                items.extend(hit)
                areas += [r for blk, _ in hit for r in blk.redact_rects()]
                pending = keep

        for pno, items in by_page.items():
            page = pdf[pno]
            fonts = self._font_table(pdf, page)
            obstacles, hlines, margins = self._obstacles(page, page_blocks[pno])
            boxes = {id(blk): self._free_box(blk, page, obstacles, margins) for blk, _ in items}
            decorations = [ln for blk, _ in items for ln in blk.decor_lines]

            # 1) via il testo originale (lo sfondo resta: fill=False)
            for blk, _ in items:
                for r in blk.redact_rects():
                    page.add_redact_annot(r, fill=False)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
            # 2) via le sole sottolineature/barrature del testo tradotto
            if decorations:
                for ln, half in decorations:
                    m = half + 0.5          # il tratto deve essere interamente coperto
                    page.add_redact_annot(ln + (-m, -m, m, m), fill=False)
                page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                      graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                                      text=pymupdf.PDF_REDACT_TEXT_NONE)

            # 3) testo tradotto
            for blk, pieces in items:
                archive, used = pymupdf.Archive(), {}
                spans_html = []
                for pc in pieces:
                    if pc[0] != "text" or not pc[2]:
                        continue
                    span = blk.spans.get(pc[1]) or blk.spans[0]
                    deco = (";text-decoration:" + span["deco"]) if span.get("deco") else ""
                    for css, part in self._styled_runs(span, pc[2], fonts, archive, used):
                        # gli spazi non separabili usati come spaziatura impedirebbero di andare a capo
                        text = html.escape(part.replace("\xa0", " ")).replace("\n", "<br>")
                        if span["flags"] & 1:
                            text = f"<sup>{text}</sup>"
                        spans_html.append(f'<span style="{css}{deco}">{text}</span>')
                faces = [f"@font-face {{font-family:{fam}; src:url({fn});}}" for fam, fn in used.items()]
                # NB: niente selettore "*": nel motore HTML di MuPDF gonfia l'altezza delle righe
                indent = ""
                if blk.indent > 0:
                    indent = f" text-indent:{blk.indent:.2f}px;"
                elif blk.indent < 0:     # rientro sporgente: la prima riga sporge a sinistra
                    indent = f" padding-left:{-blk.indent:.2f}px; text-indent:{blk.indent:.2f}px;"
                base_css = (f"body {{margin:0; padding:0;}} p {{margin:0; padding:0;}} "
                            f"p {{font-size:{blk.size:.2f}px; text-align:{blk.align}; "
                            f"line-height:{blk.line_height:.2f};{indent}}}")
                body = f"<p>{''.join(spans_html)}</p>"
                box = boxes[id(blk)]
                css_all = "\n".join(faces + [base_css])
                try:
                    box = self._align_baseline(page, blk, box, body, css_all, archive)
                    page.insert_htmlbox(box, body, css=css_all, archive=archive, rotate=blk.rotate, scale_low=0)
                except Exception as e:      # font non digeribile dal motore HTML: si riprova senza
                    log.warning("Pagina %d: font originale non utilizzabile (%s)", pno + 1, e)
                    body = re.sub(r"font-family:[fs][a-z0-9]+;font-weight:normal;font-style:normal",
                                  "font-family:sans-serif", body)
                    page.insert_htmlbox(box, body, css=base_css, rotate=blk.rotate, scale_low=0)
        out = pdf.tobytes(garbage=3, deflate=True)
        pdf.close()
        return out
