"""群聊状态管理。"""

from __future__ import annotations

import time


def create_group_state() -> dict:
    """创建新的群聊状态。"""
    return {
        "current_signature": None,
        "message_count": 0,
        "unique_senders": set(),
        "last_segments": [],
        "echoed": {},
    }


def clean_expired_echoes(group_state: dict, cooldown_seconds: int) -> None:
    """清理过期的复读冷却记录。"""
    now = time.time()
    expired_keys = [
        k for k, t in group_state["echoed"].items()
        if now - t > cooldown_seconds
    ]
    for k in expired_keys:
        del group_state["echoed"][k]
