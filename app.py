import json
import os
import re
import time
from difflib import SequenceMatcher
from typing import Any, Callable, Optional
from datetime import date
from collections import defaultdict, deque
import pandas as pd
import altair as alt

import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.prompts import PromptTemplate
from langchain_core.tools import Tool, StructuredTool, create_retriever_tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_neo4j import GraphCypherQAChain, Neo4jGraph, Neo4jVector
from langchain_openai import ChatOpenAI
from neo4j_viz.neo4j import ColorSpace, from_neo4j
from neo4j_viz import Layout
from pydantic import BaseModel, Field

from neo4j_analysis import Neo4jAnalysis


st.set_page_config(page_title="Legal Legislation Agent", layout="wide")

st.markdown(
    """
    <style>
        [data-testid="stSidebar"] {
            background-color: #ffffff;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

load_dotenv()
NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
AGENT_RETRIEVAL_K = int(os.getenv("AGENT_RETRIEVAL_K", 10))
AGENT_HISTORY_MESSAGES = int(os.getenv("AGENT_HISTORY_MESSAGES", 20))
DEBUG_TOOL_CALLS = os.getenv("DEBUG_TOOL_CALLS") is not None

NETWORK_GRAPH_HEIGHT = 620

@st.cache_resource(show_spinner=False)
def build_runtime():
    if not (NEO4J_URI and NEO4J_USER and NEO4J_PASSWORD):
        raise RuntimeError("Missing Neo4j credentials. Set NEO4J_URI, NEO4J_USER, and NEO4J_PASSWORD.")
    if not (GOOGLE_API_KEY or OPENAI_API_KEY):
        raise RuntimeError("Set GOOGLE_API_KEY or OPENAI_API_KEY in your environment.")

    analysis = Neo4jAnalysis(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, NEO4J_DATABASE)

    graph = Neo4jGraph(
        url=NEO4J_URI,
        username=NEO4J_USER,
        password=NEO4J_PASSWORD,
        database=NEO4J_DATABASE,
    )

    llm = (
        ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            temperature=0,
            api_key=GOOGLE_API_KEY,
            include_thoughts=True,
        )
        if GOOGLE_API_KEY
        else ChatOpenAI(
            model="gpt-5-mini",
            temperature=0,
            api_key=OPENAI_API_KEY,
        )
    )

    embeddings = HuggingFaceEmbeddings(
        model_name="nlpaueb/legal-bert-base-uncased",
        encode_kwargs={"normalize_embeddings": True},
    )

    cypher_prompt = PromptTemplate(
        input_variables=["schema", "question"],
        template="""You are an expert Neo4j Cypher generator for a UK legislation graph.
Generate ONLY a valid read-only Cypher query.

Graph schema:
{schema}

Rules you MUST follow:
1) Return ONLY Cypher. No markdown, no commentary.
2) Read-only queries only. Never use CREATE, MERGE, DELETE, SET, CALL dbms.*, or schema/index changes.
3) When searching for titles, themes or topics, prefer the semantic search tool.
4) Prefer exact property names above and valid relationship directions.
5) When user names an Act/title, match with case-insensitive containment.
6) When user references a known legislation.gov.uk id, filter by l.uri CONTAINS 'ukpga/2010/4' style.
7) For network/visualization requests, return a path variable `p` (e.g., MATCH p=... RETURN p).
8) For tabular requests, RETURN explicit aliased columns and use ORDER BY/LIMIT when reasonable.
9) Avoid Cartesian products; always connect patterns.
10) Use OPTIONAL MATCH only when truly optional.
11) Keep traversal bounded for path exploration (e.g., *1..6 or *1..10).
12) CONTEXT IS MANDATORY for structural/text nodes. Include parent context up to Legislation.
13) Do not return a bare content node alone unless explicitly requested.
14) For relationship alternation, use ONE leading colon only, e.g. [:HAS_PART|HAS_CHAPTER|HAS_SECTION|HAS_SCHEDULE*0..3]. Never write [:HAS_PART|:HAS_CHAPTER|...].

