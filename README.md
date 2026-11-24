# Traduttore documenti LLM

Applicazione web che traduce documenti **TXT / Markdown / Word (.docx) / PDF** in una lingua a scelta
usando un LLM configurabile: **Ollama** (API nativa) oppure qualsiasi endpoint **compatibile OpenAI**
(OpenAI, Azure OpenAI tramite gateway, vLLM, LM Studio, LiteLLM, llama.cpp server, Ollama `/v1`, ecc.).

## Avvio con Docker

L'immagine contiene solo l'applicazione (interfaccia, librerie, font: nessuna risorsa esterna).
Si collega al **tuo** Ollama o a un qualsiasi endpoint compatibile OpenAI, che si sceglie dall'interfaccia.

```bash
docker compose up -d --build          # poi apri http://localhost:8080
```
oppure senza compose:
```bash
docker build -t traduttore .
docker run -d --name traduttore -p 8080:8080 --add-host=host.docker.internal:host-gateway traduttore
```

### Raggiungere Ollama
- **Ollama su un altro server**: nell'interfaccia metti `http://IP-o-nome:11434`.
- **Ollama sulla stessa macchina del container**: puoi scrivere `http://localhost:11434`; l'app lo gira
  automaticamente verso l'host (`host.docker.internal`). Perché funzioni, Ollama deve ascoltare anche
  sull'interfaccia Docker, non solo su 127.0.0.1:
  ```bash
  # Linux con systemd
  sudo systemctl edit ollama      # aggiungi:  [Service]  Environment="OLLAMA_HOST=0.0.0.0"
  sudo systemctl restart ollama
  ```
  (Docker Desktop su Windows/Mac non ne ha bisogno.)
  In alternativa, su Linux, usa la rete dell'host: in `docker-compose.yml` togli `ports`/`extra_hosts`,
  aggiungi `network_mode: host` e `LOCALHOST_ALIAS: ""`.

### Valori predefiniti (facoltativi)
Endpoint e modello si scelgono dalla pagina; le variabili servono solo come valori iniziali
(copia `.env.example` in `.env`):

| Variabile | Default | Significato |
|---|---|---|
| `PORT` | `8080` | porta pubblicata |
| `LLM_PROVIDER` | `ollama` | `ollama` oppure `openai` |
| `LLM_BASE_URL` | `http://localhost:11434` | URL proposto nella pagina |
| `LLM_MODEL` | — | modello proposto |
| `LLM_API_KEY` | — | chiave usata se il campo nella pagina è vuoto (resta sul server) |
| `CHUNK_CHARS` | `2000` | caratteri per richiesta |
| `OLLAMA_NUM_CTX` | `8192` | contesto passato a Ollama (0 = default del modello) |
| `NO_THINK` | `1` | disattiva il "thinking" dei modelli che ragionano |
| `LLM_READ_TIMEOUT` | `900` | secondi senza risposta prima di dare errore |
| `LOG_LEVEL` | `INFO` | dettaglio dei log (`DEBUG` per tutto) |
| `CONCURRENCY` | `1` | richieste in parallelo |
| `MAX_UPLOAD_MB` | `50` | dimensione massima del file |
| `LOCALHOST_ALIAS` | `host.docker.internal` | dove girare gli URL `localhost` (vuoto = disattivato) |

Endpoint di salute: `GET /api/health`.

### Trasferire l'immagine su una macchina senza Internet
```bash
docker save traduttore:latest | gzip > traduttore.tar.gz
gunzip -c traduttore.tar.gz | docker load
```

### Senza Docker
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Uso
L'interfaccia è una sola pagina:
- in alto a destra la **pillola del modello** mostra se l'endpoint è collegato; un clic apre il pannello
  delle impostazioni;
- la **coppia di lingue** in grande (si inverte con il pulsante centrale);
- il **foglio** su cui trascinare il documento, con la scelta di cosa ricevere;
- durante la traduzione: fasi, avanzamento, tempo e il **flusso dal vivo** degli ultimi paragrafi
  (originale e traduzione);
- alla fine: download e, per i PDF, il **confronto pagina per pagina** con una linea da trascinare
  tra originale e traduzione.

