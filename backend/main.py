"""
vaakbot backend — FastAPI app wrapping the RAG query logic with
per-session conversation memory (capped at MAX_TURNS user turns,
after which it resets and asks the user to start fresh).
"""

import os
import uuid
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from openai import AzureOpenAI
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential
from azure.search.documents.models import VectorizedQuery
from azure.storage.blob import BlobServiceClient
from fastapi.responses import StreamingResponse, HTMLResponse
from urllib.parse import quote
from html import escape
import io
import re
import docx as docx_lib
import pdfplumber

AOAI_ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"]
AOAI_KEY = os.environ["AZURE_OPENAI_KEY"]
CHAT_DEPLOYMENT = os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-5.4-nano")
EMBED_DEPLOYMENT = os.environ.get("AZURE_OPENAI_EMBED_DEPLOYMENT", "text-embedding-3-small")
EMBED_DIM = 512  # must match ingest.py and the search index's vector field

SEARCH_ENDPOINT = os.environ["AZURE_SEARCH_ENDPOINT"]
SEARCH_KEY = os.environ["AZURE_SEARCH_KEY"]
SEARCH_INDEX = os.environ.get("AZURE_SEARCH_INDEX", "vaakbot-docs")

STORAGE_ACCOUNT = os.environ["AZURE_STORAGE_ACCOUNT"]
STORAGE_KEY = os.environ["AZURE_STORAGE_KEY"]
STORAGE_CONTAINER = os.environ.get("AZURE_STORAGE_CONTAINER", "docs")

blob_service = BlobServiceClient(
    account_url=f"https://{STORAGE_ACCOUNT}.blob.core.windows.net",
    credential=STORAGE_KEY,
)

TOP_K = 5
MIN_SCORE = 0.71
MAX_TURNS = 5

SYSTEM_PROMPT = """You are a warm, gentle companion for parents and caregivers asking
questions about stimming, sensory integration, and related topics. You answer
using ONLY the reference material provided to you below.

Tone:
- Speak like a caring, knowledgeable friend — genuine, sweet, and unhurried.
- Use soft, encouraging language. It's okay to affirm the person's care for
  their child (e.g. "That's a thoughtful question" or "It makes sense you'd
  wonder about that").
- Avoid clinical, robotic, or bureaucratic phrasing. Avoid sounding alarmed,
  even when the topic itself is sensitive.
- Keep answers short, clear, and reassuring — comfort first, information
  second, but never at the cost of accuracy.

Rules:
- Only answer using the CONTEXT provided below. Do not use outside knowledge,
  and do not guess or improvise on anything clinical.
- If the CONTEXT doesn't contain enough to answer confidently, say so gently
  and suggest reaching out to a specialist or therapist for that specific
  question — frame it as care, not as a limitation (e.g. "That's a great
  question for your child's therapist, since it depends a lot on your
  child specifically").
- Never sound alarmed, clinical, or bureaucratic. Be warm, direct, and kind.
"""

client = AzureOpenAI(
    azure_endpoint=AOAI_ENDPOINT,
    api_key=AOAI_KEY,
    api_version="2024-10-21",
)

search_client = SearchClient(
    endpoint=SEARCH_ENDPOINT,
    index_name=SEARCH_INDEX,
    credential=AzureKeyCredential(SEARCH_KEY),
)

import re
from faq_lookup import FaqLookup
from session_store import get_session, save_session
from query_log import log_query, record_feedback

faq = FaqLookup()
VECTOR_CONFIDENCE_THRESHOLD = float(os.environ.get("VECTOR_CONFIDENCE_THRESHOLD", "0.78"))


def embed(text: str):
    resp = client.embeddings.create(model=EMBED_DEPLOYMENT, input=text, dimensions=EMBED_DIM)
    return resp.data[0].embedding


