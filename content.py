"""消息内容提取与签名。"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Optional

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import ComponentType, Plain, Image


def _is_local_path(url: str) -> bool:
    return url.startswith("file://") or url.startswith("/") or (len(url) > 1 and url[1] == ":")


async def _read_local_file(path: str) -> Optional[bytes]:
    clean = path.replace("file://", "", 1)
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _read_file_sync, clean)
    except Exception as e:
        logger.warning(f"[复读机] 读取本地图片异常: {e}, path={clean[:80]}")
        return None


def _read_file_sync(path: str) -> Optional[bytes]:
    with open(path, "rb") as f:
        return f.read()


async def _compute_image_md5(url: str) -> Optional[str]:
    """
    下载图片并计算其 MD5 哈希值。

    Args:
        url: 图片的 URL 地址或本地文件路径

    Returns:
        图片的 MD5 十六进制字符串；下载失败返回 None
    """
    try:
        if _is_local_path(url):
            data = await _read_local_file(url)
            if data is None:
                return None
        else:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(f"[复读机] 下载图片失败: HTTP {resp.status}, url={url[:80]}")
                        return None
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


async def extract_content(event: AstrMessageEvent) -> tuple[str, list]:
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

    image_urls: list[str] = []
    segments: list[Any] = []

    message_obj = event.message_obj
    if message_obj and hasattr(message_obj, "message") and message_obj.message:
        for seg in message_obj.message:
            seg_type = seg.type if hasattr(seg, "type") else None
            if seg_type == ComponentType.Image:
                url = getattr(seg, "url", "") or getattr(seg, "file", "")
                if url:
                    image_urls.append(url)
                    segments.append(Image(file=url))
            elif seg_type == ComponentType.Plain:
                segments.append(Plain(text=getattr(seg, "text", "")))

    signature_parts = [text]
    for url in image_urls:
        md5 = await _compute_image_md5(url)
        if md5:
            logger.info(f"[复读机] 图片MD5计算成功: md5={md5}, url={url[:100]}")
            signature_parts.append(f"img_md5:{md5}")
        else:
            logger.info(f"[复读机] 图片MD5计算失败(回退URL): url={url[:100]}")
            signature_parts.append(f"img_url:{url}")

    signature = "||".join(signature_parts)

    return signature, segments if segments else [Plain(text=text)]
