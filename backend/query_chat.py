"""
vaakbot RAG query/chat script
------------------------------
Retrieves relevant chunks from Azure AI Search (vaakbot-docs index) and
answers the user's question grounded in those chunks, using gpt-5.4-nano.

Required environment variables:
  AZURE_OPENAI_ENDPOINT           e.g. https://vaakbot-openai.openai.azure.com/
  AZURE_OPENAI_KEY
  AZURE_OPENAI_CHAT_DEPLOYMENT    the deployment name for gpt-5.4-nano (check
                                  what you actually named it in Foundry)
  AZURE_OPENAI_EMBED_DEPLOYMENT   text-embedding-3-small
  AZURE_SEARCH_ENDPOINT           e.g. https://vaakbot-search.search.windows.net
  AZURE_SEARCH_KEY
  AZURE_SEARCH_INDEX              vaakbot-docs

Usage:
  python3 query_chat.py                 -> interactive chat loop
  python3 query_chat.py "your question" -> single question, single answer
"""

import os
import sys

from openai import AzureOpenAI
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential
from azure.search.documents.models import VectorizedQuery

# ---- config ---------------------------------------------------------------

AOAI_ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"]
AOAI_KEY = os.environ["AZURE_OPENAI_KEY"]
CHAT_DEPLOYMENT = os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-5.4-nano")
EMBED_DEPLOYMENT = os.environ.get("AZURE_OPENAI_EMBED_DEPLOYMENT", "text-embedding-3-small")

SEARCH_ENDPOINT = os.environ["AZURE_SEARCH_ENDPOINT"]
SEARCH_KEY = os.environ["AZURE_SEARCH_KEY"]
SEARCH_INDEX = os.environ.get("AZURE_SEARCH_INDEX", "vaakbot-docs")

TOP_K = 5
# Below this similarity score, we treat retrieval as "not confident" and
# decline rather than let the model improvise.
MIN_SCORE = 0.75

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


def answer(question: str):
    hits = retrieve(question)
    if not hits or hits[0]["score"] < MIN_SCORE:
        return (
            "I don't have solid enough information on that from what I've been given. "
            "It's best to check with a human on this one.",
            hits,
        )

    context = "\n\n---\n\n".join(
        f"[Source: {h['source']}]\n{h['content']}" for h in hits
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"CONTEXT:\n{context}\n\nQUESTION:\n{question}",
        },
    ]

    resp = client.chat.completions.create(
        model=CHAT_DEPLOYMENT,
        messages=messages,
        temperature=0.3,
        max_tokens=400,
    )
    return resp.choices[0].message.content, hits


def main():
    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
        reply, hits = answer(question)
        print("\n" + reply + "\n")
        print(f"[retrieved {len(hits)} chunks, top score={hits[0]['score']:.3f}]" if hits else "[no chunks retrieved]")
        return

    print("vaakbot query test (Ctrl+C to quit)\n")
    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            break
        if not question:
            continue
        reply, hits = answer(question)
        print(f"\nBot: {reply}\n")
        if hits:
            print(f"  (top match: {hits[0]['source']}, score {hits[0]['score']:.3f})\n")


if __name__ == "__main__":
    main()
