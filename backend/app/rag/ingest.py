from __future__ import annotations

import io
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from pydantic import BaseModel, Field

import chromadb

from ..config import settings

logger = logging.getLogger(__name__)


class DocumentChunk(BaseModel):
    chunk_id: str
    document_id: str
    document_name: str
    source_type: str
    text: str
    chunk_index: int


class KnowledgeDocument(BaseModel):
    document_id: str
    document_name: str
    source_type: str
    char_count: int
    chunk_count: int
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class LocalEmbeddingFunction:
    def __init__(self, dimensions: int = 64) -> None:
        self.dimensions = dimensions

    def name(self) -> str:
        return "local_embedding"

    def embed_documents(self, input: list[str]) -> list[list[float]]:
        return self(input)

    def embed_query(self, input: Any) -> list[list[float]] | list[float]:
        if isinstance(input, list):
            return self(input)
        res = self([input])
        return res[0] if res else [0.0] * self.dimensions

    def __call__(self, input: Any) -> list[list[float]]:
        if isinstance(input, str):
            items = [input]
        elif isinstance(input, list):
            items = input
        else:
            items = [str(input)]

        vectors = []
        for text in items:
            text_str = " ".join(str(t) for t in text) if isinstance(text, list) else str(text)
            vector = [0.0] * self.dimensions
            for token in re.findall(r"[a-z0-9]+", text_str.lower()):
                vector[hash(token) % self.dimensions] += 1.0
            norm = sum(value * value for value in vector) ** 0.5 or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


class DocumentIngester:
    def __init__(self, chroma_path: str | None = None) -> None:
        self.chroma_path = chroma_path or settings.chroma_path
        self.client = chromadb.PersistentClient(path=self.chroma_path)
        self.embedding_fn = LocalEmbeddingFunction()
        self.collection = self.client.get_or_create_collection(
            name="knowledge_base",
            metadata={"description": "Task-specific user knowledge documents"},
            embedding_function=self.embedding_fn,
        )
        self.registry_file = Path(self.chroma_path) / "doc_registry.json"
        self._ensure_registry()

    def _ensure_registry(self) -> None:
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.registry_file.exists():
            self.registry_file.write_text("{}", encoding="utf-8")

    def _load_registry(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self.registry_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_registry(self, registry: dict[str, dict[str, Any]]) -> None:
        self.registry_file.write_text(json.dumps(registry, indent=2), encoding="utf-8")

    def extract_text(self, content_bytes: bytes, filename: str) -> tuple[str, str]:
        ext = Path(filename).suffix.lower()
        if ext in {".txt", ".text"}:
            return content_bytes.decode("utf-8", errors="replace"), "txt"
        elif ext in {".md", ".markdown"}:
            return content_bytes.decode("utf-8", errors="replace"), "markdown"
        elif ext == ".pdf":
            try:
                import pypdf
                reader = pypdf.PdfReader(io.BytesIO(content_bytes))
                pages = [page.extract_text() or "" for page in reader.pages]
                return "\n\n".join(pages).strip(), "pdf"
            except Exception as error:
                logger.error(f"PDF extraction failed with pypdf: {error}")
                # Fallback: extract plain ascii strings
                text = re.sub(r"[^\x20-\x7E\n]", " ", content_bytes.decode("latin1", errors="ignore"))
                return text.strip(), "pdf"
        else:
            # General fallback
            return content_bytes.decode("utf-8", errors="replace"), ext.lstrip(".") or "unknown"

    def chunk_text(self, text: str, chunk_size: int = 400, overlap: int = 80) -> list[str]:
        cleaned = re.sub(r"\r\n", "\n", text).strip()
        if not cleaned:
            return []
        
        # Split by double newlines (paragraphs) first
        paragraphs = [p.strip() for p in cleaned.split("\n\n") if p.strip()]
        chunks: list[str] = []
        current = ""

        for p in paragraphs:
            if len(current) + len(p) + 2 <= chunk_size:
                current = f"{current}\n\n{p}" if current else p
            else:
                if current:
                    chunks.append(current)
                if len(p) > chunk_size:
                    # Sub-split long paragraph
                    start = 0
                    while start < len(p):
                        end = start + chunk_size
                        chunks.append(p[start:end])
                        start += chunk_size - overlap
                    current = ""
                else:
                    current = p

        if current:
            chunks.append(current)

        return chunks or [cleaned]

    def ingest_document(self, filename: str, content_bytes: bytes) -> KnowledgeDocument:
        raw_text, source_type = self.extract_text(content_bytes, filename)
        if not raw_text.strip():
            raise ValueError(f"Could not extract any readable text from '{filename}'.")

        doc_id = f"doc-{uuid4().hex[:8]}"
        chunks = self.chunk_text(raw_text)

        ids = [f"{doc_id}-c{idx}" for idx in range(len(chunks))]
        documents = chunks
        metadatas = [
            {
                "document_id": doc_id,
                "document_name": filename,
                "chunk_id": ids[idx],
                "source_type": source_type,
                "chunk_index": idx,
            }
            for idx in range(len(chunks))
        ]

        self.collection.upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
        )

        doc_info = KnowledgeDocument(
            document_id=doc_id,
            document_name=filename,
            source_type=source_type,
            char_count=len(raw_text),
            chunk_count=len(chunks),
        )

        registry = self._load_registry()
        registry[doc_id] = doc_info.model_dump()
        self._save_registry(registry)

        return doc_info

    def list_documents(self) -> list[KnowledgeDocument]:
        registry = self._load_registry()
        return [KnowledgeDocument(**item) for item in registry.values()]

    def delete_document(self, document_id: str) -> bool:
        registry = self._load_registry()
        if document_id not in registry:
            return False

        # Delete from Chroma
        try:
            self.collection.delete(where={"document_id": document_id})
        except Exception as error:
            logger.warning(f"Chroma delete error for {document_id}: {error}")

        del registry[document_id]
        self._save_registry(registry)
        return True
