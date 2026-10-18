"""The `idx-atlas-knowledge` index definition.

One definition, used by the seeder to create or update the index and by tests to assert the schema.
Fields follow the Phase 02 spec, plus `content_hash` for cheap idempotent re-seeding.

Two settings here are expensive to get wrong, because changing either one means dropping and
rebuilding the index after seeding:

- `content_vector` is 3072 dimensions, matching text-embedding-3-large (CLAUDE.md section 7.11).
- `services` is a filterable string collection, so the knowledge agent can scope retrieval to the
  services an incident actually touches.
"""

from __future__ import annotations

from azure.search.documents.indexes.models import (
    AzureOpenAIVectorizer,
    AzureOpenAIVectorizerParameters,
    HnswAlgorithmConfiguration,
    HnswParameters,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
    VectorSearchVectorizer,
)

from .config import EMBEDDING_DIMENSIONS, KNOWLEDGE_INDEX, MODEL_EMBED

SEMANTIC_CONFIG = "atlas-semantic"
VECTOR_PROFILE = "atlas-hnsw-profile"
HNSW_CONFIG = "atlas-hnsw"
VECTORIZER = "atlas-embed-main"

# Collection types as their documented wire values. `SearchFieldDataType.Collection()` produces the
# same strings but is attached to an enum, which mypy cannot see as callable.
COLLECTION_STRING = "Collection(Edm.String)"
COLLECTION_SINGLE = "Collection(Edm.Single)"


def build_knowledge_index(
    name: str = KNOWLEDGE_INDEX,
    *,
    vectorizer_resource_url: str | None = None,
    embedding_deployment: str = MODEL_EMBED,
) -> SearchIndex:
    """Return the index definition. `create_or_update_index` makes applying it idempotent.

    With `vectorizer_resource_url` (the Foundry account's `https://<acct>.openai.azure.com`), the
    index gets an integrated query vectorizer. ATLAS's own code embeds queries itself and does not
    need it; the Foundry Azure AI Search agent tool does. Without one the tool cannot run a vector
    or hybrid query and falls back to semantic-only ranking, where 500 incident records crowd out
    the runbooks. The Search service calls the model with its own managed identity (Cognitive
    Services OpenAI User on the account, rbac.bicep). Adding a vectorizer and the `url` field to an
    existing index is an in-place update; no re-embedding.
    """
    string = SearchFieldDataType.STRING
    fields = [
        SimpleField(name="id", type=string, key=True, filterable=True),
        SimpleField(name="doc_type", type=string, filterable=True, facetable=True),
        SimpleField(name="doc_id", type=string, filterable=True),
        SearchableField(name="title", type=string),
        SearchableField(name="chunk", type=string),
        SearchField(
            name="services",
            type=COLLECTION_STRING,
            filterable=True,
            facetable=True,
            searchable=True,
        ),
        SimpleField(name="risk", type=string, filterable=True, facetable=True),
        # ISO date as a string: filterable and sortable lexicographically, and it round-trips from
        # the runbook front matter without timezone ambiguity.
        SimpleField(name="last_reviewed", type=string, filterable=True, sortable=True),
        SimpleField(name="owner_team", type=string, filterable=True, facetable=True),
        SimpleField(name="source_path", type=string),
        SimpleField(name="url", type=string),
        # The XPIA canaries are found by this filter in tests and in the Episode 4 injection evals.
        SimpleField(name="injection_canary", type=SearchFieldDataType.BOOLEAN, filterable=True),
        SimpleField(name="content_hash", type=string, filterable=True),
        SearchField(
            name="content_vector",
            type=COLLECTION_SINGLE,
            searchable=True,
            vector_search_dimensions=EMBEDDING_DIMENSIONS,
            vector_search_profile_name=VECTOR_PROFILE,
        ),
    ]

    vectorizers: list[VectorSearchVectorizer] = []
    if vectorizer_resource_url:
        vectorizers.append(
            AzureOpenAIVectorizer(
                vectorizer_name=VECTORIZER,
                parameters=AzureOpenAIVectorizerParameters(
                    resource_url=vectorizer_resource_url,
                    deployment_name=embedding_deployment,
                    model_name="text-embedding-3-large",
                ),
            )
        )

    vector_search = VectorSearch(
        vectorizers=vectorizers or None,
        algorithms=[
            HnswAlgorithmConfiguration(
                name=HNSW_CONFIG,
                parameters=HnswParameters(m=4, ef_construction=400, ef_search=500, metric="cosine"),
            )
        ],
        profiles=[
            VectorSearchProfile(
                name=VECTOR_PROFILE,
                algorithm_configuration_name=HNSW_CONFIG,
                vectorizer_name=VECTORIZER if vectorizers else None,
            )
        ],
    )

    semantic_search = SemanticSearch(
        default_configuration_name=SEMANTIC_CONFIG,
        configurations=[
            SemanticConfiguration(
                name=SEMANTIC_CONFIG,
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name="title"),
                    content_fields=[SemanticField(field_name="chunk")],
                ),
            )
        ],
    )

    return SearchIndex(
        name=name,
        fields=fields,
        vector_search=vector_search,
        semantic_search=semantic_search,
    )
