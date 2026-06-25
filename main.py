"""复读机插件——主模块。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Optional

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Image
from astrbot.api.star import Context, Star


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

        # 读取阈值配置
        threshold = config.get("repeat_threshold", 3)
        if not isinstance(threshold, int) or threshold < 2:
            threshold = 3
        self.repeat_threshold = threshold

        # 读取冷却时间配置
        cooldown = config.get("cooldown_seconds", 300)
        if not isinstance(cooldown, int) or cooldown < 0:
            cooldown = 300
        self.cooldown_seconds = cooldown

        # 每个群聊的状态：
        # group_id -> {
        #   "current_signature": str,       # 当前连续消息的内容签名
        #   "message_count": int,           # 当前连续相同消息的总条数
        #   "unique_senders": set[str],     # 当前连续消息中不重复的发送者集合
        #   "last_segments": list,          # 最后一条消息的消息段（用于复读时重建）
        #   "echoed": dict[str, float]      # 已复读的内容签名 -> 复读时间戳
        # }
        self._group_state: dict[str, dict] = {}

        logger.info(f"复读机插件已初始化（阈值={self.repeat_threshold}, 冷却={self.cooldown_seconds}秒）")

    def _get_group_state(self, group_id: str) -> dict:
        """获取或创建群聊状态。"""
        if group_id not in self._group_state:
            self._group_state[group_id] = {
                "current_signature": None,
                "message_count": 0,
                "unique_senders": set(),
                "last_segments": [],
                "echoed": {},  # signature -> timestamp
            }
        return self._group_state[group_id]

    def _clean_expired_echoes(self, group_state: dict) -> None:
        """清理过期的复读冷却记录。"""
        now = time.time()
        expired_keys = [
            k for k, t in group_state["echoed"].items()
            if now - t > self.cooldown_seconds
        ]
        for k in expired_keys:
            del group_state["echoed"][k]

    async def _compute_image_md5(self, url: str) -> Optional[str]:
        """
        下载图片并计算其 MD5 哈希值。

        Args:
            url: 图片的 URL 地址

        Returns:
            图片的 MD5 十六进制字符串；下载失败返回 None
        """
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(f"[复读机] 下载图片失败: HTTP {resp.status}, url={url[:80]}")
                        return None
                    # 限制最大 10MB，防止恶意超大文件
                    data = await resp.read()
                    if len(data) > 10 * 1024 * 1024:
                        logger.warning(f"[复读机] 图片过大 ({len(data)} bytes)，跳过: url={url[:80]}")
                        return None
                    md5_hash = hashlib.md5(data).hexdigest()
                    return md5_hash
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"[复读机] 下载图片异常: {e}, url={url[:80]}")
            return None
        except Exception as e:
            logger.warning(f"[复读机] 计算图片MD5异常: {e}, url={url[:80]}")
            return None

    async def _extract_content(self, event: AstrMessageEvent) -> tuple[str, list]:
        """
        从消息事件中提取内容签名和消息段。

        图片通过下载后计算 MD5 哈希进行比较，而非使用 URL。
        若图片下载失败，则回退使用 URL 作为签名（保证基本功能可用）。

        Returns:
            (content_signature, message_segments)
            - content_signature: 用于比较的字符串（文本+图片MD5拼接）
            - message_segments: 消息段列表，用于复读时重建消息
        """
        text = event.message_str or ""
        text = text.strip()

        # 从消息对象中提取图片URL
        image_urls: list[str] = []
        segments: list[Any] = []

        message_obj = event.message_obj
        if message_obj and hasattr(message_obj, "message") and message_obj.message:
            for seg in message_obj.message:
                seg_type = getattr(seg, "type", None)
                if seg_type == "image":
                    url = getattr(seg, "url", "") or getattr(seg, "file", "")
                    if url:
                        image_urls.append(url)
                        segments.append(Image(file=url))
                elif seg_type == "text":
                    segments.append(Plain(text=getattr(seg, "text", "")))

        # 构建签名：文本 + 图片MD5（或回退到URL）
        signature_parts = [text]
        for url in image_urls:
            md5 = await self._compute_image_md5(url)
            if md5:
                logger.info(f"[复读机] 图片MD5计算成功: md5={md5}, url={url[:100]}")
                signature_parts.append(f"img_md5:{md5}")
            else:
                logger.info(f"[复读机] 图片MD5计算失败(回退URL): url={url[:100]}")
                # 下载失败时回退使用 URL，保证基本功能
                signature_parts.append(f"img_url:{url}")

        signature = "||".join(signature_parts)

        return signature, segments if segments else [Plain(text=text)]

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent, *args, **kwargs):
        """
        处理群聊消息，检测是否触发复读。

        触发条件：
        1. 连续 N 条消息内容完全相同（N >= repeat_threshold）
        2. 这些消息来自至少 repeat_threshold 个不同的发送者

        Args:
            event: AstrBot 消息事件
        """
        # 提取内容签名和消息段
        content_signature, segments = await self._extract_content(event)

        # 如果没有任何内容（无文字也无图片），跳过
        if not content_signature.strip("||"):
            return

        # 获取群组ID和发送者ID
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()

        if not group_id or not sender_id:
            return

        # 跳过机器人自己发送的消息，防止自身触发死循环
        if getattr(event.message_obj, "self_id", None) == sender_id:
            return

        # 获取群聊状态
        state = self._get_group_state(group_id)
        self._clean_expired_echoes(state)

        current_sig = state["current_signature"]
        unique_senders: set = state["unique_senders"]

        # 检查是否与上一条消息内容相同
        if content_signature == current_sig:
            # 相同内容：消息计数+1，并记录发送者
            state["message_count"] += 1
            unique_senders.add(sender_id)
            # 保存消息段以便复读时使用
            state["last_segments"] = segments

            new_count = state["message_count"]
            unique_count = len(unique_senders)

            log_preview = content_signature[:50].replace("\n", " ")
            logger.debug(
                f"[复读机] 群 {group_id}: 相同消息 \"{log_preview}\" "
                f"第 {new_count} 条 (不同发送者={unique_count}, 阈值={self.repeat_threshold})"
            )

            # 触发条件：消息数 >= 阈值 且 不同发送者数 >= 阈值
            if (new_count >= self.repeat_threshold
                    and unique_count >= self.repeat_threshold
                    and content_signature not in state["echoed"]):
                # 记录复读时间
                state["echoed"][content_signature] = time.time()
                logger.info(
                    f"[复读机] 群 {group_id}: 触发复读! 签名=\"{log_preview}\", "
                    f"消息数={new_count}, 不同发送者={unique_count}"
                )

                # 阻止 LLM 处理，直接复读
                event.should_call_llm(False)

                # 发送复读消息（包含文本和图片）
                yield event.chain_result(segments)
                event.stop_event()
                return

        else:
            # 新内容，重置计数和发送者集合
            state["current_signature"] = content_signature
            state["message_count"] = 1
            state["unique_senders"] = {sender_id}
            state["last_segments"] = segments

    async def terminate(self) -> None:
        """插件卸载时调用。"""
        self._group_state.clear()
        logger.info("复读机插件已终止")
