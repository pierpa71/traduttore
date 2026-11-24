"""Client LLM: supporta Ollama (API nativa) ed endpoint compatibili OpenAI."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

# Dentro un container "localhost" è il container stesso: se LOCALHOST_ALIAS è impostato
# (lo fa il Dockerfile) gli indirizzi localhost/127.0.0.1 vengono girati verso l'host Docker.
LOCALHOST_ALIAS = os.getenv("LOCALHOST_ALIAS", "").strip()


def _resolve_localhost(url: str) -> str:
    if "://" not in url:
        url = "http://" + url
    if not LOCALHOST_ALIAS:
        return url
    parts = urlsplit(url)
    if parts.hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        netloc = LOCALHOST_ALIAS + (f":{parts.port}" if parts.port else "")
        return urlunsplit(parts._replace(netloc=netloc))
    return url


@dataclass
class LLMConfig:
    provider: str          # "ollama" | "openai"
    base_url: str          # es. http://localhost:11434  oppure  https://api.openai.com/v1
    model: str
    api_key: str = ""
    temperature: float = 0.2
    num_ctx: int = 8192     # solo Ollama: finestra di contesto (0 = default del modello)
    no_think: bool = True   # solo Ollama: disattiva il "ragionamento" dei modelli che lo supportano
    # secondi massimi di silenzio tra un frammento e l'altro della risposta (include il caricamento del modello)
    read_timeout: float = float(os.getenv("LLM_READ_TIMEOUT", "900"))

    def headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    @property
    def root(self) -> str:
        return _resolve_localhost(self.base_url.strip().rstrip("/"))

    def timeout(self) -> httpx.Timeout:
        return httpx.Timeout(connect=15, read=self.read_timeout, write=60, pool=15)


class LLMError(RuntimeError):
    pass


def _unreachable(cfg: "LLMConfig", e: Exception) -> LLMError:
    msg = f"Endpoint non raggiungibile ({cfg.root}): {e}"
    if LOCALHOST_ALIAS and LOCALHOST_ALIAS in cfg.root:
        msg += (" — L'app gira in Docker: per raggiungere Ollama sull'host, Ollama deve ascoltare su "
                "tutte le interfacce (OLLAMA_HOST=0.0.0.0) oppure usa l'IP/nome del server.")
    return LLMError(msg)


def _openai_root(root: str) -> str:
    # accetta sia "http://host:8000" sia "http://host:8000/v1"
    return root if root.endswith("/v1") or "/v1/" in root or root.endswith("/openai") else root + "/v1"


# callback(tipo, n_caratteri): tipo = "content" (testo della risposta) o "thinking" (ragionamento)
Activity = Callable[[str, int], None]


async def chat(cfg: LLMConfig, system: str, user: str, on_activity: Activity | None = None) -> str:
    """Invia la richiesta in streaming, così l'avanzamento è visibile mentre il modello scrive."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    notify = on_activity or (lambda kind, n: None)
    async with httpx.AsyncClient(timeout=cfg.timeout()) as client:
        try:
            if cfg.provider == "ollama":
                body = {"model": cfg.model, "messages": messages, "stream": True,
                        "options": {"temperature": cfg.temperature}}
                if cfg.num_ctx:
                    body["options"]["num_ctx"] = cfg.num_ctx
                if cfg.no_think:
                    body["think"] = False
                try:
                    return await _ollama_stream(client, cfg, body, notify)
                except LLMError as e:
                    # versioni/modelli che non accettano il parametro "think": si riprova senza
                    if "think" in body and "think" in str(e).lower():
                        body.pop("think")
                        return await _ollama_stream(client, cfg, body, notify)
                    raise
            body = {"model": cfg.model, "messages": messages, "temperature": cfg.temperature, "stream": True}
            return await _openai_stream(client, cfg, body, notify)
        except httpx.ReadTimeout as e:
            raise LLMError(f"Il modello non ha risposto per {cfg.read_timeout:.0f} secondi "
                           "(modello troppo lento o bloccato). Prova blocchi più piccoli o un modello più leggero.") from e
        except httpx.RequestError as e:
            raise _unreachable(cfg, e) from e
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"Risposta inattesa dall'endpoint: {e}") from e


async def _raise_for_status(r: httpx.Response):
    if r.status_code >= 400:
        text = (await r.aread()).decode("utf-8", "replace")[:400]
        raise LLMError(f"Errore HTTP {r.status_code} dall'endpoint: {text}")


async def _ollama_stream(client, cfg, body, notify) -> str:
    out = []
    async with client.stream("POST", f"{cfg.root}/api/chat", headers=cfg.headers(), json=body) as r:
        await _raise_for_status(r)
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            j = json.loads(line)
            if j.get("error"):
                raise LLMError(f"Errore da Ollama: {j['error']}")
            msg = j.get("message") or {}
            if msg.get("thinking"):
                notify("thinking", len(msg["thinking"]))
            if msg.get("content"):
                out.append(msg["content"])
                notify("content", len(msg["content"]))
            if j.get("done"):
                break
    return "".join(out)


async def _openai_stream(client, cfg, body, notify) -> str:
    out = []
    url = f"{_openai_root(cfg.root)}/chat/completions"
    async with client.stream("POST", url, headers=cfg.headers(), json=body) as r:
        await _raise_for_status(r)
        if "text/event-stream" not in r.headers.get("content-type", ""):
            # il server ha ignorato stream=true e ha risposto in un colpo solo
            j = json.loads(await r.aread())
            text = j["choices"][0]["message"]["content"] or ""
            notify("content", len(text))
            return text
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            j = json.loads(data)
            if j.get("error"):
                raise LLMError(f"Errore dall'endpoint: {j['error']}")
            if not j.get("choices"):
                continue
            delta = j["choices"][0].get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                notify("thinking", len(reasoning))
            if delta.get("content"):
                out.append(delta["content"])
                notify("content", len(delta["content"]))
    return "".join(out)


async def list_models(cfg: LLMConfig) -> list[str]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(20)) as client:
        try:
            if cfg.provider == "ollama":
                r = await client.get(f"{cfg.root}/api/tags", headers=cfg.headers())
                r.raise_for_status()
                return sorted(m["name"] for m in r.json().get("models", []))
            r = await client.get(f"{_openai_root(cfg.root)}/models", headers=cfg.headers())
            r.raise_for_status()
            return sorted(m["id"] for m in r.json().get("data", []))
        except httpx.HTTPStatusError as e:
            raise LLMError(f"Errore HTTP {e.response.status_code}: {e.response.text[:300]}") from e
        except httpx.RequestError as e:
            raise _unreachable(cfg, e) from e
