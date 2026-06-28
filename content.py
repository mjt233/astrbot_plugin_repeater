"""消息内容提取与签名。"""

from __future__ import annotations

import asyncio
import hashlib
import os
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


def _detect_image_ext(data: bytes) -> str:
    if len(data) < 12:
        return ""
    if data[:3] == b'GIF':
        return ".gif"
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return ".png"
    if data[:3] == b'\xff\xd8\xff':
        return ".jpg"
    if data[:4] == b'RIFF' and len(data) >= 12 and data[8:12] == b'WEBP':
        return ".webp"
    if data[:2] == b'BM':
        return ".bmp"
    return ""


def _fix_image_path(url: str, data: bytes) -> Optional[str]:
    detected_ext = _detect_image_ext(data)
    if not detected_ext:
        return None

    if not _is_local_path(url):
        return None

    clean = url.replace("file://", "", 1)
    base, ext = os.path.splitext(clean)
    if ext.lower() == detected_ext.lower():
        return None

    new_path = base + detected_ext
    try:
        if os.path.exists(new_path):
            return None
        os.rename(clean, new_path)
        logger.info(f"[复读机] 修正图片扩展名: {ext} -> {detected_ext}, path={new_path[:80]}")
        if url.startswith("file://"):
            return "file://" + new_path
        return new_path
    except OSError as e:
        logger.warning(f"[复读机] 无法重命名图片文件: {e}, path={clean[:80]}")
        return None


async def _compute_image_md5(url: str) -> tuple[Optional[str], Optional[bytes]]:
    """
    下载图片并计算其 MD5 哈希值。

    Args:
        url: 图片的 URL 地址或本地文件路径

    Returns:
        (md5, data) 元组：md5 为十六进制字符串（失败为 None），data 为原始字节（失败为 None）
    """
    try:
        if _is_local_path(url):
            data = await _read_local_file(url)
            if data is None:
                return None, None
        else:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(f"[复读机] 下载图片失败: HTTP {resp.status}, url={url[:80]}")
                        return None, None
                    data = await resp.read()

        if len(data) > 10 * 1024 * 1024:
            logger.warning(f"[复读机] 图片过大 ({len(data)} bytes)，跳过: url={url[:80]}")
            return None, None
        md5_hash = hashlib.md5(data).hexdigest()
        return md5_hash, data
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"[复读机] 下载图片异常: {e}, url={url[:80]}")
        return None, None
    except Exception as e:
        logger.warning(f"[复读机] 计算图片MD5异常: {e}, url={url[:80]}")
        return None, None


async def extract_content(event: AstrMessageEvent) -> tuple[str, list]:
    """
    从消息事件中提取内容签名和消息段。

    图片通过下载后计算 MD5 哈希进行比较，而非使用 URL。
    若图片下载失败，则回退使用 URL 作为签名（保证基本功能可用）。

    修改内容（方案A）：
    - 新增从 raw_message.attachments 获取原始图片 URL 的逻辑
    - GIF 图片使用原始 URL（绕过 ensure_jpeg 的 JPEG 转换）

    Returns:
        (content_signature, message_segments)
        - content_signature: 用于比较的字符串（文本+图片MD5拼接）
        - message_segments: 消息段列表，用于复读时重建消息
    """
    # [新增] 获取未经 PreProcessStage 处理的原始图片 URL
    raw_image_urls = _extract_raw_image_urls(event)

    text = event.message_str or ""
    text = text.strip()

    image_urls: list[str] = []
    image_segments: list[Image] = []
    segments: list[Any] = []

    message_obj = event.message_obj
    if message_obj and hasattr(message_obj, "message") and message_obj.message:
        for seg in message_obj.message:
            seg_type = seg.type if hasattr(seg, "type") else None
            if seg_type == ComponentType.Image:
                url = getattr(seg, "url", "") or getattr(seg, "file", "")
                if url:
                    image_urls.append(url)
                    img_seg = Image(file=url)
                    image_segments.append(img_seg)
                    segments.append(img_seg)
            elif seg_type == ComponentType.Plain:
                segments.append(Plain(text=getattr(seg, "text", "")))

    # 当消息中包含图片时，移除纯 "[表情]" 文本段（QQ 表情的占位文本）
    if image_segments:
        segments = [s for s in segments if not (isinstance(s, Plain) and s.text.strip() == "[表情]")]
        text = text.replace("[表情]", "").strip()

    signature_parts = [text]
    for i, url in enumerate(image_urls):
        # [修改] 优先使用原始 URL（未被 ensure_jpeg 污染）
        # 原始 URL 来自 raw_message.attachments，未被 PreProcessStage 处理过
        original_url = (
            raw_image_urls[i]
            if i < len(raw_image_urls) and raw_image_urls[i]
            else None
        )
        compute_url = original_url if original_url else url

        md5, data = await _compute_image_md5(compute_url)
        if md5:
            logger.info(
                f"[复读机] 图片MD5计算成功: md5={md5}, "
                f"原始URL={'是' if original_url else '否'}, "
                f"url={compute_url[:100]}"
            )
            signature_parts.append(f"img_md5:{md5}")
            if data:
                # [新增] GIF 图片使用原始 URL 构建消息段
                # 检测魔数：GIF 格式的前 3 个字节为 b'GIF'
                if data[:3] == b'GIF' and original_url:
                    # GIF 图片 → 使用原始 CDN URL
                    # 这样下游发送时会从原始 CDN 下载 GIF 数据
                    image_segments[i].file = original_url
                    logger.info(
                        f"[复读机] 检测到GIF动图，"
                        f"使用原始URL复读: {original_url[:100]}"
                    )
                else:
                    # 非 GIF 图片 → 保持原有逻辑
                    fixed_url = _fix_image_path(compute_url, data)
                    if fixed_url:
                        image_segments[i].file = fixed_url
        else:
            logger.info(
                f"[复读机] 图片MD5计算失败(回退URL): "
                f"url={compute_url[:100]}"
            )
            signature_parts.append(f"img_url:{compute_url}")

    signature = "||".join(signature_parts)

    return signature, segments if segments else [Plain(text=text)]


def _extract_raw_image_urls(event: AstrMessageEvent) -> list[str]:
    """
    从原始消息对象中提取未经 PreProcessStage 处理的原始图片 CDN URL。

    平台适配：
    - QQ 官方平台：raw_message.attachments[i].url 是 QQ CDN 原始 URL
    - 其他平台暂返回空列表（不影响现有逻辑）

    Returns:
        按顺序排列的原始图片 URL 列表
    """
    urls: list[str] = []
    raw_message = event.message_obj.raw_message

    if raw_message is None:
        return urls

    # QQ官方机器人API：GroupMessage / C2CMessage 都有 attachments 属性
    # attachments 是消息中的原始附件列表，未被 PreProcessStage 处理过
    # type: ignore 因为 raw_message 类型为 object，但运行时实际有 attachments
    if hasattr(raw_message, "attachments") and raw_message.attachments:  # type: ignore[union-attr]
        for att in raw_message.attachments:  # type: ignore[union-attr]
            url = getattr(att, "url", None) or ""
            if url:
                urls.append(url)

    return urls
