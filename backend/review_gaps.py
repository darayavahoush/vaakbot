import os
from azure.data.tables import TableServiceClient
from azure.core.credentials import AzureNamedKeyCredential

cred = AzureNamedKeyCredential(os.environ["AZURE_STORAGE_ACCOUNT"], os.environ["AZURE_STORAGE_KEY"])
svc = TableServiceClient(
    endpoint=f"https://{os.environ['AZURE_STORAGE_ACCOUNT']}.table.core.windows.net",
    credential=cred,
)
table = svc.get_table_client(os.environ.get("AZURE_LOG_TABLE", "querylog"))

gaps = [e for e in table.query_entities("PartitionKey eq 'tier2'") if e.get("feedback") != "up"]
gaps += [e for e in table.query_entities("feedback eq 'down'")]

seen = set()
for e in sorted(gaps, key=lambda x: x["timestamp"], reverse=True):
    if e["RowKey"] in seen:
        continue
    seen.add(e["RowKey"])
    print(f"[{e['timestamp']}] Q: {e['query']}")
    print(f"  A given: {e['reply'][:150]}...")
    print(f"  tier: {e['PartitionKey']}  feedback: {e.get('feedback') or 'none'}\n")
