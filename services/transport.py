"""Native SDK sends and SnowLuma 1.1.9 permission APIs; never open another QQ connection."""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from ..log import logger


class DeliveryUncertain(RuntimeError):
    """Host returned no confirmed platform receipt; do not automatically resend."""


def text_segment(text: str) -> dict:
    return {"type": "text", "content": text}


def at_segment(user_id: str) -> dict:
    return {"type": "at", "data": {"target_user_id": str(user_id)}}


class Transport:
    def __init__(self, ctx):
        self.ctx = ctx

    async def streams(self) -> list[dict]:
        result = await self.ctx.chat.get_all_streams(platform="qq")
        if not isinstance(result, list):
            raise RuntimeError("宿主未能返回聊天流列表")
        return result

    async def session(self, stream_id: str) -> dict:
        for stream in await self.streams():
            if str(stream.get("stream_id") or stream.get("session_id")) == stream_id:
                return stream
        raise ValueError("聊天流不存在，请先让机器人收到目标群或私聊的消息，再使用 /bili_sid 查看")

    async def api(self, name: str, **params):
        return await self.ctx.api.call(f"adapter.snowluma.{name}", version="1", **params)

    async def group_role(self, group_id: str, user_id: str) -> str:
        try:
            result = await self.api("group.get_group_member_info", group_id=int(group_id), user_id=int(user_id))
            if isinstance(result, dict):
                result = result.get("data", result)
                return str(result.get("role") or "")
        except Exception:
            logger.warning("无法查询群角色；受保护的操作保持拒绝")
        return ""

    async def can_at_all(self, stream_id: str) -> bool:
        try:
            stream = await self.session(stream_id)
            group_id = str(stream.get("group_id") or "")
            bot_id = str(stream.get("account_id") or "")
            if not group_id or not bot_id or await self.group_role(group_id, bot_id) not in {"admin", "owner"}:
                return False
            result = await self.api("group.get_group_at_all_remain", group_id=int(group_id))
            if not isinstance(result, dict):
                return False
            result = result.get("data", result)
            remaining = result.get("remain_at_all_count_for_self", result.get("remain_at_all_count_for_uin", 0))
            return bool(result.get("can_at_all")) and int(remaining or 0) > 0 and int(result.get("remain_at_all_count_for_group") or 0) > 0
        except Exception:
            logger.warning("无法确认 @全体权限或剩余次数，使用普通推送")
            return False

    @staticmethod
    def require_receipt(result):
        sent = result.get("sent", False) if isinstance(result, dict) else result is True
        if not sent:
            raise DeliveryUncertain("未取得平台投递确认，请检查群内消息后用 /bili_pending 处理")

    async def send(self, stream_id: str, segments: list[dict], forward: bool = False, mentions: list[dict] | None = None, sync_history=False):
        mentions = mentions or []
        if forward:
            # QQ 合并转发节点中的 @ 不会产生群提醒，必须发送在外部。
            if mentions:
                self.require_receipt(await self.ctx.send.hybrid(mentions, stream_id, return_details=True))
            stream = await self.session(stream_id)
            result = await self.ctx.send.forward(
                [{"user_id": str(stream.get("account_id") or "0"), "nickname": "Bilibili 动态", "segments": segments}],
                stream_id, return_details=True, sync_to_maisaka_history=sync_history,
            )
        else:
            result = await self.ctx.send.hybrid(mentions + segments, stream_id, return_details=True, sync_to_maisaka_history=sync_history)
        self.require_receipt(result)

    async def send_file(self, stream_id: str, path: str, name: str):
        # Base64 crosses process/machine boundaries; local paths need not be shared with SnowLuma.
        encoded = await asyncio.to_thread(lambda: base64.b64encode(Path(path).read_bytes()).decode("ascii"))
        stream = await self.session(stream_id)
        group_id = str(stream.get("group_id") or "")
        action = "upload_group_file" if group_id else "upload_private_file"
        params = {"file": f"base64://{encoded}", "name": name}
        params["group_id" if group_id else "user_id"] = int(group_id or stream["user_id"])
        result = await self.api("action.call", action_name=action, params=params)
        if not isinstance(result, dict) or result.get("retcode") != 0 or result.get("status", "ok") != "ok":
            raise DeliveryUncertain("文件上传未确认，请检查目标会话")
