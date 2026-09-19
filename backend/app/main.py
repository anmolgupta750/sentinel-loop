import logging
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .engine import engine
from .models import ApprovalRequest, RetryRequest, TaskRequest
from .tools.registry import tool_registry

logger = logging.getLogger(__name__)

app = FastAPI(title="Sentinel Loop API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "http://127.0.0.1:5174", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "sentinel-loop-api",
        "version": "0.2.0",
        "status": "ok",
        "health": "/api/health",
        "docs": "/docs",
        "frontend": "http://localhost:5173",
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "sentinel-loop", "version": "0.2.0"}


@app.post("/api/tasks")
def create_task(request: TaskRequest):
    try:
        return engine.create(request, background=True)
    except Exception as error:
        logger.exception("Failed to create task")
        raise HTTPException(status_code=500, detail=f"Failed to create task: {error}")


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    try:
        return engine.get(task_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Workflow not found") from error


@app.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: str, request: RetryRequest | None = None):
    try:
        note = request.note if request else ""
        return engine.retry(task_id, note)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Workflow not found") from error


@app.post("/api/tasks/{task_id}/approval")
def approve_task(task_id: str, request: ApprovalRequest):
    try:
        return engine.approve(task_id, request.approved, request.note)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Workflow not found") from error


@app.get("/api/audit/{task_id}")
def get_audit_trail(task_id: str):
    try:
        events = engine.audit.list_for_task(task_id)
        return {"task_id": task_id, "events": events}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not retrieve audit log: {error}")


# --- Knowledge Base / RAG Document Endpoints ---

@app.post("/api/documents")
async def upload_document(file: UploadFile = File(...)):
    # Validate file size (max 10MB)
    MAX_FILE_SIZE = 10 * 1024 * 1024
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File size exceeds 10MB limit.")
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        doc = engine.ingester.ingest_document(filename=file.filename, content_bytes=content)
        return doc
    except Exception as error:
        logger.exception(f"Document ingestion failed for {file.filename}")
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {error}")


@app.get("/api/documents")
def list_documents():
    return {"documents": engine.ingester.list_documents()}


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str):
    success = engine.ingester.delete_document(doc_id)
    if not success:
        raise HTTPException(status_code=404, detail="Document not found.")
    return {"deleted": True, "document_id": doc_id}


# --- Tool Registry Endpoints ---

@app.get("/api/tools")
def list_tools():
    return {"tools": tool_registry.list_tools()}