Question: {question}""",
    )

    cypher_chain = GraphCypherQAChain.from_llm(
        graph=graph,
        llm=llm,
        cypher_prompt=cypher_prompt,
        verbose=True,
        allow_dangerous_requests=True,
    )

    vector_store = Neo4jVector.from_existing_index(
        embedding=embeddings,
        url=NEO4J_URI,
        username=NEO4J_USER,
        password=NEO4J_PASSWORD,
        index_name="text_embeddings_index",
        node_label="Text",
        text_node_properties=["title", "description", "text"],
        embedding_node_property="text_embedding",
    )
    retriever = vector_store.as_retriever(search_kwargs={"k": AGENT_RETRIEVAL_K})
    semantic_tool = create_retriever_tool(
        retriever,
        name="Semantic_Search",
        description="Semantic passage retrieval over embedded legal text (title/description/body). Use for concept or topic questions when exact Act title/URI is unknown; returns text snippets, not full hierarchy counts.",
    )

    VECTOR_INDEX_NAME = "text_embeddings_index"
    vector_hits_cache: dict[tuple[str, int], list] = {}
    title_hits_cache: dict[tuple[str, int], list] = {}
    schema_cache: dict[str, str] = {"value": ""}

    def _parse_payload(payload):
        if not payload or not str(payload).strip():
            return {}
        try:
            return json.loads(payload)
        except Exception:
            return {"q": payload}

    def _vector_hits(query_text: str, k: int = AGENT_RETRIEVAL_K):
        if not query_text or not query_text.strip():
            return []
        cache_key = (query_text.strip().lower(), int(k))
        if cache_key in vector_hits_cache:
            return vector_hits_cache[cache_key]

        embedding = embeddings.embed_query(query_text)
        query = """
        CALL db.index.vector.queryNodes($index_name, $k, $embedding)
        YIELD node, score
        RETURN elementId(node) AS node_id,
               labels(node) AS labels,
               score,
               coalesce(node.title, node.text, node.description) AS matched_content,
               node.title AS node_title,
               node.uri AS node_uri
        ORDER BY score DESC
        """
        rows = analysis.run_query(
            query,
            {
                "index_name": VECTOR_INDEX_NAME,
                "k": k,
                "embedding": embedding,
            },
        )
        vector_hits_cache[cache_key] = rows
        return rows

    def _normalize_legal_text(value: str) -> str:
        value = (value or "").lower()
        value = re.sub(r"[^a-z0-9]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    def _extract_year(value: str) -> Optional[int]:
        if not value:
            return None
        match = re.search(r"\b(1[6-9]\d{2}|20\d{2}|2100)\b", value)
        return int(match.group(1)) if match else None

    def _sortable_date(value: Any) -> str:
        if value is None:
            return "0001-01-01"
        try:
            return str(value)
        except Exception:
            return "0001-01-01"

    def _title_candidates(query_text: str, limit: int = 100):
        cleaned = _normalize_legal_text(query_text)
        if not cleaned:
            return []

        cache_key = (cleaned, int(limit))
        if cache_key in title_hits_cache:
            return title_hits_cache[cache_key]

        tokens = [t for t in cleaned.split(" ") if len(t) > 2]
        query = """
        MATCH (l:Legislation)
        WITH l, toLower(coalesce(l.title, "")) AS lt, toLower(coalesce(l.uri, "")) AS lu
        WHERE lt CONTAINS $q
           OR lu CONTAINS $q
           OR any(tok IN $tokens WHERE tok <> '' AND (lt CONTAINS tok OR lu CONTAINS tok))
        RETURN l.title AS title,
               l.uri AS uri,
               l.coming_into_force as coming_into_force,
               l.modified_date as modified_date,
               l.enactment_date AS enactment_date,
               l.status AS status,
               l.category AS category
        LIMIT $limit
        """
        rows = analysis.run_query(
            query,
            {
                "q": cleaned,
                "tokens": tokens,
                "limit": int(limit),
            },
        )
        title_hits_cache[cache_key] = rows
        return rows

    def _rank_title_matches(query_text: str, rows: list[dict], limit: int = 25):
        norm_q = _normalize_legal_text(query_text)
        if not norm_q or not rows:
            return []

        q_tokens = [t for t in norm_q.split(" ") if len(t) > 2]
        q_token_set = set(q_tokens)
        q_year = _extract_year(query_text)
        ranked = []

        for row in rows:
            title = row.get("title", "") or ""
            uri = row.get("uri", "") or ""
            title_norm = _normalize_legal_text(title)
            uri_norm = _normalize_legal_text(uri)

            title_token_set = set([t for t in title_norm.split(" ") if len(t) > 2])
            overlap_count = len(q_token_set.intersection(title_token_set))
            overlap_ratio = overlap_count / max(len(q_token_set), 1)

            lexical_score = 0.0
            if title_norm == norm_q:
                lexical_score += 1.0
            if norm_q in title_norm:
                lexical_score += 0.6
            if title_norm and title_norm in norm_q:
                lexical_score += 0.35
            if norm_q in uri_norm:
                lexical_score += 0.25

            lexical_score += 0.55 * overlap_ratio
            lexical_score += 0.35 * SequenceMatcher(None, norm_q, title_norm).ratio()

            if q_year and str(q_year) in title_norm:
                lexical_score += 0.25

            if "act" in q_token_set and "act" in title_token_set:
                lexical_score += 0.1

            candidate = dict(row)
            candidate["lexical_score"] = round(float(lexical_score), 6)
            ranked.append(candidate)

        ranked.sort(
            key=lambda r: (
                r.get("lexical_score", 0.0),
                _sortable_date(r.get("enactment_date")),
            ),
            reverse=True,
        )
        return ranked[: int(limit)]

    def resolve_legislation_title(payload: str):
        data = _parse_payload(payload)
        q = data.get("q", "")
        limit = int(data.get("limit", 10))
        if not q or not str(q).strip():
            return []

        candidates = _title_candidates(q, limit=250)
        ranked = _rank_title_matches(q, candidates, limit=limit)
        for row in ranked:
            row["match_method"] = "title_resolver"
        return ranked

    def _vector_legislation_candidates(query_text: str, k: int = AGENT_RETRIEVAL_K, limit: int = 25):
        hits = _vector_hits(query_text, k=k)
        if not hits:
            return []

        query = """
        UNWIND $hits AS h
        MATCH (hit) WHERE elementId(hit) = h.node_id
        OPTIONAL MATCH (l_direct:Legislation) WHERE elementId(l_direct) = h.node_id
        OPTIONAL MATCH (l_ctx:Legislation)-[:HAS_PART|HAS_CHAPTER|HAS_SECTION|HAS_PARAGRAPH|HAS_SCHEDULE|HAS_SUBPARAGRAPH|HAS_EXPLANATORY_NOTES*1..6]->(hit)
        WITH h, coalesce(l_direct, l_ctx) AS l
        WHERE l IS NOT NULL
        RETURN l.title AS title,
               l.uri AS uri,
               l.coming_into_force as coming_into_force,
               l.modified_date as modified_date,
               l.enactment_date AS enactment_date,
               l.status AS status,
               l.category AS category,
               max(h.score) AS vector_score
        ORDER BY vector_score DESC, enactment_date DESC
        LIMIT $limit
        """
        return analysis.run_query(query, {"hits": hits, "limit": int(limit)})

    def _hybrid_legislation_lookup(query_text: str, k: int = AGENT_RETRIEVAL_K, limit: int = 25):
        title_ranked = _rank_title_matches(query_text, _title_candidates(query_text, limit=300), limit=150)
        vector_rows = _vector_legislation_candidates(query_text, k=max(int(k), 20), limit=150)

        max_lexical = max([r.get("lexical_score", 0.0) for r in title_ranked], default=0.0)
        max_vector = max([r.get("vector_score", 0.0) for r in vector_rows], default=0.0)

        merged: dict[str, dict] = {}

        for row in title_ranked:
            key = (row.get("uri") or "").strip().lower()
            if not key:
                continue
            merged[key] = {
                **row,
                "vector_score": row.get("vector_score", 0.0) or 0.0,
            }

        for row in vector_rows:
            key = (row.get("uri") or "").strip().lower()
            if not key:
                continue
            if key not in merged:
                merged[key] = {
                    **row,
                    "lexical_score": 0.0,
                }
            else:
                merged[key]["vector_score"] = max(
                    float(merged[key].get("vector_score", 0.0) or 0.0),
                    float(row.get("vector_score", 0.0) or 0.0),
                )

        q_year = _extract_year(query_text)
        out = []
        for item in merged.values():
            lexical_raw = float(item.get("lexical_score", 0.0) or 0.0)
            vector_raw = float(item.get("vector_score", 0.0) or 0.0)

            lexical_norm = (lexical_raw / max_lexical) if max_lexical > 0 else 0.0
            vector_norm = (vector_raw / max_vector) if max_vector > 0 else 0.0

            title_norm = _normalize_legal_text(item.get("title", ""))
            year_bonus = 0.0
            if q_year and str(q_year) in title_norm:
                year_bonus = 0.1

            hybrid_score = 0.65 * lexical_norm + 0.30 * vector_norm + year_bonus

            enriched = dict(item)
            enriched["hybrid_score"] = round(hybrid_score, 6)
            enriched["match_method"] = (
                "hybrid_title_vector" if lexical_raw > 0 and vector_raw > 0 else "title_only" if lexical_raw > 0 else "vector_only"
            )
            out.append(enriched)

        out.sort(
            key=lambda r: (
                r.get("hybrid_score", 0.0),
                _sortable_date(r.get("enactment_date")),
            ),
            reverse=True,
        )
        return out[: int(limit)]

    def schema_navigation(_: str = "") -> str:
        if schema_cache["value"]:
            return schema_cache["value"]

        node_query = """
        CALL apoc.meta.data()
        YIELD label, property, type, elementType
        WHERE elementType = "node"
          AND type <> "RELATIONSHIP"
          AND label <> "Text"
        RETURN label, collect(property + ': ' + type) AS properties
        """
        nodes = analysis.run_query(node_query)

        rel_query = """
        MATCH (a)-[r]->(b)
        WITH [l IN labels(a) WHERE l <> 'Text'] AS start_labels,
             type(r) AS relationship_type,
             [l IN labels(b) WHERE l <> 'Text'] AS end_labels
        WHERE size(start_labels) > 0 AND size(end_labels) > 0
        UNWIND start_labels AS start_label
        UNWIND end_labels AS end_label
        RETURN DISTINCT start_label, relationship_type, end_label
        LIMIT 5000
        """
        rels = analysis.run_query(rel_query)

        schema_text = "GRAPH SCHEMA DEFINITION:\n\n"
        schema_text += "Node Labels and Properties:\n"
        for node in nodes:
            props = ", ".join(node["properties"]) if node["properties"] else "No properties"
            schema_text += f"   - (:{node['label']} {{ {props} }})\\n"

        schema_text += "\nValid Relationship Connections:\n"
        if rels:
            for rel in rels:
                schema_text += f"   - (:{rel['start_label']})-[:{rel['relationship_type']}]->(:{rel['end_label']})\\n"
        else:
            schema_text += "   - No relationships found.\\n"

        schema_cache["value"] = schema_text
        return schema_text

    def find_legislation(payload: str):
        data = _parse_payload(payload)
        q = data.get("q", "")
        k = int(data.get("k", AGENT_RETRIEVAL_K))
        limit = int(data.get("limit", 25))
        if not q or not str(q).strip():
            return []
        return _hybrid_legislation_lookup(q, k=k, limit=limit)

    def retrieve_text_with_context(payload: str):
        data = _parse_payload(payload)
        q = data.get("q", "")
        k = int(data.get("k", AGENT_RETRIEVAL_K))
        limit = int(data.get("limit", 15))
        hits = _vector_hits(q, k=k)
        if not hits:
            return []

        query = """
        UNWIND $hits AS h
        MATCH (n) WHERE elementId(n) = h.node_id
        OPTIONAL MATCH p=(l:Legislation)-[:HAS_PART|HAS_CHAPTER|HAS_SECTION|HAS_PARAGRAPH|HAS_SCHEDULE|HAS_SUBPARAGRAPH|HAS_EXPLANATORY_NOTES*0..6]->(n)
        WITH h, n, l, p,
             head([x IN nodes(p) WHERE x:Part]) AS part,
             head([x IN nodes(p) WHERE x:Chapter]) AS chapter,
             head([x IN nodes(p) WHERE x:Section]) AS section,
             head([x IN nodes(p) WHERE x:Paragraph]) AS paragraph
        WHERE l IS NOT NULL
        RETURN DISTINCT l.title AS legislation_title,
               l.uri AS legislation_uri,
               l.status as legislation_status,
               l.coming_into_force as legislation_coming_into_force,
               l.modified_date as legislation_modified_date,
               part.number AS part_number,
               part.title AS part_title,
               part.restrict_start_date AS part_restrict_start_date,
               part.restrict_end_date AS part_restrict_end_date,
               part.restrict_extent AS part_restrict_extent,
               part.status AS part_status,
               chapter.number AS chapter_number,
               chapter.title AS chapter_title,
               chapter.restrict_start_date AS chapter_restrict_start_date,
               chapter.restrict_end_date AS chapter_restrict_end_date,
               chapter.restrict_extent AS chapter_restrict_extent,
               chapter.status AS chapter_status,
               section.number AS section_number,
               section.title AS section_title,
               section.restrict_start_date AS section_restrict_start_date,
               section.restrict_end_date AS section_restrict_end_date,
               section.restrict_extent AS section_restrict_extent,
               section.status AS section_status,
               paragraph.number AS paragraph_number,
               paragraph.restrict_start_date AS paragraph_restrict_start_date,
               paragraph.restrict_end_date AS paragraph_restrict_end_date,
               paragraph.restrict_extent AS paragraph_restrict_extent,
               paragraph.status AS paragraph_status,
               coalesce(paragraph.text, n.text, n.title, n.description) AS matched_text,
               h.score AS vector_score
        ORDER BY vector_score DESC
        LIMIT $limit
        """
        return analysis.run_query(query, {"hits": hits, "limit": limit})

    class ContextualTextRetrieverInput(BaseModel):
        q: str = Field(..., description="Natural language legal query.")
        k: int = Field(default=AGENT_RETRIEVAL_K, ge=1, le=100, description="Top-k vector hits.")
        limit: int = Field(default=15, ge=1, le=100, description="Max rows to return.")

    def retrieve_text_with_context_structured(q: str, k: int = AGENT_RETRIEVAL_K, limit: int = 15):
        payload = json.dumps({"q": q, "k": k, "limit": limit})
        return retrieve_text_with_context(payload)

    def citation_reasoning(payload: str):
        data = _parse_payload(payload)
        q = data.get("q", "")
        k = int(data.get("k", AGENT_RETRIEVAL_K))
        hits = _vector_hits(q, k=k)
        if not hits:
            return []

        query = """
        UNWIND $hits AS h
        MATCH (hit) WHERE elementId(hit) = h.node_id
        OPTIONAL MATCH (source_direct:Legislation) WHERE elementId(source_direct) = h.node_id
        OPTIONAL MATCH (source_ctx:Legislation)-[:HAS_PART|HAS_CHAPTER|HAS_SECTION|HAS_PARAGRAPH|HAS_SCHEDULE|HAS_SUBPARAGRAPH|HAS_EXPLANATORY_NOTES*1..6]->(hit)
        WITH h, coalesce(source_direct, source_ctx) AS source
        WHERE source IS NOT NULL
        OPTIONAL MATCH (source)-[r:CITES]->(target:Legislation)
        RETURN source.title AS source_title,
               source.uri AS source_uri,
               target.title AS target_title,
               target.uri AS target_uri,
               type(r) AS relationship_type,
               h.score AS vector_score
        ORDER BY vector_score DESC
        LIMIT 20
        """
        return analysis.run_query(query, {"hits": hits})

    def supersedes_chain(payload: str):
        data = _parse_payload(payload)
        q = data.get("q", "")
        query = """
        MATCH (source:Legislation)
        WHERE toLower(coalesce(source.title, "")) CONTAINS toLower($q)
           OR toLower(coalesce(source.uri, "")) CONTAINS toLower($q)
        OPTIONAL MATCH (source)-[:SUPERSEDES]->(target:Legislation)
        RETURN source.title AS source_title,
               source.uri AS source_uri,
               target.title AS target_title,
               target.uri AS target_uri
        LIMIT 20
        """
        return analysis.run_query(query, {"q": q})

    def superseded_chain(payload: str):
        data = _parse_payload(payload)
        q = data.get("q", "")
        query = """
        MATCH (source:Legislation)
        WHERE toLower(coalesce(source.title, "")) CONTAINS toLower($q)
           OR toLower(coalesce(source.uri, "")) CONTAINS toLower($q)
        OPTIONAL MATCH (source)-[:SUPERSEDED_BY]->(target:Legislation)
        RETURN source.title AS source_title,
               source.uri AS source_uri,
               target.title AS target_title,
               target.uri AS target_uri
        LIMIT 20
        """
        return analysis.run_query(query, {"q": q})

    def read_only_cypher(payload: str):
        forbidden = r"\b(CREATE|MERGE|DELETE|DETACH|SET|DROP|REMOVE)\b"
        if re.search(forbidden, payload, flags=re.IGNORECASE):
            return {"error": "Only read-only Cypher is allowed in this tool."}
        normalized_payload = payload.replace("|:", "|")
        return analysis.run_query(normalized_payload)

    def legislation_by_uri(payload: str):
        data = _parse_payload(payload)
        uri = data.get("uri") or data.get("q", "")
        if not uri:
            return {"error": "Provide 'uri' (or 'q') in payload."}

        query = """
        MATCH (l:Legislation)
        WHERE l.uri = $uri OR l.uri CONTAINS $uri
        RETURN l.title AS title,
               l.uri AS uri,
               l.enactment_date AS enactment_date,
               l.category AS category,
               l.status as status,
               l.coming_into_force as coming_into_force,
               l.modified_date as modified_date
        ORDER BY l.enactment_date DESC
        LIMIT 5
        """
        return analysis.run_query(query, {"uri": uri})

    def hierarchy_path_resolver(payload: str):
        data = _parse_payload(payload)
        node_id = data.get("node_id")
        uri = data.get("uri")

        if not node_id and not uri:
            return {"error": "Provide 'node_id' (elementId) or 'uri'."}

        query_by_node = """
        MATCH (n)
        WHERE elementId(n) = $node_id
        OPTIONAL MATCH p=(l:Legislation)-[:HAS_PART|HAS_CHAPTER|HAS_SECTION|HAS_PARAGRAPH|HAS_SCHEDULE|HAS_SUBPARAGRAPH|HAS_EXPLANATORY_NOTES*0..6]->(n)
        WITH n, l, p,
             head([x IN nodes(p) WHERE x:Part]) AS part,
             head([x IN nodes(p) WHERE x:Chapter]) AS chapter,
             head([x IN nodes(p) WHERE x:Section]) AS section,
             head([x IN nodes(p) WHERE x:Paragraph]) AS paragraph
        RETURN labels(n) AS node_labels,
               coalesce(n.uri, n.id, elementId(n)) AS node_ref,
               l.title AS legislation_title,
               l.uri AS legislation_uri,
               l.status as legislation_status,
               l.coming_into_force as legislation_coming_into_force,
               l.modified_date as legislation_modified_date,
               part.number AS part_number,
               part.title AS part_title,
               part.restrict_start_date AS part_restrict_start_date,
               part.restrict_end_date AS part_restrict_end_date,
               part.restrict_extent AS part_restrict_extent,
               part.status AS part_status,
               chapter.number AS chapter_number,
               chapter.title AS chapter_title,
               chapter.restrict_start_date AS chapter_restrict_start_date,
               chapter.restrict_end_date AS chapter_restrict_end_date,
               chapter.restrict_extent AS chapter_restrict_extent,
               chapter.status AS chapter_status,
               section.number AS section_number,
               section.title AS section_title,
               section.restrict_start_date AS section_restrict_start_date,
               section.restrict_end_date AS section_restrict_end_date,
               section.restrict_extent AS section_restrict_extent,
               section.status AS section_status,
               paragraph.number AS paragraph_number,
               paragraph.restrict_start_date AS paragraph_restrict_start_date,
               paragraph.restrict_end_date AS paragraph_restrict_end_date,
               paragraph.restrict_extent AS paragraph_restrict_extent,
               paragraph.status AS paragraph_status
        LIMIT 10
        """

        query_by_uri = """
        MATCH (n)
        WHERE n.uri = $uri OR n.uri CONTAINS $uri
        OPTIONAL MATCH p=(l:Legislation)-[:HAS_PART|HAS_CHAPTER|HAS_SECTION|HAS_PARAGRAPH|HAS_SCHEDULE|HAS_SUBPARAGRAPH|HAS_EXPLANATORY_NOTES*0..6]->(n)
        WITH n, l, p,
             head([x IN nodes(p) WHERE x:Part]) AS part,
             head([x IN nodes(p) WHERE x:Chapter]) AS chapter,
             head([x IN nodes(p) WHERE x:Section]) AS section,
             head([x IN nodes(p) WHERE x:Paragraph]) AS paragraph
        RETURN labels(n) AS node_labels,
               coalesce(n.uri, n.id, elementId(n)) AS node_ref,
               l.title AS legislation_title,
               l.uri AS legislation_uri,
               l.status as legislation_status,
               l.coming_into_force as legislation_coming_into_force,
               l.modified_date as legislation_modified_date,
               part.number AS part_number,
               part.title AS part_title,
               part.restrict_start_date AS part_restrict_start_date,
               part.restrict_end_date AS part_restrict_end_date,
               part.restrict_extent AS part_restrict_extent,
               part.status AS part_status,
               chapter.number AS chapter_number,
               chapter.title AS chapter_title,
               chapter.restrict_start_date AS chapter_restrict_start_date,
               chapter.restrict_end_date AS chapter_restrict_end_date,
               chapter.restrict_extent AS chapter_restrict_extent,
               chapter.status AS chapter_status,
               section.number AS section_number,
               section.title AS section_title,
               section.restrict_start_date AS section_restrict_start_date,
               section.restrict_end_date AS section_restrict_end_date,
               section.restrict_extent AS section_restrict_extent,
               section.status AS section_status,
               paragraph.number AS paragraph_number,
               paragraph.restrict_start_date AS paragraph_restrict_start_date,
               paragraph.restrict_end_date AS paragraph_restrict_end_date,
               paragraph.restrict_extent AS paragraph_restrict_extent,
               paragraph.status AS paragraph_status
        LIMIT 10
        """

        if node_id:
            return analysis.run_query(query_by_node, {"node_id": node_id})
        return analysis.run_query(query_by_uri, {"uri": uri})

    def citation_counts(payload: str):
        data = _parse_payload(payload)
        q = data.get("uri") or data.get("q", "")
        if not q:
            return {"error": "Provide 'uri' or 'q'."}

        query = """
        MATCH (l:Legislation)
        WHERE l.uri = $q OR l.uri CONTAINS $q OR toLower(coalesce(l.title, "")) CONTAINS toLower($q)
        CALL(l) {
          WITH l
          OPTIONAL MATCH (l)-[:LINKED_TO]->(t:Legislation)
          RETURN count(DISTINCT t) AS outgoing_count, collect(DISTINCT t.title)[0..5] AS top_outgoing_titles
        }
        CALL(l) {
          WITH l
          OPTIONAL MATCH (s:Legislation)-[:LINKED_TO]->(l)
          RETURN count(DISTINCT s) AS incoming_count, collect(DISTINCT s.title)[0..5] AS top_incoming_titles
        }
        RETURN l.title AS legislation_title,
               l.uri AS legislation_uri,
               outgoing_count,
               incoming_count,
               top_outgoing_titles,
               top_incoming_titles
        LIMIT 5
        """
        return analysis.run_query(query, {"q": q})

    class LegislationFinderInput(BaseModel):
        q: str = Field(..., description="Natural language query.")
        k: int = Field(default=AGENT_RETRIEVAL_K, ge=1, le=100, description="Top-k vector hits.")
        limit: int = Field(default=25, ge=1, le=100, description="Max rows to return.")

    def find_legislation_structured(q: str, k: int = AGENT_RETRIEVAL_K, limit: int = 25):
        return find_legislation(json.dumps({"q": q, "k": k, "limit": limit}))

    class LegislationTitleResolverInput(BaseModel):
        q: str = Field(..., description="Legislation title-style query (e.g., Corporation Tax Act 2010).")
        limit: int = Field(default=10, ge=1, le=50, description="Max rows to return.")

    def resolve_legislation_title_structured(q: str, limit: int = 10):
        return resolve_legislation_title(json.dumps({"q": q, "limit": limit}))

    class CitationNetworkExplorerInput(BaseModel):
        q: str = Field(..., description="Natural language query.")
        k: int = Field(default=AGENT_RETRIEVAL_K, ge=1, le=100, description="Top-k vector hits.")

    def citation_reasoning_structured(q: str, k: int = AGENT_RETRIEVAL_K):
        return citation_reasoning(json.dumps({"q": q, "k": k}))

    class SupersedesNetworkInput(BaseModel):
        q: str = Field(..., description="Legislation title or URI fragment.")

    def supersedes_chain_structured(q: str):
        return supersedes_chain(json.dumps({"q": q}))

    class SupersededByNetworkInput(BaseModel):
        q: str = Field(..., description="Legislation title or URI fragment.")

    def superseded_chain_structured(q: str):
        return superseded_chain(json.dumps({"q": q}))

    class LegislationByUriInput(BaseModel):
        uri: Optional[str] = Field(default=None, description="Full or partial legislation URI.")
        q: Optional[str] = Field(default=None, description="Alternative URI/title query.")

    def legislation_by_uri_structured(uri: Optional[str] = None, q: Optional[str] = None):
        return legislation_by_uri(json.dumps({"uri": uri, "q": q}))

    class HierarchyPathResolverInput(BaseModel):
        node_id: Optional[str] = Field(default=None, description="Neo4j elementId for a node.")
        uri: Optional[str] = Field(default=None, description="Full or partial URI for a node.")

    def hierarchy_path_resolver_structured(node_id: Optional[str] = None, uri: Optional[str] = None):
        return hierarchy_path_resolver(json.dumps({"node_id": node_id, "uri": uri}))

    class CitationCountsInput(BaseModel):
        q: Optional[str] = Field(default=None, description="Legislation title or URI fragment.")
        uri: Optional[str] = Field(default=None, description="Full or partial legislation URI.")

    def citation_counts_structured(q: Optional[str] = None, uri: Optional[str] = None):
        return citation_counts(json.dumps({"q": q, "uri": uri}))

    class ReadOnlyCypherInput(BaseModel):
        query: str = Field(..., description="Read-only Cypher query string.")

    def read_only_cypher_structured(query: str):
        return read_only_cypher(query)

    class Text2CypherExpertInput(BaseModel):
        question: str = Field(..., description="Natural language question to translate to Cypher and execute.")

    def text2cypher_expert_structured(question: str):
        return cypher_chain.invoke({"query": question})

    graph_schema_tool = Tool(
        name="Graph_Schema_Navigator",
        func=schema_navigation,
        description="Return current Neo4j schema: node labels, properties, and valid relationship directions. Call first before ad-hoc Cypher or when query planning is uncertain. Input may be empty.",
    )
    legislation_finder_tool = StructuredTool.from_function(
        name="Legislation_Finder",
        func=find_legislation_structured,
        args_schema=LegislationFinderInput,
        description="Primary Act discovery tool. Input: natural-language `q` plus optional `k` and `limit`. Uses hybrid lexical-title matching + vector evidence and returns ranked legislation candidates with `hybrid_score`, match method, URI, title, dates, status, and category.",
    )
    legislation_title_resolver_tool = StructuredTool.from_function(
        name="Legislation_Title_Resolver",
        func=resolve_legislation_title_structured,
        args_schema=LegislationTitleResolverInput,
        description="High-precision resolver for explicit Act-title queries (for example 'Corporation Tax Act 2010'). Use before semantic tools when user intent is a specific named Act. Returns ranked title/URI matches with lexical score.",
    )
    text_context_tool = StructuredTool.from_function(
        name="Contextual_Text_Retriever",
        func=retrieve_text_with_context_structured,
        args_schema=ContextualTextRetrieverInput,
        description="Retrieve evidence passages with full legal context. Input: `q` (+ optional `k`, `limit`). Returns matched text plus enclosing Legislation/Part/Chapter/Section/Paragraph and restriction metadata (`restrict_start_date`, `restrict_end_date`, `restrict_extent`).",
    )
    citation_tool = StructuredTool.from_function(
        name="Citation_Network_Explorer",
        func=citation_reasoning_structured,
        args_schema=CitationNetworkExplorerInput,
        description="Citation expansion tool. Starts from vector-relevant source legislation and returns citation edges to target legislation (`source_*`, `target_*`, relationship type, vector score). Use for 'what cites what' questions.",
    )
    supersedes_tool = StructuredTool.from_function(
        name="Supersedes_Network_Explorer",
        func=supersedes_chain_structured,
        args_schema=SupersedesNetworkInput,
        description="Outgoing replacement lineage. Input: legislation title or URI fragment `q`. Returns acts that the matched source legislation supersedes.",
    )
    superseded_tool = StructuredTool.from_function(
        name="Superseded_By_Network_Explorer",
        func=superseded_chain_structured,
        args_schema=SupersededByNetworkInput,
        description="Incoming replacement lineage. Input: legislation title or URI fragment `q`. Returns acts that supersede the matched source legislation.",
    )
    safe_cypher_tool = StructuredTool.from_function(
        name="Read_Only_Cypher",
        func=read_only_cypher_structured,
        args_schema=ReadOnlyCypherInput,
        description="Execute analyst-specified read-only Cypher only. Forbid write operations (CREATE/MERGE/DELETE/SET/etc). Use when specialized query shape is needed and domain tools are insufficient.",
    )
    text2cypher_tool = StructuredTool.from_function(
        name="Text2Cypher_Expert",
        func=text2cypher_expert_structured,
        args_schema=Text2CypherExpertInput,
        description="Last-resort NL-to-Cypher executor for complex questions not covered by specialized tools. Use only after trying dedicated tools; outputs chain result from generated read-only Cypher.",
    )
    legislation_by_uri_tool = StructuredTool.from_function(
        name="Legislation_By_URI",
        func=legislation_by_uri_structured,
        args_schema=LegislationByUriInput,
        description="Deterministic metadata lookup for a known Act URI (full or partial) or URI-like query. Returns canonical legislation records (title, URI, enactment date, category, status, coming-into-force, modified date).",
    )
    hierarchy_path_resolver_tool = StructuredTool.from_function(
        name="Hierarchy_Path_Resolver",
        func=hierarchy_path_resolver_structured,
        args_schema=HierarchyPathResolverInput,
        description="Hierarchy reconstruction tool. Input either `node_id` (elementId) or `uri`; returns node labels/ref and surrounding Legislation > Part > Chapter > Section > Paragraph context with temporal/status fields.",
    )
    citation_counts_tool = StructuredTool.from_function(
        name="Citation_Counts",
        func=citation_counts_structured,
        args_schema=CitationCountsInput,
        description="Fast citation metrics for one legislation (by `uri` or `q`). Returns inbound/outbound LINKED_TO counts and sample top linked titles; use for quick influence/connectedness summaries.",
    )

    tools = [
        graph_schema_tool,
        legislation_title_resolver_tool,
        legislation_finder_tool,
        text_context_tool,
        citation_tool,
        supersedes_tool,
        superseded_tool,
        safe_cypher_tool,
        text2cypher_tool,
        semantic_tool,
        legislation_by_uri_tool,
        hierarchy_path_resolver_tool,
        citation_counts_tool
    ]

    system_prompt = """You are a highly capable legal AI assistant.
