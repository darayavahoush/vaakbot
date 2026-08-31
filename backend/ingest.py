"""
Vaakbot ingestion pipeline.

Reads every .docx / .pdf in DOCS_DIR, chunks them sensibly, embeds each
chunk with Azure OpenAI, uploads to Azure AI Search, AND writes a
readable, anchored HTML rendering of each source doc to ./doc_html/
so the frontend can link straight to the exact passage that grounded
an answer (chip click -> /sources/{slug}.html#chunk-{id}).

Run this in Azure Cloud Shell (has network access to Azure + internet).
Upload this script AND your docs folder there first (Cloud Shell ->
Manage files -> Upload), or clone/copy them in.

Usage:
    pip install python-docx pypdf azure-search-documents openai --break-system-packages --user
    export AZURE_OPENAI_ENDPOINT="https://vaakbot-openai.openai.azure.com/"
    export AZURE_OPENAI_KEY="..."
    export AZURE_OPENAI_EMBED_DEPLOYMENT="text-embedding-3-small"
    export AZURE_SEARCH_ENDPOINT="https://vaakbot-search.search.windows.net"
    export AZURE_SEARCH_KEY="..."
    python3 ingest.py ./docs
"""

import os
import re
import sys
import uuid
import glob
import html

from docx import Document as DocxDocument
from pypdf import PdfReader

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex, SimpleField, SearchableField, SearchFieldDataType,
    VectorSearch, HnswAlgorithmConfiguration, VectorSearchProfile,
    SearchField,
)
from openai import AzureOpenAI

# ---- config from env ----
AOAI_ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"]
AOAI_KEY = os.environ["AZURE_OPENAI_KEY"]
EMBED_DEPLOYMENT = os.environ["AZURE_OPENAI_EMBED_DEPLOYMENT"]
SEARCH_ENDPOINT = os.environ["AZURE_SEARCH_ENDPOINT"]
SEARCH_KEY = os.environ["AZURE_SEARCH_KEY"]
INDEX_NAME = os.environ.get("AZURE_SEARCH_INDEX", "vaakbot-docs")
EMBED_DIM = 1536  # text-embedding-3-small default
HTML_OUTPUT_DIR = os.environ.get("VAAKBOT_HTML_DIR", "./doc_html")

QA_PATTERN = re.compile(r"^Q\d+:\s*(.+?)\s*A:\s*(.+)$", re.IGNORECASE)


# ---------- chunking ----------

def chunk_docx(path):
    """Yields (content, section) tuples."""
    doc = DocxDocument(path)
    heading_stack = []  # tracks current heading path
    buffer = []

    def flush():
        if buffer:
            text = " ".join(buffer).strip()
            if text:
                yield text, " > ".join(heading_stack) if heading_stack else ""
            buffer.clear()

    for p in doc.paragraphs:
        text = p.text.strip()
        if not text:
            continue

        style = p.style.name if p.style else "Normal"

        # Q&A pattern -> its own chunk immediately, regardless of buffer
        qa_match = QA_PATTERN.match(text)
        if qa_match:
            yield from flush()
            section = " > ".join(heading_stack) if heading_stack else ""
            yield text, section
            continue

        if style.startswith("Heading"):
            yield from flush()
            level = int(style[-1]) if style[-1].isdigit() else 1
            heading_stack = heading_stack[: level - 1] + [text]
            continue

        buffer.append(text)
        # flush every ~120 words to keep chunks small
        if sum(len(t.split()) for t in buffer) > 120:
            yield from flush()

    yield from flush()


def chunk_pdf(path, words_per_chunk=350):
    reader = PdfReader(path)
    buffer_words = []
    start_page = 1

    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        buffer_words.extend(text.split())
        if len(buffer_words) >= words_per_chunk:
            content = " ".join(buffer_words)
            yield content, f"pages {start_page}-{i}"
            buffer_words = []
            start_page = i + 1

    if buffer_words:
        content = " ".join(buffer_words)
        yield content, f"pages {start_page}-{len(reader.pages)}"


def load_chunks(docs_dir):
    chunks = []
    for path in glob.glob(os.path.join(docs_dir, "**", "*"), recursive=True):
        fname = os.path.basename(path)
        if path.lower().endswith(".docx"):
            for content, section in chunk_docx(path):
                chunks.append({"content": content, "source": fname, "section": section})
        elif path.lower().endswith(".pdf"):
            for content, section in chunk_pdf(path):
                chunks.append({"content": content, "source": fname, "section": section})

    # Stabilize each chunk's id ONCE here, up front, so the same id is used
    # consistently in the search index, the HTML anchors, and anywhere else
    # downstream. (Previously this was computed later, only at upload time.)
    for c in chunks:
        c["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, c["source"] + c["section"] + c["content"][:50]))

    return chunks


# ---------- embedding ----------

def embed_all(client, chunks, batch_size=16):
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        resp = client.embeddings.create(
            model=EMBED_DEPLOYMENT,
            input=[c["content"] for c in batch],
        )
        for c, e in zip(batch, resp.data):
            c["embedding"] = e.embedding
        print(f"  embedded {min(i + batch_size, len(chunks))}/{len(chunks)}")


# ---------- search index ----------

def ensure_index(index_client):
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SimpleField(name="source", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="section", type=SearchFieldDataType.String, filterable=True),
        SearchField(
            name="embedding",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=EMBED_DIM,
            vector_search_profile_name="default-profile",
        ),
    ]
    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name="default-hnsw")],
        profiles=[VectorSearchProfile(name="default-profile", algorithm_configuration_name="default-hnsw")],
    )
    index = SearchIndex(name=INDEX_NAME, fields=fields, vector_search=vector_search)
    index_client.create_or_update_index(index)


