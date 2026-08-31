"""
backend/analyze_queries.py

Reads the querylog Table Storage table and produces a content-planning report:
  1. Most-asked question clusters (fuzzy-deduped)
  2. Most common follow-up pairs (what people ask right after X)
  3. Gaps: tier2 (LLM fallback) queries and thumbs-down feedback, clustered

Run on demand:
    python3 backend/analyze_queries.py
    python3 backend/analyze_queries.py --top 30 --min-cluster 2

No new dependencies — reuses azure-data-tables and rapidfuzz, both already
in requirements from session_store.py / faq_lookup.py.
"""
import os
import argparse
from collections import defaultdict
from datetime import datetime
from azure.data.tables import TableServiceClient
from azure.core.credentials import AzureNamedKeyCredential
from rapidfuzz import fuzz

STORAGE_ACCOUNT = os.environ["AZURE_STORAGE_ACCOUNT"]
STORAGE_KEY = os.environ["AZURE_STORAGE_KEY"]
LOG_TABLE = os.environ.get("AZURE_LOG_TABLE", "querylog")

CLUSTER_THRESHOLD = 85  # rapidfuzz token_sort_ratio; same scale as faq_lookup.py's match()


def fetch_all_entities():
    cred = AzureNamedKeyCredential(STORAGE_ACCOUNT, STORAGE_KEY)
    service = TableServiceClient(
        endpoint=f"https://{STORAGE_ACCOUNT}.table.core.windows.net",
        credential=cred,
    )
    client = service.get_table_client(LOG_TABLE)
    return list(client.list_entities())


def parse_ts(entity):
    try:
        return datetime.fromisoformat(entity["timestamp"])
    except Exception:
        return datetime.min


def cluster_queries(queries):
    """
    Greedy fuzzy clustering, same scorer/threshold style as faq_lookup.py's
    Tier 0 match. Returns list of (representative_text, count, member_texts).
    Representative = the shortest member (usually the cleanest phrasing).
    """
    clusters = []  # list of dicts: {"members": [...]}
    for q in queries:
        q_clean = q.strip()
        if not q_clean:
            continue
        placed = False
        for c in clusters:
            if fuzz.token_sort_ratio(q_clean, c["members"][0]) >= CLUSTER_THRESHOLD:
                c["members"].append(q_clean)
                placed = True
                break
        if not placed:
            clusters.append({"members": [q_clean]})

    results = []
    for c in clusters:
        rep = min(c["members"], key=len)
        results.append((rep, len(c["members"]), c["members"]))
    results.sort(key=lambda x: -x[1])
    return results


def cluster_pairs(pairs):
    """
    Cluster (q1, q2) follow-up pairs by fuzzy-matching BOTH sides together.
    Returns list of (rep_q1, rep_q2, count).
    """
    clusters = []  # {"reps": (q1, q2), "count": n}
    for q1, q2 in pairs:
        placed = False
        for c in clusters:
            if (fuzz.token_sort_ratio(q1, c["reps"][0]) >= CLUSTER_THRESHOLD and
                    fuzz.token_sort_ratio(q2, c["reps"][1]) >= CLUSTER_THRESHOLD):
                c["count"] += 1
                placed = True
                break
        if not placed:
            clusters.append({"reps": (q1, q2), "count": 1})
    clusters.sort(key=lambda c: -c["count"])
    return [(c["reps"][0], c["reps"][1], c["count"]) for c in clusters]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20, help="how many rows to show per section")
    ap.add_argument("--min-cluster", type=int, default=2, help="hide clusters/pairs seen fewer than this many times")
    args = ap.parse_args()

    print("Fetching query log ...")
    entities = fetch_all_entities()
    print(f"Total logged queries: {len(entities)}\n")

    if not entities:
        print("No queries logged yet.")
        return

    # ---- 1. Most-asked question clusters ----
    all_queries = [e["query"] for e in entities if e.get("query")]
    clusters = cluster_queries(all_queries)

    print("=" * 60)
    print(f"TOP {args.top} MOST-ASKED QUESTION CLUSTERS")
    print("=" * 60)
    shown = 0
    for rep, count, members in clusters:
        if count < args.min_cluster:
            continue
        print(f"[{count}x] {rep}")
        shown += 1
        if shown >= args.top:
            break
    if shown == 0:
        print(f"(no cluster reached min-cluster={args.min_cluster})")
    print()

    # ---- 2. Follow-up pairs, grouped by session ----
    by_session = defaultdict(list)
    for e in entities:
        sid = e.get("session_id")
        if sid:
            by_session[sid].append(e)

    pairs = []
    for sid, rows in by_session.items():
        rows.sort(key=parse_ts)
        for i in range(len(rows) - 1):
            q1 = rows[i].get("query", "").strip()
            q2 = rows[i + 1].get("query", "").strip()
            if q1 and q2:
                pairs.append((q1, q2))

    pair_clusters = cluster_pairs(pairs)

    print("=" * 60)
    print(f"TOP {args.top} FOLLOW-UP PATTERNS (asked X, then asked Y)")
    print("=" * 60)
    shown = 0
    for q1, q2, count in pair_clusters:
        if count < args.min_cluster:
            continue
        print(f"[{count}x] \"{q1}\"  ->  \"{q2}\"")
        shown += 1
        if shown >= args.top:
            break
    if shown == 0:
        print(f"(no follow-up pair reached min-cluster={args.min_cluster})")
    print()

    # ---- 3. Gaps: tier2 (LLM fallback) and thumbs-down ----
    tier2 = [e for e in entities if e.get("PartitionKey") == "tier2"]
    thumbs_down = [e for e in entities if e.get("feedback") == "down"]

    print("=" * 60)
    print(f"CONTENT GAPS: Tier-2 (LLM fallback) queries — {len(tier2)} total")
    print("=" * 60)
    tier2_clusters = cluster_queries([e["query"] for e in tier2 if e.get("query")])
    shown = 0
    for rep, count, members in tier2_clusters:
        print(f"[{count}x] {rep}")
        shown += 1
        if shown >= args.top:
            break
    if not tier2_clusters:
        print("(none)")
    print()

    print("=" * 60)
    print(f"CONTENT GAPS: Thumbs-down queries — {len(thumbs_down)} total")
    print("=" * 60)
    for e in thumbs_down[:args.top]:
        print(f"[{e.get('PartitionKey')}] {e.get('query')}")
        print(f"    reply given: {e.get('reply', '')[:150]}")
    if not thumbs_down:
        print("(none)")
    print()

    print("Done. Use the most-asked clusters + follow-up patterns to plan new")
    print("site pages/sections; use the gap lists to write new FAQ entries and")
    print("re-run ingest.py to promote them to Tier 0.")


if __name__ == "__main__":
    main()