def retrieve(question: str):
    vector = embed(question)
    vq = VectorizedQuery(vector=vector, k_nearest_neighbors=TOP_K, fields="embedding")
    results = search_client.search(
        search_text=None,
        vector_queries=[vq],
        select=["content", "source", "section"],
        top=TOP_K,
    )
    hits = []
    for r in results:
        hits.append(
            {
                "content": r.get("content", ""),
                "source": r.get("source", "unknown"),
                "score": r.get("@search.score", 0.0),
            }
        )
    return hits


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str


class SourceInfo(BaseModel):
    source: str
    section: str
    snippet: str
    doc_url: str


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    restarted: bool
    sources: List[dict] = []
    log_id: str | None = None
    tier: str | None = None


class FeedbackRequest(BaseModel):
    log_id: str
    tier: str
    vote: str


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    history = get_session(session_id)

    user_turns = sum(1 for m in history if m["role"] == "user")
    if user_turns >= MAX_TURNS:
        save_session(session_id, [])
        return ChatResponse(
            session_id=session_id,
            reply=(
                "We've covered a good amount here — let's start fresh so I can stay "
                "sharp and accurate. Go ahead and ask your next question!"
            ),
            restarted=True,
        )

    # Tier 0 — fuzzy FAQ match, no API call at all
    faq_hit = faq.match(req.message)
    if faq_hit:
        reply = faq_hit["answer"]
        log_id = log_query(session_id, req.message, "tier0", faq_hit["score"] / 100, reply)
        history.append({"role": "user", "content": req.message})
        history.append({"role": "assistant", "content": reply})
        save_session(session_id, history)
        sources = [{
            "source": faq_hit["source"],
            "section": "",
            "snippet": reply[:400],
            "doc_url": f"/api/docs/{quote(faq_hit['source'])}",
        }]
        return ChatResponse(session_id=session_id, reply=reply, restarted=False, sources=sources, log_id=log_id, tier="tier0")

    # For follow-ups, fold the last SUCCESSFULLY-ANSWERED user turn into the
    # retrieval query so pronouns/short questions ("what causes it", "what are
    # early signs") can still find the right chunk. Tier 0 stays untouched
    # (exact/fuzzy match on the raw message).
    # Walk backward past any declined turns -- a decline carries no usable
    # topic, so anchoring to it (or stopping at it) would either poison
    # retrieval or leave genuinely answerable follow-ups with no context.
    DECLINE_MARKER = "I don't have solid enough information"
    prior_user_msg = None
    for i in range(len(history) - 1, 0, -1):
        if history[i]["role"] == "assistant" and history[i - 1]["role"] == "user":
            if DECLINE_MARKER not in history[i]["content"]:
                prior_user_msg = history[i - 1]["content"]
                break
    retrieval_query = f"{prior_user_msg} {req.message}" if prior_user_msg else req.message
    hits = retrieve(retrieval_query)

    # Tier 1 — vector search alone is confident enough, no chat completion
    if hits and hits[0]["score"] >= VECTOR_CONFIDENCE_THRESHOLD:
        top = hits[0]
        m = re.match(r'^Q\d+[:.]\s*(.+?)\s*A\d*[:.]\s*(.+)$', top["content"].strip(), re.IGNORECASE | re.DOTALL)
        reply = m.group(2).strip() if (m and len(m.group(1)) < 300) else top["content"]
        log_id = log_query(session_id, req.message, "tier1", top["score"], reply)
        history.append({"role": "user", "content": req.message})
        history.append({"role": "assistant", "content": reply})
        save_session(session_id, history)
        sources = [{
            "source": top["source"],
            "section": top.get("section", ""),
            "snippet": top["content"][:400],
            "doc_url": f"/api/docs/{quote(top['source'])}",
        }]
        return ChatResponse(session_id=session_id, reply=reply, restarted=False, sources=sources, log_id=log_id, tier="tier1")

    # Tier 2 — existing LLM RAG path, unchanged
    if not hits or hits[0]["score"] < MIN_SCORE:
        reply = (
            "I don't have solid enough information on that from what I've been given. "
            "It's best to check with a human on this one."
        )
        tier = "none"
    else:
        context = "\n\n---\n\n".join(
            f"[Source: {h['source']}]\n{h['content']}" for h in hits
        )
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(history)
        messages.append(
            {"role": "user", "content": f"CONTEXT:\n{context}\n\nQUESTION:\n{req.message}"}
        )
        resp = client.chat.completions.create(
            model=CHAT_DEPLOYMENT,
            messages=messages,
            temperature=0.3,
            max_completion_tokens=400,
        )
        reply = resp.choices[0].message.content
        tier = "tier2"

    log_id = log_query(session_id, req.message, tier, hits[0]["score"] if hits else 0.0, reply)

    history.append({"role": "user", "content": req.message})
    history.append({"role": "assistant", "content": reply})
    save_session(session_id, history)

    sources = []
    if hits and hits[0]["score"] >= MIN_SCORE:
        seen = set()
        for h in hits[:3]:
            key = (h["source"], h.get("section", ""))
            if key in seen:
                continue
            seen.add(key)
            sources.append({
                "source": h["source"],
                "section": h.get("section", ""),
                "snippet": h["content"][:400],
                "doc_url": f"/api/docs/{quote(h['source'])}",
            })

    return ChatResponse(session_id=session_id, reply=reply, restarted=False, sources=sources, log_id=log_id, tier=tier)