Impostazioni del modello:
1. Scegli il tipo di endpoint (Ollama / compatibile OpenAI), l'URL e, se serve, l'API key.
2. *Verifica connessione e carica modelli* controlla l'endpoint e riempie l'elenco dei modelli
   (`/api/tags` per Ollama, `/v1/models` per OpenAI). Se il modello non è nell'elenco scegli *Altro*.
   Con *Salva* memorizzi l'endpoint (URL, tipo, chiave, modello) per richiamarlo dal menu *Endpoint salvati*.
3. Scegli lingua di origine (o rilevamento automatico) e di destinazione (anche personalizzata).
4. Facoltativo: glossario / istruzioni (terminologia, registro formale, termini da non tradurre).
5. Trascina il file, premi **Traduci**, scarica il risultato.

Le impostazioni vengono ricordate nel browser; l'API key solo se salvi l'endpoint.

## Fedeltà della formattazione

### Word (.docx)
Viene cambiato **solo il testo**: il file originale resta com'è in tutto il resto.
- stili, font, dimensioni, colori, grassetto/corsivo/sottolineato **anche su singole parole**
- link (con il loro indirizzo), tab, immagini in linea e fluttuanti, forme e caselle di testo
- tabelle, elenchi, sezioni, intestazioni, piè di pagina, note, commenti, campi (numeri di pagina, date, riferimenti)
- testi dentro grafici e SmartArt

Come funziona: ogni frammento con formattazione diversa viene marcato con un tag (`<t1>…</t1>`) e ogni
elemento non testuale con un segnaposto (`<x1/>`). Il modello traduce lasciando i tag e il paragrafo
viene ricomposto con le proprietà originali di ciascun frammento. Se il modello altera i tag, il paragrafo
viene ritradotto da solo; se fallisce ancora, il testo viene messo con la formattazione prevalente del
paragrafo (nessun contenuto si perde).

### PDF
Si può scegliere tra tre uscite:
- **PDF con la stessa impaginazione** (predefinita): la traduzione viene ricomposta sopra la pagina originale;
- **Word modificabile**: il PDF viene prima convertito in .docx mantenendo impaginazione, rientri, font,
  tabelle e immagini (pdf2docx), poi tradotto con la stessa precisione dei file Word;
- **solo testo**.

Nel PDF con la stessa impaginazione:
- immagini, disegni, linee, sfondi e colori delle tabelle, link: invariati
- **font originali**: i font incorporati nei PDF sono quasi sempre "sottoinsiemi" senza tabella dei
  caratteri; l'app la ricostruisce da come il PDF usa i glifi, così il font originale viene riutilizzato.
  Ordine di scelta: font originale se ha tutti i caratteri, poi la stessa famiglia installata o un font
  metricamente compatibile (Calibri → Carlito, Arial/Helvetica → Liberation Sans, Times → Liberation Serif,
  Cambria → Caladea, Courier → Liberation Mono), poi font originale con i soli caratteri mancanti
  (es. k, w, y assenti da un testo italiano) presi da un font simile
- **rientri**: rientro della prima riga, rientri sporgenti (elenchi, note), blocchi rientrati, righe
  centrate o a destra; le righe vengono unite in paragrafi solo quando vanno davvero a capo, così il
  modello traduce frasi intere
- dimensione, colore, grassetto, corsivo, apici, sottolineature e barrature per ogni parte di testo
- allineamento (sinistra, destra, centrato, giustificato), interlinea, testo ruotato
- celle di tabella, colonne, elenchi puntati e testo accanto alle immagini restano separati
- i testi che non cambiano (numeri, codici) non vengono toccati

Limiti: se la traduzione è più lunga dell'originale il carattere si riduce quanto basta per farla stare
nello spazio disponibile; il testo che va a capo in modo diverso può far cambiare leggermente l'aspetto
dei paragrafi giustificati. I PDF scansionati (solo immagini) richiedono prima un OCR.
In alternativa si può scegliere l'uscita **.docx** (testo modificabile, senza impaginazione) o **.txt**.

### Testo (.txt / .md)
Paragrafi, righe vuote e struttura Markdown restano invariati.

