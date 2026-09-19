from __future__ import annotations

import logging
from typing import Any
from pydantic import BaseModel, Field

import chromadb

from ..config import settings
from .ingest import DocumentIngester, LocalEmbeddingFunction

logger = logging.getLogger(__name__)

FALLBACK_GUIDANCE_DOCS = [
    {"id": "TEXT-01", "title": "Text task guidance", "text": "For text tasks, be accurate, concise, structured, and explicit about assumptions.", "source": "Sentinel task guidance"},
    {"id": "PLAN-01", "title": "Planning guidance", "text": "For planning tasks, define a clear objective, sequence practical actions, and include a measurable outcome.", "source": "Sentinel task guidance"},
    {"id": "SUMMARY-01", "title": "Summarization guidance", "text": "For summaries, preserve the source meaning, prioritize key points, and avoid inventing details.", "source": "Sentinel task guidance"},
]


class RetrievedChunk(BaseModel):
    chunk_id: str
    document_id: str
    document_name: str
    source_type: str
    text: str


class RetrievalResult(BaseModel):
    query: str
    sources: list[str] = Field(default_factory=list)
    chunks: list[RetrievedChunk] = Field(default_factory=list)
    formatted_context: str = ""
    has_results: bool = False


class KnowledgeRetriever:
    def __init__(self, chroma_path: str | None = None) -> None:
        self.chroma_path = chroma_path or settings.chroma_path
        self.client = chromadb.PersistentClient(path=self.chroma_path)
        self.embedding_fn = LocalEmbeddingFunction()
        self.kb_collection = self.client.get_or_create_collection(
            name="knowledge_base",
            metadata={"description": "Task-specific user knowledge documents"},
            embedding_function=self.embedding_fn,
        )
        self.guidance_collection = self._build_guidance_collection()

    def _build_guidance_collection(self):
        collection = self.client.get_or_create_collection(
            name="task_guidance",
            metadata={"description": "Generic guidance for text task execution"},
            embedding_function=self.embedding_fn,
        )
        collection.upsert(
            ids=[doc["id"] for doc in FALLBACK_GUIDANCE_DOCS],
            documents=[doc["text"] for doc in FALLBACK_GUIDANCE_DOCS],
            metadatas=[{key: value for key, value in doc.items() if key != "text"} for doc in FALLBACK_GUIDANCE_DOCS],
        )
        return collection

    def retrieve(
        self,
        query: str,
        n_results: int = 3,
        document_ids: list[str] | None = None,
    ) -> RetrievalResult:
        """Retrieve task-relevant chunks from Chroma."""
        if not query.strip():
            return RetrievalResult(query=query)

        where_filter = None
        if document_ids and len(document_ids) == 1:
            where_filter = {"document_id": document_ids[0]}
        elif document_ids and len(document_ids) > 1:
            where_filter = {"document_id": {"$in": document_ids}}

        try:
            # Check if kb_collection has any documents
            count = self.kb_collection.count()
            if count > 0:
                results = self.kb_collection.query(
                    query_texts=[query],
                    n_results=min(n_results, count),
                    where=where_filter,
                )
                
                docs = results.get("documents", [[]])[0]
                metas = results.get("metadatas", [[]])[0]
                ids = results.get("ids", [[]])[0]

                if docs:
                    chunks: list[RetrievedChunk] = []
                    sources_set = set()
                    context_lines = ["--- RELEVANT KNOWLEDGE CONTEXT ---"]

                    for doc_text, meta, chunk_id in zip(docs, metas, ids):
                        doc_name = meta.get("document_name", "unknown")
                        doc_id = meta.get("document_id", "unknown")
                        source_type = meta.get("source_type", "txt")
                        sources_set.add(doc_name)
                        chunks.append(
                            RetrievedChunk(
                                chunk_id=chunk_id,
                                document_id=doc_id,
                                document_name=doc_name,
                                source_type=source_type,
                                text=doc_text,
                            )
                        )
                        context_lines.append(f"[{doc_name} (chunk {chunk_id})]:\n{doc_text}\n")

                    context_lines.append("--- END KNOWLEDGE CONTEXT ---")
                    return RetrievalResult(
                        query=query,
                        sources=sorted(list(sources_set)),
                        chunks=chunks,
                        formatted_context="\n".join(context_lines),
                        has_results=True,
                    )

            # Fallback to guidance collection if no user documents matched or exist
            guidance_res = self.guidance_collection.query(query_texts=[query], n_results=1)
            g_docs = guidance_res.get("documents", [[]])[0]
            if g_docs:
                return RetrievalResult(
                    query=query,
                    sources=["Generic Task Guidance"],
                    chunks=[],
                    formatted_context=f"--- GUIDANCE NOTE ---\n{g_docs[0]}\n--- END NOTE ---",
                    has_results=False,
                )

        except Exception as error:
            logger.error(f"Retrieval error: {error}")

        return RetrievalResult(query=query)