Use the most specific tool first. Use the Graph_Schema_Navigator before other tools to understand the schema. Prefer granular tools before Text2Cypher_Expert.
For explicit Act/title lookup queries (for example: 'Corporation Tax Act 2010'), call Legislation_Title_Resolver first.
For retrieval tasks, prefer Legislation_Finder (hybrid title+vector) and then vector-index-backed tools (Contextual_Text_Retriever, Citation_Network_Explorer, Semantic_Search).
Always preserve legal context (Legislation > Part > Chapter > Section > Paragraph) when answering content questions.
If a tool returns empty results, do not repeat the exact same call. Always include links to relevant legislation, sections and parts in your responses.
Use Legislation_By_URI for exact act lookup, Hierarchy_Path_Resolver for context reconstruction and Citation_Counts for quick citation metrics.
Focus on the precise problem you are asked, and do not do more than asked."""

    agent_executor = create_agent(llm, tools, system_prompt=system_prompt)
    return analysis, agent_executor


def stream_agent_answer(
    agent_executor,
    chat_messages: list[dict],
    on_tool_event: Optional[Callable[[list[dict]], None]] = None,
):
    final_answer = ""
    tool_events = []
    run_start = time.perf_counter()
    tool_start_times = defaultdict(deque)
    debug_tools = DEBUG_TOOL_CALLS

    lc_messages = []
    for msg in chat_messages[-AGENT_HISTORY_MESSAGES:]:
        role = msg.get("role")
        content = msg.get("content", "")
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            lc_messages.append((role, content))

    for event in agent_executor.stream(
        {"messages": lc_messages},
        stream_mode="updates",
    ):
        if not isinstance(event, dict):
            continue

        elapsed = round(time.perf_counter() - run_start, 3)
        for node_name, data in event.items():
            messages = data.get("messages", []) if isinstance(data, dict) else []
            if not messages:
                continue

            msg = messages[-1]
            msg_type = getattr(msg, "type", None)

            if msg_type == "ai" and getattr(msg, "tool_calls", None):
                for tool_call in msg.tool_calls:
                    tool_name = tool_call.get("name", "unknown")
                    tool_args = tool_call.get("args", {})
                    started_at = time.perf_counter()
                    tool_start_times[tool_name].append(started_at)
                    if debug_tools:
                        print(
                            f"[DEBUG:TOOL_CALL] node={node_name} elapsed_s={elapsed} "
                            f"tool={tool_name} args={json.dumps(tool_args, ensure_ascii=False, default=str)}"
                        )
                    tool_events.append(
                        {
                            "elapsed_s": elapsed,
                            "node": node_name,
                            "type": "tool_call",
                            "tool_name": tool_name,
                            "args": tool_args,
                        }
                    )
                    if on_tool_event:
                        on_tool_event(tool_events)

            elif msg_type == "tool":
                tool_name = getattr(msg, "name", "unknown")
                finished_at = time.perf_counter()
                duration_s = None
                if tool_start_times[tool_name]:
                    started_at = tool_start_times[tool_name].popleft()
                    duration_s = round(finished_at - started_at, 3)

                raw_content = getattr(msg, "content", "")
                preview = raw_content
                if isinstance(raw_content, str) and len(raw_content) > 1200:
                    preview = raw_content[:1200] + "..."

                tool_events.append(
                    {
                        "elapsed_s": elapsed,
                        "node": node_name,
                        "type": "tool_result",
                        "tool_name": tool_name,
                        "duration_s": duration_s,
                        "content_preview": preview,
                    }
                )
                if on_tool_event:
                    on_tool_event(tool_events)
                if debug_tools:
                    print(
                        f"[DEBUG:TOOL_RESULT] node={node_name} elapsed_s={elapsed} "
                        f"tool={tool_name} duration_s={duration_s}"
                    )

            elif msg_type == "ai":
                content = getattr(msg, "content", "")
                if isinstance(content, list):
                    text_blocks = [b.get("text", "") for b in content if isinstance(b, dict)]
                    text = "\n".join([t for t in text_blocks if t]).strip()
                else:
                    text = str(content).strip()

                if text:
                    final_answer = text

    return final_answer, tool_events


st.title("UK Legislation Graph Agent")
st.caption("A demonstration of time aware GraphRAG for policy, tax and legal governance.")

with st.sidebar:
    st.image("https://pbs.twimg.com/profile_images/940510063718031360/Mv-_CAlX_400x400.jpg", width=100)
    st.subheader("Pick a view")
    selected_use_case = st.radio(
        "Select a view",
        [
            "Chat Interface",
            "The Complete Graph",
            "Legislation Graph",
            "Parts",
            "Commentaries",
            "Schedules",
            "Supersedes/Superseded By",
            "Point in Time",
            "Temporal Diff (As-Of vs As-Of)",
        ],
        index=0,
    )

    use_case_params = {"height": NETWORK_GRAPH_HEIGHT}

    if selected_use_case == "Legislation Graph":
        use_case_params["uri_contains"] = st.text_input(
            "Legislation URI contains",
            value="ukpga/2010/4",
            key="uc_legislation_uri",
        )
    elif selected_use_case == "Parts":
        use_case_params["uri_contains"] = st.text_input(
            "Legislation URI contains",
            value="ukpga/2010/4",
            key="uc_part_uri",
        )
        use_case_params["part_order"] = st.number_input(
            "Part order",
            min_value=1,
            step=1,
            value=2,
            key="uc_part_order",
        )
    elif selected_use_case == "Commentaries":
        use_case_params["uri_contains"] = st.text_input(
            "Legislation URI contains",
            value="ukpga/2018/12",
            key="uc_commentaries_uri",
        )
    elif selected_use_case == "Schedules":
        use_case_params["uri_contains"] = st.text_input(
            "Legislation URI contains",
            value="ukpga/2010/4",
            key="uc_schedules_uri",
        )
    elif selected_use_case == "Point in Time":
        use_case_params["uri_contains"] = st.text_input(
            "Legislation URI contains",
            value="ukpga/2010/4",
            key="uc_point_time_uri",
        )
        use_case_params["cutoff_date"] = st.date_input(
            "Cutoff date",
            value=date(2018, 1, 1),
            key="uc_point_time_cutoff_date",
        ).isoformat()
    elif selected_use_case == "Temporal Diff (As-Of vs As-Of)":
        use_case_params["uri_contains"] = st.text_input(
            "Legislation URI contains",
            value="ukpga/2010/4",
            key="uc_temporal_diff_uri",
        )
        use_case_params["cutoff_date"] = st.date_input(
            "Cutoff date",
            value=date(2018, 1, 1),
            key="uc_temporal_diff_cutoff",
        ).isoformat()

    st.markdown("---")
    st.caption("Select 'Chat Interface' to open the assistant chat.")


def _render_use_case_graph(
    analysis: Neo4jAnalysis,
    query: str,
    params=None,
    height: int = NETWORK_GRAPH_HEIGHT,
    enlarged_node_ids: Optional[set[Any]] = None,
    enlarged_node_size: int = 40,
):
    colors = {
        "Legislation": "#1f77b4",
        "Part": "#ff7f0e",
        "Chapter": "#2ca02c",
        "Section": "#d62728",
        "Paragraph": "#9467bd",
        "Schedule": "#8c564b",
        "ScheduleParagraph": "#e377c2",
        "ScheduleSubparagraph": "#7f7f7f",
        "Commentary": "#bcbd22",
        "Citation": "#17becf",
        "CitationSubRef": "#aec7e8",
        "ExplanatoryNotes": "#ffbb78",
        "ExplanatoryNotesParagraph": "#98df8a"
    }
    label_to_property = {
        "Legislation": "title",
        "Part": "title",
        "Chapter": "title",
        "Section": "title",
        "Paragraph": "number",
        "Schedule": "title",
        "ScheduleParagraph": "number",
        "ScheduleSubparagraph": "number",
        "Commentary": "text",
        "Citation": "text",
        "CitationSubRef": "text",
        "ExplanatoryNotes": "uri",
        "ExplanatoryNotesParagraph": "text",
    }

    results = analysis.run_query_viz(query, params or {})
    VG = from_neo4j(results)
    VG.color_nodes(field="caption", color_space=ColorSpace.DISCRETE, colors=colors)
    analysis.set_caption_by_label(VG, label_to_property)

    if enlarged_node_ids:
        sizes = {}
        for node in getattr(VG, "nodes", []):
            props = getattr(node, "properties", {}) or {}
            node_id_property = props.get("id")
            if node_id_property is not None and str(node_id_property) in enlarged_node_ids:
                sizes[getattr(node, "id")] = enlarged_node_size

        if sizes:
            VG.resize_nodes(sizes=sizes, node_radius_min_max=None)

    generated_html = VG.render(layout=Layout.FORCE_DIRECTED, initial_zoom=1.0)
    html_str = generated_html.data if hasattr(generated_html, "data") else str(generated_html)
    components.html(html_str, height=height, scrolling=True)


def _show_use_case_panel(analysis: Neo4jAnalysis, selected_use_case: str, use_case_params: dict):
    st.subheader(selected_use_case)

    use_case_descriptions = {
        "Chat Interface":
            "**Ask natural-language questions to explore legislation structure, citations, supersession links, and point-in-time context.**",
        "The Complete Graph":
            "**Visualize the full graph schema, including available node types and how they are connected.**",
        "Legislation Graph":
            "**Inspect a single legislation hierarchy from legislation to parts, chapters, and sections.**",
        "Parts":
            "**Focus on a specific part within a legislation and trace its chapters, sections, paragraphs, and commentary links.**",
        "Commentaries":
            "**Explore commentary and citation chains that connect interpretive notes back to legislation.**",
        "Schedules":
            "**View schedule structures and nested paragraphs/subparagraphs, including optional commentary citation paths.**",
        "Supersedes/Superseded By":
            "**Analyze legislation-to-legislation replacement relationships across supersedes and superseded-by links.**",
        "Point in Time":
            "**Use one cutoff date: left shows content active at cutoff; right shows post-cutoff content with changed nodes sized larger.**",
        "Temporal Diff (As-Of vs As-Of)":
            "**Use one cutoff date and compare cutoff vs today to surface provisions that were added, removed, or restricted after cutoff.**",
    }
    st.caption(use_case_descriptions.get(selected_use_case, ""))

    if selected_use_case == "Chat Interface":
        return

    if selected_use_case == "The Complete Graph":
        query = """
        CALL db.schema.visualization()
        YIELD nodes, relationships
        // Filter by the virtual node's label instead of its name property
        WITH [n IN nodes WHERE labels(n)[0] <> 'Text'] AS filtered_nodes, relationships
        WITH filtered_nodes, 
            [r IN relationships WHERE startNode(r) IN filtered_nodes AND endNode(r) IN filtered_nodes] AS filtered_rels
        RETURN filtered_nodes AS nodes, filtered_rels AS relationships
        """
        _render_use_case_graph(analysis, query, height=NETWORK_GRAPH_HEIGHT)
    elif selected_use_case == "Legislation Graph":
        query = """
        MATCH p=(l:Legislation)-[:HAS_PART]->(:Part)-[:HAS_CHAPTER]->(:Chapter)-[:HAS_SECTION]->(:Section)
        WHERE l.uri CONTAINS $uri_contains
        RETURN p
        """
        _render_use_case_graph(
            analysis,
            query,
            params={"uri_contains": use_case_params.get("uri_contains", "ukpga/2010/4")},
            height=NETWORK_GRAPH_HEIGHT,
        )
    elif selected_use_case == "Parts":
        query = """
        MATCH p=(l:Legislation)-[:HAS_PART]->(part:Part)-[:HAS_CHAPTER]->(:Chapter)-[:HAS_SECTION]->(section:Section)-[:HAS_PARAGRAPH]->(para:Paragraph)-[:HAS_COMMENTARY]->(comm:Commentary)
        WHERE l.uri CONTAINS $uri_contains AND part.order = $part_order
        RETURN p
        """
        _render_use_case_graph(
            analysis,
            query,
            params={
                "uri_contains": use_case_params.get("uri_contains", "ukpga/2010/4"),
                "part_order": int(use_case_params.get("part_order", 2)),
            },
            height=NETWORK_GRAPH_HEIGHT,
        )
    elif selected_use_case == "Commentaries":
        query = """
        MATCH p=(:Commentary)-[:HAS_CITATION]->(:Citation)-[:CITES]->(l:Legislation)
        WHERE l.uri CONTAINS $uri_contains
        RETURN p
        """
        _render_use_case_graph(
            analysis,
            query,
            params={"uri_contains": use_case_params.get("uri_contains", "ukpga/2018/12")},
            height=NETWORK_GRAPH_HEIGHT,
        )
    elif selected_use_case == "Schedules":
        query = """
        MATCH p=(l:Legislation)-[:HAS_SCHEDULE]->(sc:Schedule)-[:HAS_PARAGRAPH]->(scp:ScheduleParagraph)-[:HAS_SUBPARAGRAPH]->(scsp:ScheduleSubparagraph)
        WHERE l.uri CONTAINS $uri_contains
        OPTIONAL MATCH (scp)-[:HAS_COMMENTARY]-(:Commentary)-[:HAS_CITATION]-(:Citation)-[:HAS_SUBREF]->(:CitationSubRef)
        RETURN p
        """
        _render_use_case_graph(
            analysis,
            query,
            params={"uri_contains": use_case_params.get("uri_contains", "ukpga/2010/4")},
            height=NETWORK_GRAPH_HEIGHT,
        )
    elif selected_use_case == "Supersedes/Superseded By":
        query = """
        MATCH p=(:Legislation)-[:SUPERSEDED_BY|SUPERSEDES]-(:Legislation)
        RETURN p
        """
        _render_use_case_graph(analysis, query, height=NETWORK_GRAPH_HEIGHT)
    elif selected_use_case == "Point in Time":
        left_query = """
        MATCH p=(l:Legislation)-[:HAS_PART]->(part:Part)-[:HAS_CHAPTER]->(:Chapter)-[:HAS_SECTION]->(section:Section)-[:HAS_PARAGRAPH]->(para:Paragraph)-[:HAS_COMMENTARY]->(comm:Commentary)
        WHERE l.uri CONTAINS $uri_contains
          AND coalesce(para.restrict_start_date, date('0001-01-01')) <= date($cutoff_date)
          AND (para.restrict_end_date IS NULL OR para.restrict_end_date >= date($cutoff_date))
        RETURN p
        """

        right_query = """
        MATCH p=(l:Legislation)-[:HAS_PART]->(part:Part)-[:HAS_CHAPTER]->(:Chapter)-[:HAS_SECTION]->(section:Section)-[:HAS_PARAGRAPH]->(para:Paragraph)-[:HAS_COMMENTARY]->(comm:Commentary)
        WHERE l.uri CONTAINS $uri_contains
          AND coalesce(para.restrict_end_date, date('9999-12-31')) >= date($cutoff_date)
        RETURN p
        """

        cutoff_date = use_case_params.get("cutoff_date", "2018-01-01")
        today_date = date.today().isoformat()
        uri_contains = use_case_params.get("uri_contains", "ukpga/2010/4")

        altered_nodes_query = """
        MATCH (l:Legislation)-[:HAS_PART]->(:Part)-[:HAS_CHAPTER]->(:Chapter)-[:HAS_SECTION]->(:Section)-[:HAS_PARAGRAPH]->(para:Paragraph)-[:HAS_COMMENTARY]->(:Commentary)
        WHERE l.uri CONTAINS $uri_contains
        WITH para,
             (para.restrict_start_date IS NOT NULL
                AND para.restrict_start_date > date($cutoff_date)
                AND para.restrict_start_date <= date($today_date)) AS started_after_cutoff,
             (para.restrict_end_date IS NOT NULL
                AND para.restrict_end_date > date($cutoff_date)
                AND para.restrict_end_date <= date($today_date)) AS ended_after_cutoff,
             (coalesce(para.restrict_end_date, date('9999-12-31')) >= date($cutoff_date)) AS visible_after_cutoff
        WHERE para.id IS NOT NULL
          AND visible_after_cutoff
          AND (started_after_cutoff OR ended_after_cutoff)
        RETURN collect(DISTINCT para.id) AS altered_ids
        """

        altered_rows = analysis.run_query(
            altered_nodes_query,
            {
                "uri_contains": uri_contains,
                "cutoff_date": cutoff_date,
                "today_date": today_date,
            },
        )
        altered_ids = set(
            str(x)
            for x in (altered_rows[0].get("altered_ids", []) if altered_rows else [])
            if x is not None
        )

        left_col, right_col = st.columns(2)

        with left_col:
            st.markdown(f"**At cutoff: {cutoff_date}**")
            _render_use_case_graph(
                analysis,
                left_query,
                params={
                    "uri_contains": uri_contains,
                    "cutoff_date": cutoff_date,
                },
                height=NETWORK_GRAPH_HEIGHT,
            )

        with right_col:
            st.markdown(f"**After cutoff: {cutoff_date} → {today_date}**")
            _render_use_case_graph(
                analysis,
                right_query,
                params={
                    "uri_contains": uri_contains,
                    "cutoff_date": cutoff_date,
                },
                height=NETWORK_GRAPH_HEIGHT,
                enlarged_node_ids=altered_ids,
                enlarged_node_size=50,
            )
    elif selected_use_case == "Temporal Diff (As-Of vs As-Of)":
        cutoff_date = use_case_params.get("cutoff_date", "2018-01-01")
        today_date = date.today().isoformat()

        query = """
        MATCH (l:Legislation)-[:HAS_PART]->(part:Part)-[:HAS_CHAPTER]->(:Chapter)-[:HAS_SECTION]->(section:Section)-[:HAS_PARAGRAPH]->(para:Paragraph)
        WHERE l.uri CONTAINS $uri_contains
        WITH l, part, section, para,
             (coalesce(para.restrict_start_date, date('0001-01-01')) <= date($from_date)
                AND (para.restrict_end_date IS NULL OR para.restrict_end_date >= date($from_date))) AS active_from,
             (coalesce(para.restrict_start_date, date('0001-01-01')) <= date($to_date)
                AND (para.restrict_end_date IS NULL OR para.restrict_end_date >= date($to_date))) AS active_to,
             (para.restrict_start_date IS NOT NULL
                AND para.restrict_start_date > date($from_date)
                AND para.restrict_start_date <= date($to_date)) AS starts_between,
             (para.restrict_end_date IS NOT NULL
                AND para.restrict_end_date > date($from_date)
                AND para.restrict_end_date <= date($to_date)) AS ends_between
        WITH l, part, section, para, active_from, active_to, starts_between, ends_between,
             CASE
                 WHEN (NOT active_from) AND active_to THEN 'Added'
                 WHEN active_from AND (NOT active_to) THEN 'Removed'
                 WHEN starts_between AND ends_between THEN 'Restricted'
                ELSE NULL
             END AS change_type
        WHERE change_type IS NOT NULL
        RETURN change_type,
               l.title AS legislation_title,
               l.uri AS legislation_uri,
               part.number AS part_number,
               section.number AS section_number,
               section.title AS section_title,
               para.number AS paragraph_number,
               para.uri AS paragraph_uri,
               para.status AS paragraph_status,
               para.restrict_start_date AS restrict_start_date,
               para.restrict_end_date AS restrict_end_date
        ORDER BY change_type, part_number, section_number, paragraph_number
        LIMIT 500
        """

        rows_df = analysis.run_query_df(
            query,
            {
                "uri_contains": use_case_params.get("uri_contains", "ukpga/2010/4"),
                "from_date": cutoff_date,
                "to_date": today_date,
            },
        )

        if rows_df.empty:
            st.info("No temporal differences found for the selected legislation and date range.")
            return

        added_count = int((rows_df["change_type"] == "Added").sum())
        removed_count = int((rows_df["change_type"] == "Removed").sum())
        restricted_count = int((rows_df["change_type"] == "Restricted").sum())

        c1, c2, c3 = st.columns(3)
        c1.metric("Added", added_count)
        c2.metric("Removed", removed_count)
        c3.metric("Restricted", restricted_count)

        st.markdown("#### Amendment timeline")
        added_events = (
            rows_df.loc[
                (rows_df["change_type"] == "Added") & rows_df["restrict_start_date"].notna(),
                ["restrict_start_date"],
            ]
            .rename(columns={"restrict_start_date": "amendment_date"})
            .assign(change_type="Added")
        )
        removed_events = (
            rows_df.loc[
                (rows_df["change_type"] == "Removed") & rows_df["restrict_end_date"].notna(),
                ["restrict_end_date"],
            ]
            .rename(columns={"restrict_end_date": "amendment_date"})
            .assign(change_type="Removed")
        )
        restricted_start_events = (
            rows_df.loc[
                (rows_df["change_type"] == "Restricted") & rows_df["restrict_start_date"].notna(),
                ["restrict_start_date"],
            ]
            .rename(columns={"restrict_start_date": "amendment_date"})
            .assign(change_type="Restricted (start)")
        )
        restricted_end_events = (
            rows_df.loc[
                (rows_df["change_type"] == "Restricted") & rows_df["restrict_end_date"].notna(),
                ["restrict_end_date"],
            ]
            .rename(columns={"restrict_end_date": "amendment_date"})
            .assign(change_type="Restricted (end)")
        )

        timeline_df = pd.concat(
            [added_events, removed_events, restricted_start_events, restricted_end_events],
            ignore_index=True,
        )

        if timeline_df.empty:
            st.info("No amendment date events found in the table rows.")
        else:
            timeline_df["amendment_date"] = pd.to_datetime(
                timeline_df["amendment_date"].astype(str), errors="coerce"
            )
            timeline_df = timeline_df.dropna(subset=["amendment_date"])

            if timeline_df.empty:
                st.info("No valid amendment dates available to plot.")
            else:
                timeline_df["amendment_year"] = timeline_df["amendment_date"].dt.to_period("Y").dt.to_timestamp()

                timeline_df = (
                    timeline_df.groupby(["amendment_year", "change_type"], as_index=False)
                    .size()
                    .rename(columns={"size": "amendments"})
                    .sort_values(["amendment_year", "change_type"])
                )

                timeline_chart = (
                    alt.Chart(timeline_df)
                    .mark_bar()
                    .encode(
                        x=alt.X("amendment_year:T", title="Amendment year", axis=alt.Axis(format="%Y")),
                        y=alt.Y("sum(amendments):Q", title="Number of amendments"),
                        color=alt.Color("change_type:N", title="Change type"),
                        tooltip=[
                            alt.Tooltip("year(amendment_year):T", title="Year"),
                            alt.Tooltip("change_type:N", title="Type"),
                            alt.Tooltip("sum(amendments):Q", title="Amendments"),
                        ],
                    )
                    .properties(height=320)
                )

                st.altair_chart(timeline_chart, width="stretch")

        st.markdown("#### Temporal diff rows")
        st.dataframe(rows_df, width="stretch", hide_index=True)


def _render_global_metrics(analysis: Neo4jAnalysis):
    query = """
    CALL() {
        MATCH (l:Legislation)
        RETURN count(l) AS legislation_acts,
               min(l.enactment_date) AS min_enactment_date,
               max(l.enactment_date) AS max_enactment_date
    }
    CALL() {
        MATCH (:Paragraph)
        RETURN count(*) AS paragraphs
    }
    CALL() {
        MATCH (:Citation)
        RETURN count(*) AS citations
    }
    RETURN legislation_acts,
           paragraphs,
           citations,
           CASE
               WHEN min_enactment_date IS NULL THEN 'N/A'
               ELSE substring(toString(min_enactment_date), 0, 4)
           END AS earliest_legislation_year,
           CASE
               WHEN max_enactment_date IS NULL THEN 'N/A'
               ELSE substring(toString(max_enactment_date), 0, 4)
           END AS latest_legislation_year
    """

    try:
        metrics_df = analysis.run_query_df(query)
        metrics = metrics_df.iloc[0].to_dict() if not metrics_df.empty else {}

        yearly_df = analysis.run_query_df(
            """
            MATCH (l:Legislation)
            WHERE l.enactment_date IS NOT NULL
            WITH substring(toString(l.enactment_date), 0, 4) AS enactment_year
            RETURN enactment_year, count(*) AS legislations
            ORDER BY enactment_year
            """
        )
    except Exception as exc:
        st.warning(f"Unable to load dashboard metrics: {exc}")
        return

    sparkline_values = (
        yearly_df["legislations"].fillna(0).astype(int).tolist() if not yearly_df.empty else []
    )

    earliest_year = str(metrics.get("earliest_legislation_year", "N/A"))
    latest_year = str(metrics.get("latest_legislation_year", "N/A"))
    if earliest_year == "N/A" and latest_year == "N/A":
        year_range = "N/A"
    elif earliest_year == latest_year:
        year_range = earliest_year
    else:
        year_range = f"{earliest_year} - {latest_year}"

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Legislation acts", f"{int(metrics.get('legislation_acts', 0)):,}", border=True)
    c2.metric("Paragraphs", f"{int(metrics.get('paragraphs', 0)):,}", border=True)
    c3.metric("Citations", f"{int(metrics.get('citations', 0)):,}", border=True)
    c4.metric("Legislation year range", year_range, border=True)

    with c5:
        with st.container(border=True):
            st.caption("Legislations / Year")
            if sparkline_values:
                sparkline_df = yearly_df.copy()
                sparkline_df["enactment_year"] = pd.to_datetime(
                    sparkline_df["enactment_year"].astype(str) + "-01-01", errors="coerce"
                )
                sparkline_df = sparkline_df.dropna(subset=["enactment_year"])

                sparkline_chart = (
                    alt.Chart(sparkline_df)
                    .mark_line(strokeWidth=2)
                    .encode(
                        x=alt.X("enactment_year:T", axis=None),
                        y=alt.Y("legislations:Q", axis=None),
                        tooltip=[
                            alt.Tooltip("year(enactment_year):T", title="Year"),
                            alt.Tooltip("legislations:Q", title="Legislations"),
                        ],
                    )
                    .properties(height=70)
                )
                st.altair_chart(sparkline_chart, width="stretch")
            else:
                st.caption("No data")

    st.markdown("---")

try:
    analysis, agent_executor = build_runtime()
    if not analysis.verify_connection():
        st.error("Neo4j connection test failed.")
        st.stop()
except Exception as e:
    st.error(f"Initialization failed: {e}")
    st.stop()

_render_global_metrics(analysis)
_show_use_case_panel(analysis, selected_use_case, use_case_params)

if selected_use_case == "Chat Interface":
    if "messages" not in st.session_state:
        st.session_state.messages = [
            {
                "role": "assistant",
                "content": "Ask me about UK legislation structure, citations, superseded chains, topical networks, or point-in-time context, specifically for tax themes.",
            }
        ]

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message.get("tool_events"):
                with st.expander("Tool trace", expanded=False):
                    st.json(message["tool_events"])

    prompt = st.chat_input("Ask a legal graph question...", key="main_chat_input")

    if prompt:
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.expander("Tool trace (live)", expanded=False):
                live_trace_placeholder = st.empty()

            def _update_live_trace(events: list[dict]):
                live_trace_placeholder.json(events)

            with st.status("Running agent...", expanded=False):
                answer, tool_events = stream_agent_answer(
                    agent_executor,
                    st.session_state.messages,
                    on_tool_event=_update_live_trace,
                )

            if not answer:
                answer = "No response generated. Try a more specific prompt (Act title, URI, or topic)."

            st.markdown(answer)

        st.session_state.messages.append(
            {
                "role": "assistant",
                "content": answer,
                "tool_events": tool_events,
            }
        )