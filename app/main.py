"""Traduttore di documenti con LLM (Ollama o endpoint compatibile OpenAI)."""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import documents
from .llm import LLMConfig, LLMError, list_models
from .translator import Stats, translate_segments

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("traduttore")

STATIC = Path(__file__).parent / "static"
MAX_MB = float(os.getenv("MAX_UPLOAD_MB", "50"))
JOB_TTL = 3600

DEFAULTS = {
    "provider": os.getenv("LLM_PROVIDER", "ollama"),
    "base_url": os.getenv("LLM_BASE_URL", "http://localhost:11434"),
    "model": (os.getenv("LLM_MODEL", "").split() or [""])[0],
    "has_api_key": bool(os.getenv("LLM_API_KEY")),
    "max_chars": int(os.getenv("CHUNK_CHARS", "2000")),
    "concurrency": int(os.getenv("CONCURRENCY", "1")),
    "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "8192")),
    "no_think": os.getenv("NO_THINK", "1") == "1",
}

app = FastAPI(title="Traduttore documenti LLM")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
JOBS: dict[str, dict] = {}


def _cfg(provider: str, base_url: str, model: str, api_key: str, temperature: float = 0.2,
         num_ctx: int = 0, no_think: bool = True) -> LLMConfig:
    if provider not in ("ollama", "openai"):
        raise HTTPException(400, "Provider non valido (ollama | openai).")
    if not base_url:
        raise HTTPException(400, "Indica l'URL dell'endpoint.")
    return LLMConfig(provider=provider, base_url=base_url, model=model,
                     api_key=api_key or os.getenv("LLM_API_KEY", ""), temperature=temperature,
                     num_ctx=max(0, num_ctx), no_think=no_think)


def _gc():
    now = time.time()
    for k in [k for k, j in JOBS.items() if now - j["created"] > JOB_TTL]:
        JOBS.pop(k, None)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/config")
def config():
    return DEFAULTS


class ModelsReq(BaseModel):
    provider: str
    base_url: str
    api_key: str = ""


@app.post("/api/models")
async def models(req: ModelsReq):
    try:
        return {"models": await list_models(_cfg(req.provider, req.base_url, "", req.api_key))}
    except LLMError as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/api/translate")
