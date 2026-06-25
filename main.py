"""复读机插件——主模块。"""

from __future__ import annotations

import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .content import extract_content
from .state import create_group_state, clean_expired_echoes


class RepeaterPlugin(Star):
    """
    复读机插件。

    当群聊中不同的群友在一段连续的消息中发送重复相同的内容时，
    重复次数 >= 阈值（默认 3）且来自至少阈值个不同群友时，机器人也跟着发一次完全相同的消息。
    相同内容在冷却时间内不会重复复读，避免死循环。
    同时支持文本和图片的复读检测（图片使用 MD5 哈希进行比较）。
    """

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config

        threshold = config.get("repeat_threshold", 3)
        if not isinstance(threshold, int) or threshold < 2:
            threshold = 3
        self.repeat_threshold = threshold

        cooldown = config.get("cooldown_seconds", 300)
        if not isinstance(cooldown, int) or cooldown < 0:
            cooldown = 300
        self.cooldown_seconds = cooldown

        self._group_state: dict[str, dict] = {}

        logger.info(f"复读机插件已初始化（阈值={self.repeat_threshold}, 冷却={self.cooldown_seconds}秒）")

    def _get_group_state(self, group_id: str) -> dict:
        """获取或创建群聊状态。"""
        if group_id not in self._group_state:
            self._group_state[group_id] = create_group_state()
        return self._group_state[group_id]

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent, *args, **kwargs):
        """
        处理群聊消息，检测是否触发复读。

        触发条件：
        1. 连续 N 条消息内容完全相同（N >= repeat_threshold）
        2. 这些消息来自至少 repeat_threshold 个不同的发送者
        """
        content_signature, segments = await extract_content(event)

        if not content_signature.strip("||"):
            return

        group_id = event.get_group_id()
        sender_id = event.get_sender_id()

        if not group_id or not sender_id:
            return

        if getattr(event.message_obj, "self_id", None) == sender_id:
            return

        state = self._get_group_state(group_id)
        clean_expired_echoes(state, self.cooldown_seconds)

        current_sig = state["current_signature"]
        unique_senders: set = state["unique_senders"]

        if content_signature == current_sig:
            state["message_count"] += 1
            unique_senders.add(sender_id)
            state["last_segments"] = segments

            new_count = state["message_count"]
            unique_count = len(unique_senders)

            log_preview = content_signature[:50].replace("\n", " ")
            logger.debug(
                f"[复读机] 群 {group_id}: 相同消息 \"{log_preview}\" "
                f"第 {new_count} 条 (不同发送者={unique_count}, 阈值={self.repeat_threshold})"
            )

            if (new_count >= self.repeat_threshold
                    and unique_count >= self.repeat_threshold
                    and content_signature not in state["echoed"]):
                state["echoed"][content_signature] = time.time()
                logger.info(
                    f"[复读机] 群 {group_id}: 触发复读! 签名=\"{log_preview}\", "
                    f"消息数={new_count}, 不同发送者={unique_count}"
                )

                event.should_call_llm(False)
                yield event.chain_result(segments)
                event.stop_event()
                return

        else:
            state["current_signature"] = content_signature
            state["message_count"] = 1
            state["unique_senders"] = {sender_id}
            state["last_segments"] = segments

    async def terminate(self) -> None:
        """插件卸载时调用。"""
        self._group_state.clear()
        logger.info("复读机插件已终止")
