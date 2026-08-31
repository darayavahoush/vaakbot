import os, json, uuid
from datetime import datetime, timezone
from azure.data.tables import TableServiceClient
from azure.core.credentials import AzureNamedKeyCredential

STORAGE_ACCOUNT = os.environ["AZURE_STORAGE_ACCOUNT"]
STORAGE_KEY = os.environ["AZURE_STORAGE_KEY"]
SESSION_TABLE = os.environ.get("AZURE_SESSION_TABLE", "sessions")

_cred = AzureNamedKeyCredential(STORAGE_ACCOUNT, STORAGE_KEY)
_table_service = TableServiceClient(
    endpoint=f"https://{STORAGE_ACCOUNT}.table.core.windows.net",
    credential=_cred,
)
_table_service.create_table_if_not_exists(SESSION_TABLE)
_table_client = _table_service.get_table_client(SESSION_TABLE)

def new_session_id() -> str:
    return str(uuid.uuid4())

def get_session(session_id: str) -> list:
    try:
        entity = _table_client.get_entity(partition_key="session", row_key=session_id)
        return json.loads(entity["history"])
    except Exception:
        return []

def save_session(session_id: str, history: list):
    _table_client.upsert_entity({
        "PartitionKey": "session",
        "RowKey": session_id,
        "history": json.dumps(history),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
