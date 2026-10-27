"""MiniMax 引擎入口。"""

from .engine import (
    DEFAULT_MUSIC_MODEL,
    MINIMAX_EMOTIONS,
    MINIMAX_FORMATS,
    MINIMAX_VOICES,
    MiniMaxEngine,
)

__all__ = [
    "MiniMaxEngine",
    "MINIMAX_VOICES",
    "MINIMAX_EMOTIONS",
    "MINIMAX_FORMATS",
    "DEFAULT_MUSIC_MODEL",
]
