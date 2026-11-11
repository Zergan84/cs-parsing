"""
profile_analyzer.py - Канонический фасад анализатора профилей, делегирующий все операции в `core.collector`.
"""

from core.collector import ProfileAnalyzer, get_profile_analyzer

__all__ = ["ProfileAnalyzer", "get_profile_analyzer"]
