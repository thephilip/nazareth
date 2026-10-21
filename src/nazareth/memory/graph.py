"""Graphiti temporal knowledge graph wrapper.

Handles decision nodes, constraint nodes, supersession edges, and hybrid
queries against FalkorDB. Configures LLM and embedder to use the LLM gateway.
"""

from __future__ import annotations

import os

from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDBDriver
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_client import OpenAIClient


def _get_gateway_key() -> str:
    key = os.environ.get("LLM_GATEWAY_KEY", "")
    if not key:
        import subprocess
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-a", os.environ.get("USER", ""), "-s", "llm-gateway", "-w"],
                capture_output=True, text=True, timeout=5,
            )
            key = result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    return key


def _build_llm_client() -> OpenAIClient:
    api_key = _get_gateway_key()
    base_url = os.environ.get("LLM_GATEWAY_BASE_URL", "https://llm-gateway.example.com/v1")
    model = os.environ.get("LLM_GATEWAY_LLM_MODEL", "glm-5.3")
    config = LLMConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        small_model=model,
    )
    return OpenAIClient(config=config)


def _build_embedder() -> OpenAIEmbedder:
    api_key = _get_gateway_key()
    base_url = os.environ.get("LLM_GATEWAY_BASE_URL", "https://llm-gateway.example.com/v1")
    embedding_model = os.environ.get("LLM_GATEWAY_EMBEDDING_MODEL", "text-embedding-3-small")
    config = OpenAIEmbedderConfig(
        api_key=api_key,
        base_url=base_url,
        embedding_model=embedding_model,
    )
    return OpenAIEmbedder(config=config)


async def create_graph(
    host: str = "localhost",
    port: int = 6379,
    graph_name: str = "nazareth",
) -> Graphiti:
    driver = FalkorDBDriver(
        host=host,
        port=port,
        graph_name=graph_name,
    )
    llm_client = _build_llm_client()
    embedder = _build_embedder()
    graphiti = Graphiti(
        graph_driver=driver,
        llm_client=llm_client,
        embedder=embedder,
    )
    await graphiti.build_indices()
    return graphiti
