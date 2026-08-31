"""
vaakbot backend — FastAPI app wrapping the RAG query logic with
per-session conversation memory (capped at MAX_TURNS user turns,
after which it resets and asks the user to start fresh).
"""

import os
import uuid
from typing import Dict, List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from openai import AzureOpenAI
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential
from azure.search.documents.models import VectorizedQuery

AOAI_ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"]
AOAI_KEY = os.environ["AZURE_OPENAI_KEY"]
CHAT_DEPLOYMENT = os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-5.4-nano")
EMBED_DEPLOYMENT = os.environ.get("AZURE_OPENAI_EMBED_DEPLOYMENT", "text-embedding-3-small")

SEARCH_ENDPOINT = os.environ["AZURE_SEARCH_ENDPOINT"]
SEARCH_KEY = os.environ["AZURE_SEARCH_KEY"]
SEARCH_INDEX = os.environ.get("AZURE_SEARCH_INDEX", "vaakbot-docs")

TOP_K = 5
MIN_SCORE = 0.75
MAX_TURNS = 5

SYSTEM_PROMPT = """You are a calm, warm, clear assistant answering questions using ONLY the
reference material provided to you below. Speak plainly, avoid alarming or
clinical-sounding language, and keep answers short and reassuring in tone.

Rules:
- Only answer using the CONTEXT provided. Do not use outside knowledge.
- If the CONTEXT does not contain enough information to answer confidently,
  say so plainly and suggest the person reach out to a human for that
  specific question. Do not guess or improvise.
- Never sound alarmed, clinical, or bureaucratic. Be direct and kind.
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

sessions: Dict[str, List[dict]] = {}


def embed(text: str):
    resp = client.embeddings.create(model=EMBED_DEPLOYMENT, input=text)
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


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    restarted: bool


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    history = sessions.setdefault(session_id, [])

    user_turns = sum(1 for m in history if m["role"] == "user")
    if user_turns >= MAX_TURNS:
        sessions[session_id] = []
        return ChatResponse(
            session_id=session_id,
            reply=(
                "We've covered a good amount here — let's start fresh so I can stay "
                "sharp and accurate. Go ahead and ask your next question!"
            ),
            restarted=True,
        )

    hits = retrieve(req.message)
    if not hits or hits[0]["score"] < MIN_SCORE:
        reply = (
            "I don't have solid enough information on that from what I've been given. "
            "It's best to check with a human on this one."
        )
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
            max_tokens=400,
        )
        reply = resp.choices[0].message.content

    history.append({"role": "user", "content": req.message})
    history.append({"role": "assistant", "content": reply})
    sessions[session_id] = history

    return ChatResponse(session_id=session_id, reply=reply, restarted=False)


app.mount("/", StaticFiles(directory="static", html=True), name="static")