@app.post("/api/feedback")
def feedback(req: FeedbackRequest):
    try:
        record_feedback(req.log_id, req.tier, req.vote)
    except Exception:
        raise HTTPException(status_code=404, detail="log entry not found")
    return {"ok": True}


def extract_paragraphs(filename: str, raw_bytes: bytes):
    if filename.lower().endswith(".docx"):
        doc = docx_lib.Document(io.BytesIO(raw_bytes))
        return [p.text for p in doc.paragraphs if p.text.strip()]
    elif filename.lower().endswith(".pdf"):
        paragraphs = []
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                for block in text.split("\n\n"):
                    block = block.strip()
                    if block:
                        paragraphs.append(block)
        return paragraphs
    return []


def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


@app.get("/api/docs/view/{filename}", response_class=HTMLResponse)
def view_doc(filename: str, q: str = ""):
    blob_client = blob_service.get_blob_client(container=STORAGE_CONTAINER, blob=filename)
    raw_bytes = blob_client.download_blob().readall()

    paragraphs = extract_paragraphs(filename, raw_bytes)
    target = normalize(q)[:120]

    html_parts = []
    found = False
    for p in paragraphs:
        p_norm = normalize(p)
        if target and not found and target[:40] in p_norm:
            html_parts.append(f'<p class="hl" id="hl">{escape(p)}</p>')
            found = True
        else:
            html_parts.append(f"<p>{escape(p)}</p>")

    body = "\n".join(html_parts)
    scroll_script = "<script>document.getElementById('hl')?.scrollIntoView({block:'center'});</script>" if found else ""

    page = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{escape(filename)}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 720px; margin: 40px auto;
         padding: 0 20px; line-height: 1.6; color: #2b2b2b; background: #fbfaf7; }}
  h1 {{ font-size: 18px; color: #3F6F63; }}
  p {{ margin: 0 0 14px; }}
  p.hl {{ background: #FFE9A8; padding: 6px 10px; border-radius: 6px; }}
</style>
</head>
<body>
<h1>{escape(filename)}</h1>
{body}
{scroll_script}
</body>
</html>"""
    return HTMLResponse(content=page)


@app.get("/api/docs/{filename}")
def get_doc(filename: str):
    blob_client = blob_service.get_blob_client(container=STORAGE_CONTAINER, blob=filename)
    stream = blob_client.download_blob()
    media_type = "application/pdf" if filename.lower().endswith(".pdf") else         "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return StreamingResponse(
        stream.chunks(),
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# Frontend is served separately via Vercel; no static mount needed here.