def upload_chunks(search_client, chunks):
    docs = []
    for c in chunks:
        docs.append({
            "id": c["id"],
            "content": c["content"],
            "source": c["source"],
            "section": c["section"],
            "embedding": c["embedding"],
        })
    # upload in batches of 100
    for i in range(0, len(docs), 100):
        batch = docs[i:i + 100]
        result = search_client.upload_documents(batch)
        failed = [r for r in result if not r.succeeded]
        if failed:
            print(f"  {len(failed)} failed in batch {i // 100}")
        print(f"  uploaded {min(i + 100, len(docs))}/{len(docs)}")


# ---------- anchored HTML source pages ----------

def slugify(name: str) -> str:
    """Deterministic filename -> URL-safe slug. Keep this identical to the
    copy of this function in query.py so both sides agree on the URL."""
    base = os.path.splitext(name)[0]
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    return slug or "doc"


def generate_html_docs(chunks, output_dir):
    """Writes one HTML file per source document, in ./output_dir, with each
    chunk wrapped in <div id="chunk-{id}">. Chunks are rendered in the same
    order they were extracted, grouped under their section heading, so the
    page reads naturally even though it isn't a pixel-perfect copy of the
    original PDF/DOCX layout."""
    os.makedirs(output_dir, exist_ok=True)

    by_source = {}
    for c in chunks:
        by_source.setdefault(c["source"], []).append(c)

    for source, source_chunks in by_source.items():
        slug = slugify(source)
        parts = [
            "<!DOCTYPE html><html><head><meta charset='utf-8'>",
            "<meta name='viewport' content='width=device-width, initial-scale=1'>",
            f"<title>{html.escape(source)}</title>",
            "<style>",
            "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
            "max-width:720px;margin:0 auto;padding:32px 20px 80px;line-height:1.65;"
            "color:#3A3428;background:#FBF9F4;}",
            "h1{font-size:22px;margin-bottom:24px;color:#3A3428;}",
            ".section-label{font-size:12px;text-transform:uppercase;letter-spacing:.04em;"
            "color:#8C8672;margin:28px 0 6px;font-weight:600;}",
            ".chunk{padding:6px 10px;margin:0 -10px 14px;border-radius:8px;}",
            ".chunk:target{background-color:#DCE6D6;animation:vb-fade 2.5s ease forwards;}",
            "@keyframes vb-fade{0%{background-color:#DCE6D6;}70%{background-color:#DCE6D6;}"
            "100%{background-color:#F0EDE1;}}",
            "</style></head><body>",
            f"<h1>{html.escape(source)}</h1>",
        ]
        last_section = None
        for c in source_chunks:
            if c.get("section") and c["section"] != last_section:
                parts.append(f"<div class='section-label'>{html.escape(c['section'])}</div>")
                last_section = c["section"]
            escaped = html.escape(c["content"]).replace("\n", "<br>")
            parts.append(f"<div class='chunk' id='chunk-{c['id']}'>{escaped}</div>")
        parts.append("</body></html>")

        out_path = os.path.join(output_dir, f"{slug}.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(parts))
        print(f"  wrote {out_path} ({len(source_chunks)} chunks)")


def main():
    docs_dir = sys.argv[1] if len(sys.argv) > 1 else "./docs"

    print(f"Chunking documents in {docs_dir} ...")
    chunks = load_chunks(docs_dir)
    print(f"Total chunks: {len(chunks)}")

    print("Embedding chunks ...")
    aoai = AzureOpenAI(azure_endpoint=AOAI_ENDPOINT, api_key=AOAI_KEY, api_version="2024-10-21")
    embed_all(aoai, chunks)

    print("Creating/updating search index ...")
    index_client = SearchIndexClient(SEARCH_ENDPOINT, AzureKeyCredential(SEARCH_KEY))
    ensure_index(index_client)

    print("Uploading chunks to search ...")
    search_client = SearchClient(SEARCH_ENDPOINT, INDEX_NAME, AzureKeyCredential(SEARCH_KEY))
    upload_chunks(search_client, chunks)

    print(f"Writing anchored HTML source pages to {HTML_OUTPUT_DIR} ...")
    generate_html_docs(chunks, HTML_OUTPUT_DIR)

    print("Done.")


if __name__ == "__main__":
    main()
