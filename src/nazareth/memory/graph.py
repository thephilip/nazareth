"""Graphiti temporal knowledge graph wrapper.

Handles decision nodes, constraint nodes, supersession edges, and hybrid
queries against FalkorDB.
"""

from __future__ import annotations

from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDBDriver


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
    graphiti = Graphiti(driver=driver)
    await graphiti.build_indices()
    return graphiti
