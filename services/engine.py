from __future__ import annotations

import asyncio
import base64
import copy
import html
import time
from pathlib import Path

from ..core.constant import BANNER_PATH
from ..core.models import RenderPayload
from ..core.utils import create_qrcode, image_to_base64, is_height_valid, render_text_to_plain
from ..log import logger
from .transport import at_segment, text_segment

ACTIONS = {
    "DYNAMIC_TYPE_AV": "投稿了新视频", "DYNAMIC_TYPE_ARTICLE": "发布了新专栏",
    "DYNAMIC_TYPE_DRAW": "发布了新图文", "DYNAMIC_TYPE_WORD": "发布了新动态",
    "DYNAMIC_TYPE_FORWARD": "转发了新动态",
}


class SubscriptionEngine:
    def __init__(self, plugin):
        self.plugin = plugin
        self.store = plugin.store
        self.parser = plugin.parser
        self.transport = plugin.transport
        self.client = plugin.client
        self.lock = asyncio.Lock()
        self.silent_until = 0.0
        self.summary_cache = {}

    @property
    def config(self):
        return self.plugin.config

    async def save_runtime(self, key: str, value):
        async with self.store._write_lock:
            self.store.data[key] = value
            await self.store.save()

    def pending(self) -> dict:
        return copy.deepcopy(self.store.data.get("pending_deliveries", {}))

    @staticmethod
    def key(stream_id: str, uid: int) -> str:
        return f"{stream_id}/{uid}"

    async def mark_pending(self, stream_id: str, uid: int, **details):
        pending = self.pending()
        pending[self.key(stream_id, uid)] = {"stream_id": stream_id, "uid": uid, "time": int(time.time()), **details}
        await self.save_runtime("pending_deliveries", pending)

    async def resolve_pending(self, stream_id: str, uid: int, action: str):
        if action not in {"ack", "retry"}:
            raise ValueError("处理方式只能是 ack（已收到）或 retry（确认未收到，允许下轮重试）")
        key = self.key(stream_id, uid)
        pending = self.pending()
        record = pending.get(key)
        if not record:
            raise ValueError("没有该订阅的待确认投递")
        if action == "ack":
            await self.commit_delivery(record)
        pending.pop(key)
        await self.save_runtime("pending_deliveries", pending)

    async def commit_delivery(self, delivery: dict):
        stream_id, uid = delivery["stream_id"], int(delivery["uid"])
        if delivery["category"] == "dynamic":
            await self.store.update_last_dynamic_id(stream_id, uid, delivery["dyn_id"])
        else:
            sub = self.store.get_subscription(stream_id, uid)
            if sub:
                sub.last_live_start_ts = int(delivery.get("live_start", 0))
                await self.store.update_live_status(stream_id, uid, bool(delivery["is_live"]))

    async def initialize(self, stream_id: str, uid: int):
        """A failed initial request must not turn old history into new notifications later."""
        sub = self.store.get_subscription(stream_id, uid)
        if not sub:
            return False
        dyn = await self.client.get_latest_dynamics(uid)
        if not dyn or "items" not in dyn:
            return False
        for item in reversed(dyn.get("items") or []):
            if item.get("id_str"):
                await self.store.update_last_dynamic_id(stream_id, uid, str(item["id_str"]))
        baselines = dict(self.store.data.get("baselines", {}))
        baselines[self.key(stream_id, uid)] = True
        await self.save_runtime("baselines", baselines)
        return True

    async def start(self):
        due = {}
        gap_until = 0.0
        last = self.store.get_last_success_sub_notify_ts()
        if self.config.push.reconnect_silent and last and time.time() - last > 21600:
            uid_count = len({sub.uid for subs in self.store.get_all_subscriptions().values() for sub in subs})
            self.silent_until = time.monotonic() + self.config.push.interval_secs + self.config.push.task_gap_secs * uid_count + 60
        while True:
            try:
                targets = {}
                for stream_id, records in self.store.get_all_subscriptions().items():
                    for sub in list(records):
                        targets.setdefault(sub.uid, []).append(stream_id)
                due = {uid: due.get(uid, 0) for uid in targets}
                now = time.monotonic()
                ready = [uid for uid in targets if due[uid] <= now]
                if not ready or now < gap_until:
                    await asyncio.sleep(1)
                    continue
                uid = min(ready, key=lambda item: (due[item], item))
                # UID-level requests are shared across all subscribed chats.
                if self.client.credential is None:
                    await asyncio.sleep(5)
                    continue
                dyn = await self.client.get_latest_dynamics(uid)
                live = None
                if any("live" not in (self.store.get_subscription(s, uid).filter_types if self.store.get_subscription(s, uid) else ["live"]) for s in targets[uid]):
                    try:
                        live = await self.client.get_live_info_by_uids([uid])
                    except Exception as exc:
                        logger.warning("直播查询失败 uid=%s: %s", uid, type(exc).__name__)
                for stream_id in targets[uid]:
                    try:
                        async with self.lock:
                            await self.check(stream_id, uid, dyn, live)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("处理订阅失败 stream=%s uid=%s", stream_id, uid)
                finished = time.monotonic()
                due[uid] = finished + self.config.push.interval_secs
                gap_until = finished + self.config.push.task_gap_secs
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("B站轮询异常")
                await asyncio.sleep(5)

    async def check(self, stream_id: str, uid: int, dyn, live):
        sub = self.store.get_subscription(stream_id, uid)
        if not sub or self.key(stream_id, uid) in self.pending():
            return
        if not self.store.data.get("baselines", {}).get(self.key(stream_id, uid)):
            if not dyn or "items" not in dyn:
                return
            for item in reversed(dyn.get("items") or []):
                if item.get("id_str"):
                    await self.store.update_last_dynamic_id(stream_id, uid, str(item["id_str"]))
            baselines = dict(self.store.data.get("baselines", {}))
            baselines[self.key(stream_id, uid)] = True
            await self.save_runtime("baselines", baselines)
            return
        silent = time.monotonic() < self.silent_until
        if dyn:
            parsed = self.parser._parse_and_filter_dynamics(dyn, sub)
            sent = 0
            # Oldest first: a failed or uncertain send cannot skip a preceding update.
            for result in reversed(parsed):
                if not result.dyn_id:
                    continue
                if not result.has_payload() or silent:
                    await self.store.update_last_dynamic_id(stream_id, uid, result.dyn_id)
                    continue
                if sent >= self.config.push.dynamic_limit:
                    break
                if not await self.deliver(stream_id, sub, result.payload, "dynamic", dyn_id=result.dyn_id):
                    return
                sent += 1
        if live and "live" not in sub.filter_types:
            now_live, started, ended = self.parser._evaluate_live_transition(sub, live)
            live_start = self.parser._parse_live_start_timestamp(live) or sub.last_live_start_ts
            if now_live and not started and live_start != sub.last_live_start_ts:
                sub.last_live_start_ts = live_start
                await self.store.update_live_status(stream_id, uid, True)
            if not (started or ended):
                return
            if silent or (ended and "live_end" in sub.filter_types):
                sub.last_live_start_ts = live_start if now_live else 0
                await self.store.update_live_status(stream_id, uid, now_live)
                return
            name = html.escape(str(live.get("uname") or uid))
            url = f"https://live.bilibili.com/{int(live.get('room_id') or 0)}"
            text = f"📣 你订阅的 UP「{name}」{'开播了' if started else '下播了'}！"
            if ended and live_start:
                duration = self.parser._format_live_duration_text(max(0, int(time.time()) - live_start))
                text += f"<br>本场直播时长：{duration}"
            payload = RenderPayload(name="MaiBot", avatar=image_to_base64(self.plugin.logo.get_active_path()), banner=image_to_base64(BANNER_PATH),
                                    title=str(live.get("title") or ""), url=url, qrcode=create_qrcode(url), text=text,
                                    image_urls=[str(live.get("cover_from_user") or "")], uid=str(uid))
            await self.deliver(stream_id, sub, payload, "live", is_live=now_live, live_start=live_start if now_live else 0, offline=ended)

    def plain_text(self, payload, nested=False):
        body = payload.summary or render_text_to_plain(payload.text)
        values = {"name": payload.name, "uid": payload.uid, "action": ACTIONS.get(payload.type, "发布了新动态"),
                  "title": payload.title, "text": body, "url": payload.url}
        template = self.config.push.plain_push_forward_template if nested else self.config.push.plain_push_template
        if template:
            try:
                return "\n".join(line for line in template.format_map(values).splitlines() if line.strip())
            except (KeyError, ValueError):
                logger.warning("文字模板格式错误，使用内置格式")
        return "\n".join(str(v) for v in [f"{payload.name} {values['action']}", payload.title, body, payload.url] if v)

    async def plain_segments(self, payload):
        segments = [text_segment(self.plain_text(payload))]
        segments += [{"type": "imageurl", "data": url} for url in payload.image_urls if url]
        if getattr(payload, "forward", None):
            forward = payload.forward
            segments.append(text_segment("转发原文：\n" + self.plain_text(forward, nested=True)))
            segments += [{"type": "imageurl", "data": url} for url in forward.image_urls if url]
        return segments

    async def mentions(self, stream_id: str, sub, category: str, offline=False):
        if offline:
            return []
        stream = await self.transport.session(stream_id)
        if not stream.get("group_id"):
            return []
        if (sub.at_all or (category == "live" and sub.live_atall)) and await self.transport.can_at_all(stream_id):
            return [at_segment("all"), text_segment(" ")]
        return [segment for user in sub.at_sub_users for segment in (at_segment(user), text_segment(" "))]

    async def send_payload(self, stream_id: str, payload: RenderPayload, sub=None, category="dynamic", offline=False):
        path = None
        try:
            if self.config.push.render_as_image:
                path = await self.plugin.renderer.render_dynamic(payload)
            mentions = await self.mentions(stream_id, sub, category, offline) if sub else []
            if path and not await asyncio.to_thread(is_height_valid, path):
                if mentions:
                    await self.transport.send(stream_id, mentions)
                await self.transport.send_file(stream_id, path, f"bilibili-{int(time.time())}.png")
                return
            if path:
                encoded = await asyncio.to_thread(lambda: base64.b64encode(Path(path).read_bytes()).decode("ascii"))
                segments = [{"type": "image", "content": encoded}]
                if payload.url:
                    segments.append(text_segment(payload.url))
            else:
                segments = await self.plain_segments(payload)
            await self.transport.send(stream_id, segments, forward=self.config.push.forward, mentions=mentions, sync_history=True)
        finally:
            if path:
                Path(path).unlink(missing_ok=True)

    async def deliver(self, stream_id: str, sub, payload, category: str, offline=False, **state):
        # Persist before issuing platform sends so a process crash also stays pending.
        await self.mark_pending(stream_id, sub.uid, category=category, **state)
        try:
            await self.send_payload(stream_id, payload, sub, category, offline)
        except Exception as exc:
            logger.warning("订阅投递暂停，需人工确认 stream=%s uid=%s error=%s", stream_id, sub.uid, type(exc).__name__)
            return False
        await self.resolve_pending(stream_id, sub.uid, "ack")
        await self.store.set_last_success_sub_notify_ts(int(time.time()))
        if category == "dynamic" and self.config.ai.enable_summary:
            try:
                await self.summary(stream_id, payload, str(state.get("dyn_id") or ""))
            except Exception:
                logger.exception("AI摘要失败，动态本体已成功推送")
        return True

    async def summary(self, stream_id: str, payload, dyn_id: str):
        cache_key = (dyn_id, self.config.ai.model, self.config.ai.summary_prompt, self.config.ai.include_images)
        text = self.summary_cache.get(cache_key)
        if not text:
            content = self.plain_text(payload)
            if payload.forward:
                content += "\n转发原文：\n" + self.plain_text(payload.forward, nested=True)
            prompt = (self.config.ai.summary_prompt or "请简洁总结以下 B站动态，先用一句话概括，再列2至4条要点。不要编造未提供的信息。\n{content}").replace("{content}", content)
            if self.config.ai.include_images:
                parts = [{"type": "text", "text": prompt}]
                urls = [url.replace("http://", "https://", 1) for url in payload.image_urls[:8]]
                parts += [{"type": "image_url", "image_url": {"url": url}} for url in urls if url.startswith("https://")]
                prompt = [{"role": "user", "content": parts}]
            result = await self.plugin.ctx.llm.generate(prompt, model=self.config.ai.model, max_tokens=1000)
            if not result.get("success"):
                raise RuntimeError("摘要模型调用失败")
            text = str(result.get("response") or "").strip()
            if not text:
                return
            self.summary_cache[cache_key] = text
            while len(self.summary_cache) > 64:
                self.summary_cache.pop(next(iter(self.summary_cache)))
        await self.transport.send(stream_id, [text_segment("动态摘要：\n" + text)], sync_history=True)
