"""Compatibility exports for the local-only AI adapter."""
from local_ai import generate_embedding, query_local_model, query_medgemma

__all__ = ["generate_embedding", "query_local_model", "query_medgemma"]