I file `.doc` vanno salvati come `.docx`.

## Come funziona la traduzione
- Il documento viene diviso in paragrafi; i paragrafi vengono raggruppati in blocchi di circa
  `CHUNK_CHARS` caratteri e inviati con marcatori `[[1]]`, `[[2]]`… così ogni traduzione torna al suo posto.
- Se il modello non rispetta i marcatori, quel blocco viene ritradotto paragrafo per paragrafo (più lento ma sicuro).
- Numeri e simboli puri non vengono inviati al modello.
- La formattazione viaggia come tag inline: i modelli più grandi li rispettano meglio.
- I blocchi `<think>…</think>` dei modelli "reasoning" (Qwen3, DeepSeek-R1…) vengono rimossi.

## Se la traduzione non va avanti
La pagina mostra in tempo reale caratteri tradotti, blocco corrente e cosa sta facendo il modello.
I log dettagliati (un riga per blocco con i tempi) si vedono con:
```bash
docker compose logs -f traduttore
```
| Cosa vedi | Causa probabile | Rimedio |
|---|---|---|
| "in attesa della risposta del modello" per minuti al primo blocco | il modello si sta caricando in memoria o gira su CPU | attendi; poi riduci *Caratteri per richiesta* (1000–1500) o usa un modello più piccolo |
| "il modello sta ragionando" | modello con thinking (qwen3, deepseek-r1, gpt-oss) | attiva *Disattiva il ragionamento* (default) |
| "blocchi ritradotti paragrafo per paragrafo" | il modello non rispetta i marcatori | modello più grande o blocchi più piccoli |
| errore "non ha risposto per 900 secondi" | modello bloccato o troppo lento | controlla `ollama ps`; aumenta `LLM_READ_TIMEOUT` |

Per vedere se Ollama sta lavorando: `ollama ps` (modello caricato, CPU/GPU) e i log di Ollama.

## Testo rimasto non tradotto
I modelli locali, davanti a paragrafi con molta formattazione (tipico dei PDF convertiti in Word), a volte
restituiscono il testo invariato o non traducono le parti formattate. L'app lo riconosce (frasi o parole
identiche all'originale) e ritraduce quel paragrafo: prima da solo con un avviso esplicito al modello, poi,
se serve, senza i marcatori di formato (si perde il grassetto di quel paragrafo, non la traduzione).
Le lunghe serie di spazi usate per allineare le colonne non vengono mai mostrate al modello e sono
rimesse identiche. Se alla fine resta qualcosa, l'interfaccia lo segnala.

## Consigli sui modelli
Per traduzioni di qualità servono modelli multilingue di almeno 7–14B
(es. `qwen2.5:14b`, `qwen3:14b`, `gemma3:12b`, `mistral-small`). Con modelli piccoli riduci *Caratteri per richiesta* (es. 1500).

## API
| Metodo | Percorso | Descrizione |
|---|---|---|
| GET | `/api/config` | valori predefiniti |
| POST | `/api/models` | elenco modelli (`{provider, base_url, api_key}`) |
| POST | `/api/translate` | multipart: `file, target_lang, source_lang, provider, base_url, model, api_key, output_format, glossary, temperature, max_chars, concurrency` → `{job_id}` |
| GET | `/api/jobs/{id}` | stato e avanzamento |
| GET | `/api/jobs/{id}/download` | file tradotto |
| DELETE | `/api/jobs/{id}` | annulla |
| GET | `/api/jobs/{id}/preview/{orig\|tr}/{pagina}` | anteprima PNG di una pagina (solo PDF) |

Esempio da riga di comando:
```bash
curl -F file=@contratto.docx -F target_lang=English -F provider=ollama \
     -F base_url=http://localhost:11434 -F model=qwen2.5:14b http://localhost:8080/api/translate
```

## Note
- I job sono in memoria e scadono dopo 1 ora; riavviando il servizio si perdono.
- L'URL dell'endpoint viene chiamato **dal server**, non dal browser: deve essere raggiungibile dalla macchina che esegue l'app.
- Non c'è autenticazione: se lo esponi in rete mettilo dietro un reverse proxy con login.