async def translate(
    file: UploadFile = File(...),
    target_lang: str = Form(...),
    source_lang: str = Form("auto"),
    provider: str = Form(...),
    base_url: str = Form(...),
    model: str = Form(...),
    api_key: str = Form(""),
    output_format: str = Form("same"),
    glossary: str = Form(""),
    temperature: float = Form(0.2),
    max_chars: int = Form(DEFAULTS["max_chars"]),
    concurrency: int = Form(DEFAULTS["concurrency"]),
    num_ctx: int = Form(DEFAULTS["num_ctx"]),
    no_think: bool = Form(DEFAULTS["no_think"]),
):
    _gc()
    if not model:
        raise HTTPException(400, "Seleziona un modello.")
    data = await file.read()
    if len(data) > MAX_MB * 1024 * 1024:
        raise HTTPException(413, f"File troppo grande (max {MAX_MB:.0f} MB).")
    try:
        doc = documents.load(file.filename or "", data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # file corrotto
        raise HTTPException(400, f"Impossibile leggere il documento: {e}")

    cfg = _cfg(provider, base_url, model, api_key, temperature, num_ctx, no_think)
    fmt = doc.kind if output_format == "same" else output_format
    if fmt not in ("txt", "docx", "pdf") or (fmt == "pdf" and doc.kind != "pdf"):
        raise HTTPException(400, "Formato di uscita non valido per questo documento.")

    job_id = uuid.uuid4().hex
    stats = Stats()
    job = JOBS[job_id] = {
        "created": time.time(), "status": "running", "error": None, "filename": file.filename,
        "result": None, "segments": sum(1 for s in doc.segments if s.strip()), "stats": stats,
        "source": (data, doc.kind), "source_lang": source_lang, "target_lang": target_lang,
    }
    log.info("Job %s: %s (%s, %d byte) -> %s, uscita %s", job_id[:8], file.filename, doc.kind,
             len(data), target_lang, fmt)

    async def run():
        nonlocal doc
        try:
            if doc.kind == "pdf" and fmt == "docx":
                # PDF -> Word con impaginazione, poi traduzione come un normale .docx
                stats["phase"] = "conversione del PDF in Word"
                from .docx_rich import DocxDoc
                converted = await asyncio.to_thread(documents.pdf_to_docx, data)
                doc = await asyncio.to_thread(DocxDoc, converted)
                job["segments"] = sum(1 for s in doc.segments if s.strip())
            tr = await translate_segments(
                cfg, doc.segments, source_lang, target_lang, glossary,
                max_chars=max(500, max_chars), concurrency=max(1, min(concurrency, 16)), stats=stats,
                validate=getattr(doc, "validate", None),
            )
            content, ext, mime = await asyncio.to_thread(doc.render, tr, fmt)
            stem = (file.filename or "documento").rsplit(".", 1)[0]
            job["result"] = (content, f"{stem}_{target_lang}.{ext}", mime)
            job["status"] = "done"
            log.info("Job %s completato in %.0fs", job_id[:8], time.time() - job["created"])
        except asyncio.CancelledError:
            log.info("Job %s annullato", job_id[:8])
            raise
        except LLMError as e:
            job["status"], job["error"] = "error", str(e)
            log.error("Job %s fallito: %s", job_id[:8], e)
        except Exception as e:
            job["status"], job["error"] = "error", f"{type(e).__name__}: {e}"
            log.exception("Job %s fallito", job_id[:8])

    job["task"] = asyncio.create_task(run())
    return {"job_id": job_id, "segments": job["segments"]}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "Job non trovato.")
    st, now = j["stats"], time.time()
    return {k: j[k] for k in ("status", "error", "filename", "segments")} | {
        "output_name": j["result"][1] if j["result"] else None,
        "done": st["batches_done"], "total": st["batches_total"],
        "chars_total": st["chars_total"], "chars_out": st["chars_out"], "thinking": st["thinking"],
        "fallbacks": st["fallbacks"], "phase": st["phase"],
        "retries": st.get("retries", 0), "untranslated": st.get("untranslated", 0),
        "elapsed": round(now - j["created"]), "idle": round(now - st["last_activity"]),
        "recent": st["recent"], "pages": _page_count(j),
    }


def _page_count(j) -> int:
    """Pagine in anteprima: solo quando originale e risultato sono PDF."""
    if not j["result"] or j["source"][1] != "pdf" or not j["result"][1].endswith(".pdf"):
        return 0
    if "pages" not in j:
        import pymupdf
        with pymupdf.open(stream=j["result"][0], filetype="pdf") as d:
            j["pages"] = d.page_count
    return j["pages"]


@app.get("/api/jobs/{job_id}/preview/{which}/{page}")
def preview(job_id: str, which: str, page: int, width: int = 900):
    """Immagine PNG di una pagina dell'originale (which=orig) o della traduzione (which=tr)."""
    j = JOBS.get(job_id)
    if not j or not _page_count(j) or which not in ("orig", "tr"):
        raise HTTPException(404, "Anteprima non disponibile.")
    import pymupdf
    data = j["source"][0] if which == "orig" else j["result"][0]
    with pymupdf.open(stream=data, filetype="pdf") as d:
        if not 0 <= page < d.page_count:
            raise HTTPException(404, "Pagina inesistente.")
        pg = d[page]
        zoom = max(0.5, min(3.0, width / pg.rect.width))
        png = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.delete("/api/jobs/{job_id}")
def job_cancel(job_id: str):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "Job non trovato.")
    if j["status"] == "running":
        j["task"].cancel()
        j["status"], j["error"] = "error", "Annullato dall'utente."
    return {"ok": True}


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str):
    j = JOBS.get(job_id)
    if not j or not j["result"]:
        raise HTTPException(404, "Risultato non disponibile.")
    content, name, mime = j["result"]
    return Response(content, media_type=mime,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
