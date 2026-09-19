"""Hybrid + semantic retrieval over `idx-atlas-knowledge`.

Every search combines keyword, vector and semantic reranking. Scoping by `doc_types` matters in
practice, not just in theory. Unscoped, the canonical query "checkout latency after payments
deploy" ranks the runbook the agent must cite (RB-0042) third, behind two incident records, and
only 0.01 ahead of fourth. Scoped to runbooks, it ranks first by a clear margin. So the knowledge
agent retrieves runbooks and past incidents as separate scoped searches, which is also how the
architecture describes it. See docs/API-NOTES.md.
"""

from __future__ import annotations

from collections.abc import Sequence

from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import get_bearer_token_provider
from azure.search.documents.aio import SearchClient
from azure.search.documents.models import VectorizedQuery
from openai import AsyncOpenAI

from ..auth import SCOPE_FOUNDRY, get_async_credential
from ..config import Settings
from ..models import DocType, KnowledgeHit, KnowledgeResults
from ..search_index import SEMANTIC_CONFIG
from .base import DEFAULT_TIMEOUT_S, with_timeout

SNIPPET_CHARS = 600
VECTOR_NEIGHBOURS = 50


def _odata_string(value: str) -> str:
    """Quote a value for an OData filter. Single quotes are escaped by doubling."""
    return "'" + value.replace("'", "''") + "'"


def build_filter(
    services: Sequence[str] | None = None, doc_types: Sequence[DocType] | None = None
) -> str | None:
    """Compose the OData filter. Exposed for unit testing; values are always quoted."""
    clauses: list[str] = []
    if doc_types:
        clauses.append(
            "(" + " or ".join(f"doc_type eq {_odata_string(t)}" for t in doc_types) + ")"
        )
    if services:
        joined = ",".join(services)
        clauses.append(f"services/any(s: search.in(s, {_odata_string(joined)}, ','))")
    return " and ".join(clauses) or None


class KnowledgeSearch:
    """Retrieval tool backend. Use as an async context manager."""

    def __init__(self, settings: Settings, credential: AsyncTokenCredential | None = None) -> None:
        self._settings = settings
        self._owns_credential = credential is None
        self._credential = credential or get_async_credential()
        self._search = SearchClient(
            settings.search_endpoint, settings.knowledge_index, self._credential
        )
        self._openai = AsyncOpenAI(
            base_url=settings.foundry_openai_base_url,
            api_key=get_bearer_token_provider(self._credential, SCOPE_FOUNDRY),
            timeout=DEFAULT_TIMEOUT_S,
            max_retries=2,
        )

    async def _embed(self, text: str) -> list[float]:
        response = await with_timeout(
            self._openai.embeddings.create(model=self._settings.model_embed, input=[text]),
            "embedding model",
        )
        return list(response.data[0].embedding)

    async def search(
        self,
        query: str,
        *,
        services: Sequence[str] | None = None,
        doc_types: Sequence[DocType] | None = None,
        top: int = 5,
    ) -> KnowledgeResults:
        """Hybrid keyword + vector search, reranked semantically."""
        vector = await self._embed(query)

        async def run() -> list[KnowledgeHit]:
            results = await self._search.search(
                search_text=query,
                vector_queries=[
                    VectorizedQuery(
                        vector=vector,
                        k_nearest_neighbors=VECTOR_NEIGHBOURS,
                        fields="content_vector",
                    )
                ],
                query_type="semantic",
                semantic_configuration_name=SEMANTIC_CONFIG,
                filter=build_filter(services, doc_types),
                select=[
                    "doc_id",
                    "doc_type",
                    "title",
                    "chunk",
                    "services",
                    "last_reviewed",
                    "injection_canary",
                ],
                top=top,
            )
            hits: list[KnowledgeHit] = []
            async for r in results:
                hits.append(
                    KnowledgeHit(
                        doc_id=r["doc_id"],
                        doc_type=r["doc_type"],
                        title=r["title"],
                        snippet=str(r.get("chunk") or "")[:SNIPPET_CHARS],
                        text=str(r.get("chunk") or ""),
                        services=list(r.get("services") or []),
                        last_reviewed=r.get("last_reviewed"),
                        score=float(r.get("@search.score") or 0.0),
                        reranker_score=r.get("@search.reranker_score"),
                        injection_canary=bool(r.get("injection_canary")),
                    )
                )
            return hits

        hits = await with_timeout(run(), "knowledge search")
        return KnowledgeResults(query=query, hits=hits)

    async def close(self) -> None:
        await self._search.close()
        await self._openai.close()
        if self._owns_credential:
            await self._credential.close()

    async def __aenter__(self) -> KnowledgeSearch:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
