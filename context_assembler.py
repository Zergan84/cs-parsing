"""
context_assembler.py - Канонический фасад Context Assembler, делегирующий все операции в `core.context`.
"""

from core.context import (
    ANCHOR_TO_FACT_MAPPINGS,
    ContextAssembler,
    get_context_assembler,
)

__all__ = [
    "ANCHOR_TO_FACT_MAPPINGS",
    "ContextAssembler",
    "get_context_assembler",
]
