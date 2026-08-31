# vaakbot

A retrieval-augmented chatbot answering questions from a clinical/autism/speech-therapy
reference library, built on Azure. Designed to run at 100-200 users for roughly
₹3-5k/month, tapering down as the FAQ coverage improves.

## How it works

1. **Ingestion** (`backend/ingest.py`) — chunks the source documents (FAQ doc,
   conditions doc, therapy manuals, PDFs), embeds each chunk with
   `text-embedding-3-small`, and uploads them to an Azure AI Search index
   (`vaakbot-docs`).
2. **Retrieval + response** (`backend/main.py`) — a FastAPI backend that:
   - embeds the incoming question,
   - retrieves the top-5 matching chunks from Azure AI Search (vector search),
   - if the top match's confidence score is below `0.75`, declines to answer
     rather than guessing, and points the person to a human instead,
   - otherwise answers using `gpt-5.4-nano`, grounded only in the retrieved
     chunks, in a calm, non-alarmist tone,
   - keeps a short conversation memory per session (last 5 user turns), after
     which it resets and tells the person plainly, so cost and context stay
     bounded.
3. **Frontend** (`frontend/`) — a Vite + React chat UI that talks to the
   backend's `/api/chat` endpoint.

## Architecture

```
 ┌─────────────┐      /api/chat       ┌──────────────┐
 │   React UI   │ ───────────────────▶ │   FastAPI     │
 │  (frontend)  │ ◀─────────────────── │  (backend)    │
 └─────────────┘                       └──────┬───────┘
                                               │
                        ┌──────────────────────┼──────────────────────┐
                        ▼                                              ▼
              Azure AI Search (vaakbot-docs)                Azure OpenAI (vaakbot-openai)
              free tier, vector search                       gpt-5.4-nano (chat)
                                                              text-embedding-3-small (embed)
```

## Azure resources

| Resource | Name | Tier |
|---|---|---|
| Resource group | `vaakbot-rg` | — |
| Azure OpenAI | `vaakbot-openai` | S0, Global Standard PAYG |
| Azure AI Search | `vaakbot-search` | Free |

## Running locally

**Backend:**
```bash
cd backend
pip install fastapi uvicorn openai azure-search-documents --break-system-packages -q

export AZURE_OPENAI_ENDPOINT="https://vaakbot-openai.openai.azure.com/"
export AZURE_OPENAI_KEY="<your key>"
export AZURE_OPENAI_CHAT_DEPLOYMENT="gpt-5.4-nano"
export AZURE_OPENAI_EMBED_DEPLOYMENT="text-embedding-3-small"
export AZURE_SEARCH_ENDPOINT="https://vaakbot-search.search.windows.net"
export AZURE_SEARCH_KEY="<your key>"
export AZURE_SEARCH_INDEX="vaakbot-docs"

uvicorn main:app --reload --port 8000
```

**Frontend** (in a separate terminal):
```bash
cd frontend
npm install
npm run dev
```
Opens at `http://localhost:5173`, proxying `/api/*` to the backend on `:8000`.

## Ingesting new documents

Drop new files into a folder and run:
```bash
python3 backend/ingest.py ./docs
```
Same env vars as above, plus the script chunks per-doc-type (FAQ splits per
Q&A pair, manuals split by heading section, PDFs split by page groups) and
uploads to the `vaakbot-docs` index.

## Notes on cost and tuning

- Everything runs on Azure's free tiers except the OpenAI token cost itself,
  which stays well under budget at this scale (~₹50-200/month in raw model
  usage for 100-200 users at normal support-bot volume).
- Log low-confidence / declined answers — that log is the backlog for what to
  add to the FAQ doc next, which is what drives the cost taper over time.

## Security

No API keys are committed to this repo. Set them as environment variables
locally, or as secrets in whatever hosting environment you deploy to.
