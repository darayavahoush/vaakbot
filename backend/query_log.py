import os, uuid
from datetime import datetime, timezone
from azure.data.tables import TableServiceClient
from azure.core.credentials import AzureNamedKeyCredential

STORAGE_ACCOUNT = os.environ["AZURE_STORAGE_ACCOUNT"]
STORAGE_KEY = os.environ["AZURE_STORAGE_KEY"]
LOG_TABLE = os.environ.get("AZURE_LOG_TABLE", "querylog")

_cred = AzureNamedKeyCredential(STORAGE_ACCOUNT, STORAGE_KEY)
_table_service = TableServiceClient(
    endpoint=f"https://{STORAGE_ACCOUNT}.table.core.windows.net",
    credential=_cred,
)
_table_service.create_table_if_not_exists(LOG_TABLE)
_log_client = _table_service.get_table_client(LOG_TABLE)

def log_query(session_id: str, user_msg: str, tier: str, confidence: float, reply: str) -> str:
    log_id = str(uuid.uuid4())
    _log_client.upsert_entity({
        "PartitionKey": tier,
        "RowKey": log_id,
        "session_id": session_id,
        "query": user_msg,
        "confidence": confidence,
        "reply": reply,
        "feedback": "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return log_id

def record_feedback(log_id: str, tier: str, feedback: str):
    entity = _log_client.get_entity(partition_key=tier, row_key=log_id)
    entity["feedback"] = feedback
    _log_client.upsert_entity(entity)
