"""Traduzione a blocchi di segmenti di testo, preservando l'allineamento 1:1."""
from __future__ import annotations

import asyncio
import logging
import re
import time

from .llm import LLMConfig, chat
from .richtext import TOKEN_RE, strip_format_tags, strip_tags

log = logging.getLogger("traduttore")

MARKER_RE = re.compile(r"^\s*\[\[(\d+)\]\]\s*$", re.MULTILINE)
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# segmenti senza lettere (numeri, punteggiatura, simboli) non vengono inviati al modello
NOTHING_TO_TRANSLATE = re.compile(r"^[\W\d_]*$", re.UNICODE)



_WORD = re.compile(r"[^\W\d_]{4,}", re.UNICODE)


def untranslated_parts(src: str, out: str) -> int:
    """Quante frasi del sorgente (>= 3 parole) compaiono identiche nella traduzione.

    Serve a riconoscere il difetto tipico dei modelli locali: il testo dentro i tag di
    formattazione (o un'intera parte del paragrafo) lasciato nella lingua originale.
    """
    from .richtext import decode
    if not strip_tags(out).strip():
        return 0
    bad = 0
    for p in decode(src, 1000):
        if p[0] != "text":
            continue
        text = " ".join(p[2].split())
        words = _WORD.findall(text)
        # frase (non un nome proprio): almeno 3 parole e qualche parola minuscola (articoli, preposizioni...)
        prose = any(t[0].islower() for t in re.findall(r"[^\W\d_]+", text))
        out_norm = " ".join(TOKEN_RE.sub(" ", out).split())   # i tag separano le parole
        if len(words) >= 3 and prose and text in out_norm:
            bad += 1
        elif p[1] and any(w[0].islower() for w in words) and re.search(
                r"(?<![^\W\d_])" + re.escape(text.strip()) + r"(?![^\W\d_])", out_norm):
            bad += 1        # parola formattata (dentro un tag) lasciata nella lingua originale
    if not bad:   # etichetta breve restituita identica (es. "Targa Assicurato:")
        sw = [w for w in _WORD.findall(strip_tags(src)) if not w.isupper()]
        if len(sw) >= 2 and " ".join(strip_tags(out).split()) == " ".join(strip_tags(src).split()):
            bad = 1
    if not bad:   # paragrafo lasciato in buona parte nella lingua originale (anche tutto maiuscolo)
        sw = _WORD.findall(strip_tags(src))
        if len(sw) >= 3:
            ow = set(_WORD.findall(strip_tags(out)))      # confronto sensibile alle maiuscole
            if sum(w in ow for w in sw) / len(sw) > 0.5:
                bad = 1
    return bad


RETRY_NOTE = ("\n\nIMPORTANT: a previous answer left some text untranslated (often the text inside "
              "<tN> tags). Translate ALL the text, including every word inside the tags.")


class Stats(dict):
    """Stato di avanzamento condiviso con l'API (letto dall'interfaccia ogni secondo)."""

    def __init__(self):
        super().__init__(batches_done=0, batches_total=0, chars_total=0, chars_out=0,
                         thinking=0, fallbacks=0, last_activity=time.time(), phase="avvio",
                         recent=[], seq=0)

    def add_pair(self, orig: str, tr: str):
        """Ultimi paragrafi tradotti (per il flusso dal vivo nell'interfaccia)."""
        o, t = strip_tags(orig).strip(), strip_tags(tr).strip()
        if len(o) < 3:
            return
        self["seq"] += 1
        self["recent"] = (self["recent"] + [{"n": self["seq"], "o": o[:400], "t": t[:400]}])[-8:]

    def activity(self, kind: str, n: int):
        self["last_activity"] = time.time()
        if kind == "thinking":
            self["thinking"] += n
            self["phase"] = "ragionamento"
        else:
            self["chars_out"] += n
            self["phase"] = "scrittura"


def _system_prompt(source: str, target: str, glossary: str) -> str:
    src = f"from {source} " if source and source.lower() != "auto" else ""
    p = (
        f"You are a professional translator. Translate the text {src}into {target}.\n"
        "Rules:\n"
        "- Output ONLY the translation, with no comments, notes or explanations.\n"
        "- Preserve meaning, tone and register. Keep numbers, codes, URLs, e-mail addresses, "
        "product names and placeholders unchanged.\n"
        "- Preserve line breaks and list markers.\n"
        "- If a segment is already in the target language, return it unchanged.\n"
        "- Some text contains inline formatting tags such as <t1>...</t1> (they only mean bold, italic, a "
        "different font, a link...) and placeholders such as <x1/> (a line break, tab or image). "
        "The text INSIDE the tags is normal text and MUST be translated too. Keep every tag and placeholder "
        "exactly as written, put each tag pair around the translated words that correspond to the originally "
        "tagged words, and keep placeholders in the same order. Never add, drop or rename tags.\n"
        "  Example (Italian -> English): 'Il <t1>gatto nero</t1> dorme.<x1/><t2>Data:</t2> oggi' -> "
        "'The <t1>black cat</t1> is sleeping.<x1/><t2>Date:</t2> today'"
    )
    if glossary.strip():
        p += "\n- Use this glossary / these instructions strictly:\n" + glossary.strip()
    return p


def _batch_instructions() -> str:
    return (
        "\n\nThe input is split into segments, each preceded by a marker line like [[1]], [[2]] ... "
        "Return EVERY marker on its own line, in the same order, each followed by the translation "
        "of its segment. Never merge, split, drop or renumber segments. Do not translate the markers."
    )


def _clean(text: str) -> str:
    text = THINK_RE.sub("", text).strip()
    # alcuni modelli racchiudono la risposta in ``` ... ```
    if text.startswith("```") and text.endswith("```"):
        text = text.strip("`").split("\n", 1)[-1].strip()
    return text


def _make_batches(items: list[tuple[int, str]], max_chars: int) -> list[list[tuple[int, str]]]:
    batches, cur, size = [], [], 0
    for idx, txt in items:
        if cur and size + len(txt) > max_chars:
            batches.append(cur)
            cur, size = [], 0
        cur.append((idx, txt))
        size += len(txt) + 8
    if cur:
        batches.append(cur)
    return batches


async def _translate_single(cfg: LLMConfig, system: str, text: str, stats: Stats) -> str:
    out = _clean(await chat(cfg, system, text, stats.activity))
    return out or text


async def _translate_batch(cfg, system, batch, stats, validate=None, same_language=False) -> dict[int, str]:
    res = await _translate_batch_raw(cfg, system, batch, stats)

    def score(idx, src, out):
        # (parti non tradotte, tag alterati): più basso è meglio; non tradurre è peggio che perdere un grassetto
        left = 0 if same_language else untranslated_parts(src, out)
        tags_bad = 0 if (validate is None or validate(idx, out)) else 1
        return (left, tags_bad)

    for idx, txt in batch:
        best = score(idx, txt, res[idx])
        if best == (0, 0):
            continue
        stats["retries"] = stats.get("retries", 0) + 1
        why = f"{best[0]} parti non tradotte" if best[0] else "tag alterati"
        log.info("Paragrafo %d (%s): nuovo tentativo singolo", idx, why)
        again = await _translate_single(cfg, system + (RETRY_NOTE if best[0] else ""), txt, stats)
        sc = score(idx, txt, again)
        if sc < best:
            res[idx], best = again, sc
        if best[0]:
            # ancora testo non tradotto: si riprova senza i tag di formato (restano a capo, tab, spazi)
            plain = strip_format_tags(txt)
            if plain != txt:
                log.info("Paragrafo %d: terzo tentativo senza tag di formato", idx)
                third = await _translate_single(cfg, system + RETRY_NOTE, plain, stats)
                sc = score(idx, txt, third)
                if sc[0] < best[0]:
                    res[idx], best = third, sc
        if best[0]:
            stats["untranslated"] = stats.get("untranslated", 0) + 1
            log.warning("Paragrafo %d: parte del testo è rimasta non tradotta", idx)
    return res


async def _translate_batch_raw(cfg: LLMConfig, system: str, batch: list[tuple[int, str]], stats: Stats) -> dict[int, str]:
    if len(batch) == 1:
        idx, txt = batch[0]
        return {idx: await _translate_single(cfg, system, txt, stats)}

    payload = "\n".join(f"[[{n}]]\n{txt}" for n, (_, txt) in enumerate(batch, 1))
    raw = _clean(await chat(cfg, system + _batch_instructions(), payload, stats.activity))

    parts = MARKER_RE.split(raw)
    # parts = [prefisso, num, testo, num, testo, ...]
    found: dict[int, str] = {}
    for i in range(1, len(parts) - 1, 2):
        found[int(parts[i])] = parts[i + 1].strip()

    if set(found) == set(range(1, len(batch) + 1)) and all(found.values()):
        return {idx: found[n] for n, (idx, _) in enumerate(batch, 1)}

    # Il modello non ha rispettato i marcatori: si ripiega segmento per segmento.
    log.warning("Marcatori non rispettati (attesi %d, trovati %d): traduzione paragrafo per paragrafo",
                len(batch), len(found))
    stats["fallbacks"] += 1
    stats["chars_total"] += sum(len(t) for _, t in batch)   # il lavoro si ripete
    result = {}
    for idx, txt in batch:
        result[idx] = await _translate_single(cfg, system, txt, stats)
    return result


async def translate_segments(
    cfg: LLMConfig,
    segments: list[str],
    source: str,
    target: str,
    glossary: str = "",
    max_chars: int = 3000,
    concurrency: int = 1,
    stats: Stats | None = None,
    validate=None,
) -> list[str]:
    same_language = bool(source) and source.lower() != "auto" and source.strip().lower() == target.strip().lower()
    stats = stats if stats is not None else Stats()
    system = _system_prompt(source, target, glossary)
    out = list(segments)
    todo = [(i, s) for i, s in enumerate(segments)
            if strip_tags(s).strip() and not NOTHING_TO_TRANSLATE.match(strip_tags(s).strip())]
    batches = _make_batches(todo, max_chars)
    stats.update(batches_total=len(batches), chars_total=sum(len(t) for _, t in todo), phase="in attesa del modello")
    log.info("Traduzione: %d segmenti, %d caratteri, %d blocchi, modello %s @ %s",
             len(todo), stats["chars_total"], len(batches), cfg.model, cfg.root)
    sem = asyncio.Semaphore(max(1, concurrency))

    async def run(n, batch):
        async with sem:
            t0 = time.time()
            stats["phase"] = "in attesa del modello"
            log.info("Blocco %d/%d: %d paragrafi, %d caratteri", n, len(batches), len(batch),
                     sum(len(t) for _, t in batch))
            res = await _translate_batch(cfg, system, batch, stats, validate, same_language)
            log.info("Blocco %d/%d completato in %.1fs", n, len(batches), time.time() - t0)
        for idx, txt in res.items():
            # conserva gli spazi iniziali/finali originali del segmento
            orig = segments[idx]
            lead = orig[: len(orig) - len(orig.lstrip())]
            trail = orig[len(orig.rstrip()):]
            out[idx] = lead + txt + trail
            stats.add_pair(orig, txt)
        stats["batches_done"] += 1

    await asyncio.gather(*(run(n, b) for n, b in enumerate(batches, 1)))
    stats["phase"] = "ricostruzione documento"
    return out
