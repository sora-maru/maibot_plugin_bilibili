"""MaiBot 1.3.16 / SDK 2.16 entry point. AstrBot is not a runtime dependency."""
from __future__ import annotations

import asyncio
import copy
import inspect
import html
import os
import re
import shlex
import time
from pathlib import Path

from maibot_sdk import API, Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import HookMode

from .bili_client import BiliClient
from .config import BilibiliConfig
from .core.bili_login import BiliQrLoginClient
from .core.constant import CARD_TEMPLATES, LOGO_PATH, VALID_FILTER_TYPES, VALID_SUB_OPTIONS
from .core.credential_lifecycle import CredentialLifecycle, CredentialRefreshStatus
from .core.data_manager import DataManager
from .core.models import RenderPayload, SubscriptionRecord
from .core.utils import image_to_base64, parse_bilibili_uid
from .log import logger
from .services.engine import SubscriptionEngine
from .services.logo_service import LogoService
from .services.parser import DynamicParser
from .services.renderer import Renderer
from .services.subscription_service import SubscriptionService
from .services.transport import Transport, text_segment
from .tools.bgm_daily import BgmDailyTool
from .tools.bgm_subject import BgmAdvancedSubjectSearchTool, BgmRecommendHotSubjectsTool
from .tools.bili_hot_video import BiliSearchHotVideosTool
from .tools.bili_user_dynamics import BiliUserDynamicsTool

ALIASES = {"订阅动态": "bili_sub", "订阅列表": "bili_sub_list", "订阅删除": "bili_sub_del", "订阅测试": "bili_sub_test",
           "全局订阅": "bili_global_sub", "全局列表": "bili_global_list", "全局删除": "bili_global_del", "卡片样式": "bili_card_style"}
COMMANDS = ["bili_help", "bili_sid", "bili_sub", "bili_sub_list", "bili_sub_del", "bili_sub_test", "bili_global_sub",
            "bili_global_list", "bili_global_del", "bili_login", "bili_logout", "bili_card_style", "bili_pending", *ALIASES]
COMMAND_PATTERN = r"^\s*/(?P<action>" + "|".join(sorted(COMMANDS, key=len, reverse=True)) + r")(?:\s+(?P<args>.*))?\s*$"
VIDEO_PATTERN = re.compile(r"https?://(?:www\.)?bilibili\.com/video/(BV[a-zA-Z0-9]{10})(?:[^\s<>\"']*)?|https?://b23\.tv/[a-zA-Z0-9]+|(?<![a-zA-Z0-9])BV[a-zA-Z0-9]{10}(?![a-zA-Z0-9])", re.I)


class BilibiliPlugin(MaiBotPlugin):
    config_model = BilibiliConfig

    def __init__(self):
        super().__init__()
        self.tasks = set()
        self.login_task = None
        self.ready = False
        self.parse_cache = {}
        self.parse_lock = asyncio.Lock()

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(self.task_finished)
        return task

    @staticmethod
    def task_finished(task):
        if not task.cancelled() and task.exception():
            logger.error("Bilibili 后台任务退出: %s", type(task.exception()).__name__)

    async def on_load(self):
        if not self.config.plugin.enabled:
            return
        data_dir = Path(self.ctx.paths.data_dir)
        cache_dir = Path(self.ctx.paths.runtime_dir) / "cards"
        self.store = await asyncio.to_thread(DataManager, data_dir, self.config.push.recent_dynamic_cache)
        self.logo = LogoService(LOGO_PATH, data_dir)
        self.renderer = Renderer(self.ctx, cache_dir, style=self.store.data.get("renderer_template") or self.config.push.renderer_template)
        self.loaded_config_style = self.config.push.renderer_template
        self.parser = DynamicParser(self.renderer)
        self.transport = Transport(self.ctx)
        self.client = BiliClient(credential_dict=self.store.get_credential(), sessdata=self.config.bili.sessdata, proxy=self.config.bili.proxy)
        self.engine = SubscriptionEngine(self)
        self.subscription_service = SubscriptionService(self.store, self.client, self.parser._parse_and_filter_dynamics)
        self.credentials = CredentialLifecycle(lambda: self.client, self.store.set_credential, lambda: self.store.get_credential() is not None)
        self.tools = {
            "bgm_search_subjects_advanced": BgmAdvancedSubjectSearchTool(token=self.config.bili.bangumi_token),
            "bgm_recommend_hot_subjects": BgmRecommendHotSubjectsTool(token=self.config.bili.bangumi_token),
            "get_bgm_daily_schedule": BgmDailyTool(token=self.config.bili.bangumi_token),
            "bili_search_hot_videos": BiliSearchHotVideosTool(bili_client=self.client),
            "bili_get_user_dynamics": BiliUserDynamicsTool(bili_client=self.client, parse_dynamics=self.parser._parse_and_filter_dynamics),
        }
        self.ready = True
        self.spawn(self.engine.start())
        self.spawn(self.credential_loop())
        logger.info("Bilibili 插件已加载；聊天管理员白名单人数=%s", len(self.config.access.admin_users))

    async def on_unload(self):
        self.ready = False
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.tasks.clear()
        self.login_task = None

    async def on_config_update(self, scope: str, config_data: dict, version: str):
        if scope == "self":
            requested_style = (config_data.get("push") or {}).get("renderer_template")
            if self.ready and requested_style and requested_style != self.loaded_config_style:
                await self.set_style(requested_style)
            await self.on_unload()
            await self.on_load()

    def require_ready(self):
        if not self.ready:
            raise ValueError("Bilibili 插件未启用或尚未初始化")

    async def credential_loop(self):
        while True:
            try:
                result = await self.credentials.refresh_if_required()
                if result.status == CredentialRefreshStatus.RELOGIN_REQUIRED:
                    logger.warning("B站凭据需要重新登录，请管理员私聊使用 /bili_login")
                elif result.status == CredentialRefreshStatus.FAILED:
                    logger.warning("B站凭据维护失败，下一周期重试")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("B站凭据维护异常: %s", type(exc).__name__)
            await asyncio.sleep(86400)

    def is_admin(self, user_id: str, local_operator=False):
        return local_operator or str(user_id) in {str(value) for value in self.config.access.admin_users}

    async def require_manage(self, user_id: str, group_id: str, local_operator=False, global_only=False):
        if self.is_admin(user_id, local_operator):
            return
        if not global_only:
            if not group_id or self.config.access.allow_member_subscribe:
                return
            if self.config.access.group_admin_can_manage and await self.transport.group_role(group_id, user_id) in {"admin", "owner"}:
                return
        raise PermissionError("权限不足；全局操作和B站登录需要在插件配置 access.admin_users 中设置管理员 QQ 号")

    @staticmethod
    def parse_options(options: str):
        tokens = shlex.split(options)
        filters, expressions = [], []
        flags = set()
        for token in tokens:
            if token in VALID_SUB_OPTIONS:
                flags.add(token)
            elif token in VALID_FILTER_TYPES:
                filters.append(token)
            else:
                if len(token) > 200:
                    raise ValueError("单条过滤表达式最多200字符")
                re.compile(token)
                expressions.append(token)
        return filters, expressions, flags

    async def subscribe(self, stream_id: str, uid: int, options: str, sender="", from_page=False):
        await self.transport.session(stream_id)
        filters, expressions, flags = self.parse_options(options)
        if not sender and flags & {"at_sub", "unat_sub"}:
            raise ValueError("全局或网页订阅请使用 at_users 指定提醒对象；at_sub/unat_sub 用于用户本人发命令")
        async with self.engine.lock:
            existing = self.store.get_subscription(stream_id, uid)
            inherit = not from_page and not filters and not expressions and bool(flags & {"at_all", "at_sub", "unat_sub"})
            live_atall = bool(existing.live_atall) if existing and inherit and "live_atall" not in flags else "live_atall" in flags
            result = await self.subscription_service.add_or_update(
                stream_id, uid, filters, expressions, live_atall,
                at_all=("at_all" in flags) if from_page else (True if "at_all" in flags else None),
                add_sub_users=[sender] if "at_sub" in flags else None,
                rm_sub_users=[sender] if "unat_sub" in flags else None,
                inherit_filters=inherit,
            )
            if not result.updated:
                baselines = dict(self.store.data.get("baselines", {}))
                baselines[self.engine.key(stream_id, uid)] = result.initialized
                await self.engine.save_runtime("baselines", baselines)
        return result

    async def remove(self, stream_id: str, uid=None):
        async with self.engine.lock:
            removed = await (self.subscription_service.remove(stream_id, uid) if uid is not None else self.subscription_service.remove_all_for_session(stream_id))
            for field in ("baselines", "pending_deliveries"):
                state = dict(self.store.data.get(field, {}))
                for key in list(state):
                    if key == self.engine.key(stream_id, uid) or (uid is None and key.startswith(stream_id + "/")):
                        state.pop(key)
                await self.engine.save_runtime(field, state)
            return removed

    @Command("bilibili_commands", description="B站订阅、登录和管理", pattern=COMMAND_PATTERN, timeout_ms=120000)
    async def command(self, stream_id: str = "", user_id: str = "", group_id: str = "", text: str = "", matched_groups=None, is_local_operator=False, **kwargs):
        try:
            self.require_ready()
            match = re.match(COMMAND_PATTERN, text, re.S)
            groups = matched_groups or (match.groupdict() if match else {})
            action = ALIASES.get(groups.get("action"), groups.get("action"))
            raw = (groups.get("args") or "").strip()
            args = shlex.split(raw.replace("「", '"').replace("」", '"'))
            reply = None
            if action == "bili_help":
                reply = "B站插件命令：\n/bili_sid 查看当前聊天流\n/bili_sub UID [过滤器/提醒选项]\n/bili_sub_list\n/bili_sub_del UID\n/bili_sub_test UID\n/bili_login（管理员私聊）\n/bili_logout（管理员）\n/bili_card_style [样式]\n/bili_global_list /bili_global_sub 聊天流 UID [选项] /bili_global_del 聊天流\n/bili_pending [UID ack|retry]：处理未确认推送\n过滤器：forward lottery video article draw live live_end forward_lottery 或正则\n提醒：at_sub unat_sub at_all live_atall"
            elif action == "bili_sid":
                reply = f"当前聊天流：{stream_id}"
            elif action == "bili_login":
                await self.require_manage(user_id, group_id, is_local_operator, global_only=True)
                if group_id:
                    raise ValueError("请在私聊中扫码登录B站")
                if self.login_task and not self.login_task.done():
                    raise ValueError("已有扫码登录任务，请先完成扫码或等待过期")
                self.login_task = self.spawn(self.login(stream_id))
                reply = "正在生成B站登录二维码……"
            elif action == "bili_logout":
                await self.require_manage(user_id, group_id, is_local_operator, global_only=True)
                if self.login_task and not self.login_task.done():
                    self.login_task.cancel()
                    await asyncio.gather(self.login_task, return_exceptions=True)
                async with self.engine.lock, self.credentials._refresh_lock:
                    success = await self.client.logout()
                    self.client.credential = None
                    await self.store.clear_credential()
                    if self.config.bili.sessdata:
                        self.client.set_credential({"sessdata": self.config.bili.sessdata})
                reply = "本地扫码凭据已清除；" + ("服务端已退出登录。" if success else "服务端退出未确认。")
                if self.config.bili.sessdata:
                    reply += "当前恢复使用配置中的 SESSDATA。"
            elif action == "bili_card_style":
                await self.require_manage(user_id, group_id, is_local_operator, global_only=True)
                if not args:
                    reply = "当前样式：" + self.renderer.style + "\n" + "\n".join(f"{key}：{value['name']}" for key, value in CARD_TEMPLATES.items())
                else:
                    reply = await self.set_style(args[0])
            elif action in {"bili_sub_list", "bili_global_list"}:
                if action == "bili_global_list":
                    await self.require_manage(user_id, group_id, is_local_operator, global_only=True)
                data = await self.page_state()
                records = data["subscriptions"]
                if action == "bili_sub_list":
                    records = [r for r in records if r["stream_id"] == stream_id]
                reply = "\n\n".join(f"{r['session_name']}｜UID {r['uid']}\n{r['options']}\n@订阅者：{', '.join(r['at_users']) or '无'}" for r in records) or "当前没有订阅"
            elif action == "bili_sub":
                if not args:
                    raise ValueError("用法：/bili_sub UID [选项]")
                uid = parse_bilibili_uid(args[0])
                opts = shlex.join(args[1:])
                filters, expressions, flags = self.parse_options(opts)
                personal_only = bool(flags) and flags <= {"at_sub", "unat_sub"} and not filters and not expressions
                if personal_only:
                    if not self.store.get_subscription(stream_id, uid):
                        raise ValueError("本群尚未订阅该 UP 主，请先由管理员添加订阅")
                else:
                    await self.require_manage(user_id, group_id, is_local_operator)
                if flags & {"at_all", "live_atall"}:
                    if not self.is_admin(user_id, is_local_operator) and (not group_id or await self.transport.group_role(group_id, user_id) not in {"admin", "owner"}):
                        raise PermissionError("只有管理员可设置 @全体")
                result = await self.subscribe(stream_id, uid, opts, user_id)
                reply = "订阅已更新。" if result.updated else "订阅成功。" + ("" if result.initialized else "B站暂未返回动态，首次成功查询将建立基线，不补发历史动态。")
                if flags & {"at_all", "live_atall"} and not await self.transport.can_at_all(stream_id):
                    reply += "\n当前无法确认机器人有 @全体权限或剩余次数，将使用普通通知。"
            elif action == "bili_global_sub":
                await self.require_manage(user_id, group_id, is_local_operator, global_only=True)
                if len(args) < 2:
                    raise ValueError("用法：/bili_global_sub 聊天流ID UID [选项]")
                result = await self.subscribe(args[0], parse_bilibili_uid(args[1]), shlex.join(args[2:]))
                reply = "目标聊天订阅已更新。" if result.updated else "目标聊天订阅已添加。"
            elif action in {"bili_sub_del", "bili_global_del"}:
                await self.require_manage(user_id, group_id, is_local_operator, global_only=action == "bili_global_del")
                if not args:
                    raise ValueError("请提供 UID 或目标聊天流ID")
                deleted = await self.remove(stream_id, parse_bilibili_uid(args[0])) if action == "bili_sub_del" else await self.remove(args[0])
                reply = "删除成功。" if deleted else "没有找到指定订阅。"
            elif action == "bili_sub_test":
                await self.require_manage(user_id, group_id, is_local_operator)
                if not args:
                    raise ValueError("用法：/bili_sub_test UID")
                uid = parse_bilibili_uid(args[0])
                dyn = await self.client.get_latest_dynamics(uid)
                if not dyn:
                    raise ValueError("没有取得动态，请检查B站登录状态")
                sub = copy.deepcopy(self.store.get_subscription(stream_id, uid) or SubscriptionRecord(uid=uid))
                sub.last, sub.recent_ids = "", []
                payload = next((r.payload for r in self.parser._parse_and_filter_dynamics(dyn, sub) if r.has_payload()), None)
                if not payload:
                    raise ValueError("没有可测试的动态，可能全部被过滤或类型暂不支持")
                await self.engine.send_payload(stream_id, payload)  # 测试不更新游标，也不触发 @。
            elif action == "bili_pending":
                await self.require_manage(user_id, group_id, is_local_operator)
                if len(args) == 2:
                    async with self.engine.lock:
                        await self.engine.resolve_pending(stream_id, parse_bilibili_uid(args[0]), args[1])
                    reply = "待确认记录已处理。retry 将允许下一轮重新推送。"
                elif not args:
                    rows = [r for r in self.engine.pending().values() if r["stream_id"] == stream_id]
                    reply = "\n".join(f"UID {r['uid']}｜{r['category']}｜动态ID {r.get('dyn_id', '-')}" for r in rows) or "没有待确认投递"
                else:
                    raise ValueError("用法：/bili_pending 或 /bili_pending UID ack|retry")
            if reply:
                await self.transport.send(stream_id, [text_segment(reply)])
            return True, None, True
        except (ValueError, PermissionError, re.error) as exc:
            await self.ctx.send.text(str(exc), stream_id)
            return False, None, True
        except Exception as exc:
            logger.exception("Bilibili命令执行失败")
            await self.ctx.send.text(f"操作失败（{type(exc).__name__}），请查看日志；未确认的订阅推送可用 /bili_pending 查询。", stream_id)
            return False, None, True

    async def login(self, stream_id: str):
        qr = None
        try:
            client = BiliQrLoginClient(proxy=self.config.bili.proxy)
            qr = await client.generate_qrcode()
            import base64
            image = await asyncio.to_thread(lambda: base64.b64encode(Path(qr.image_path).read_bytes()).decode("ascii"))
            await self.transport.send(stream_id, [text_segment("请使用 Bilibili App 扫码登录："), {"type": "image", "content": image}])
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                result = await client.poll()
                if result.is_done:
                    async with self.engine.lock, self.credentials._refresh_lock:
                        self.client.set_credential(result.credential)
                        await self.store.set_credential(result.credential)
                    await self.transport.send(stream_id, [text_segment("B站登录成功，凭据已保存。")])
                    return
                if result.is_timeout:
                    break
                await asyncio.sleep(2)
            await self.ctx.send.text("二维码已过期，请重新使用 /bili_login。", stream_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Credential values must not be interpolated into chat messages or logs.
            logger.warning("扫码登录失败: %s", type(exc).__name__)
            await self.ctx.send.text("B站扫码登录失败，请稍后重试。", stream_id)
        finally:
            if qr:
                Path(qr.image_path).unlink(missing_ok=True)

    @HookHandler("chat.receive.after_process", name="bilibili_link_parser", mode=HookMode.OBSERVE, timeout_ms=120000)
    async def receive(self, message=None, **kwargs):
        if not self.ready or not isinstance(message, dict) or message.get("platform") != "qq" or message.get("is_notify"):
            return
        info = message.get("message_info") or {}
        user = info.get("user_info") or {}
        if str(user.get("user_id") or "") == str(message.get("account_id") or ""):
            return
        text = str(message.get("processed_plain_text") or message.get("plain_text") or "")
        if text.lstrip().startswith("/"):
            return
        raw = message.get("raw_message") or []
        if isinstance(raw, list):
            text += "\n" + "\n".join(str(seg.get("data") or seg.get("content") or "") for seg in raw if isinstance(seg, dict) and seg.get("type") == "text")
        is_card = "[小程序]" in text or bool((info.get("additional_config") or {}).get("platform_card_payloads"))
        if (is_card and not self.config.parse.enable_parse_miniapp) or (not is_card and not self.config.parse.enable_parse_BV):
            return
        match = VIDEO_PATTERN.search(text)
        if not match:
            return
        try:
            # after_process occurs before chat registration; ask the Host to resolve/create
            # the real session rather than guessing the chat stream hash.
            stream_id = str(message.get("session_id") or "")
            if not stream_id:
                group = info.get("group_info") or {}
                stream = await self.ctx.chat.open_session(platform="qq", chat_type="group" if group.get("group_id") else "private",
                    user_id=str(user.get("user_id") or ""), group_id=str(group.get("group_id") or ""),
                    account_id=str(message.get("account_id") or ""), scope=str(message.get("scope") or ""))
                stream_id = str(stream.get("stream_id") or stream.get("session_id") or "")
            token = match.group(0)
            if "b23.tv/" in token:
                token = await self.client.b23_to_bv(token)
            bvid = re.search(r"BV[a-zA-Z0-9]{10}", token or "", re.I)
            if not bvid or not stream_id:
                return
            bvid = bvid.group(0)
            key = (stream_id, bvid)
            async with self.parse_lock:
                now = time.monotonic()
                self.parse_cache = {k: v for k, v in self.parse_cache.items() if now - v < max(self.config.parse.cooldown_secs, 1)}
                if key in self.parse_cache:
                    return
                self.parse_cache[key] = now
            data = await self.client.get_video_info(bvid)
            if not data:
                await self.ctx.send.text("未能取得视频信息，请稍后重试。", stream_id)
                return
            video, online = data["info"], data.get("online") or {}
            stats = video.get("stat") or {}
            body = f"UP主：{html.escape(str(video.get('owner', {}).get('name', '')))}<br>播放：{stats.get('view', 0)}｜点赞：{stats.get('like', 0)}｜投币：{stats.get('coin', 0)}<br>正在观看：{online.get('total', '未知')}"
            payload = RenderPayload(name="MaiBot", avatar=image_to_base64(self.logo.get_active_path()), title=str(video.get("title") or ""),
                                    text=body, image_urls=[video["pic"]] if video.get("pic") else [], url=f"https://www.bilibili.com/video/{bvid}")
            await self.engine.send_payload(stream_id, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("自动解析B站链接失败: %s", type(exc).__name__)

    async def set_style(self, style: str):
        if style not in CARD_TEMPLATES:
            raise ValueError("样式只能是 template_1、template_2、simple")
        self.renderer.style = style
        await self.engine.save_runtime("renderer_template", style)
        return f"已切换样式：{CARD_TEMPLATES[style]['name']}"

    @API("page_state", version="1")
    async def page_state(self):
        self.require_ready()
        streams = await self.transport.streams()
        names = {str(s.get("stream_id") or s.get("session_id")): s.get("group_name") or s.get("user_cardname") or s.get("user_nickname") or "未命名聊天" for s in streams}
        records = []
        for stream_id, subs in self.store.get_all_subscriptions().items():
            for sub in subs:
                options = sub.filter_types + [shlex.quote(r) for r in sub.filter_regex]
                options += [name for name, enabled in (("at_all", sub.at_all), ("live_atall", sub.live_atall)) if enabled]
                records.append({"stream_id": stream_id, "session_name": names.get(stream_id, "离线聊天"), "uid": str(sub.uid),
                                "options": " ".join(options), "at_users": sub.at_sub_users,
                                "pending": self.engine.key(stream_id, sub.uid) in self.engine.pending()})
        return {"subscriptions": records, "streams": [{"stream_id": str(s.get("stream_id") or s.get("session_id")), "name": names[str(s.get("stream_id") or s.get("session_id"))]} for s in streams],
                "pending": list(self.engine.pending().values()), "logged_in": self.client.credential is not None,
                "style": self.renderer.style, "running": self.ready, "logo_customized": Path(self.logo.custom_path).is_file()}

    @API("page_save", version="1")
    async def page_save(self, stream_id: str, uid: str, options: str = "", at_users: str = ""):
        self.require_ready()
        uid_int = parse_bilibili_uid(uid)
        users = [value for value in re.split(r"[,，\s]+", at_users.strip()) if value]
        if any(not u.isdigit() for u in users) or len(users) > 50:
            raise ValueError("提醒QQ号需为数字，最多50人，使用空格或逗号分隔")
        result = await self.subscribe(stream_id, uid_int, options, from_page=True)
        async with self.engine.lock:
            current = self.store.get_subscription(stream_id, uid_int)
            if not current:
                raise ValueError("订阅已被删除，请刷新页面")
            await self.store.update_subscription(stream_id, uid_int, current.filter_types, current.filter_regex, current.live_atall,
                at_all="at_all" in self.parse_options(options)[2], add_sub_users=users, rm_sub_users=[u for u in current.at_sub_users if u not in users])
        return {"message": "订阅已更新" if result.updated else "订阅已添加"}

    @API("page_delete", version="1")
    async def page_delete(self, stream_id: str, uid: str):
        self.require_ready()
        return {"deleted": await self.remove(stream_id, parse_bilibili_uid(uid))}

    @API("page_delete_session", version="1")
    async def page_delete_session(self, stream_id: str):
        self.require_ready()
        return {"deleted": await self.remove(stream_id)}

    @API("page_pending", version="1")
    async def page_pending(self, stream_id: str, uid: str, resolution: str):
        self.require_ready()
        async with self.engine.lock:
            await self.engine.resolve_pending(stream_id, parse_bilibili_uid(uid), resolution)
        return {"message": "待确认投递已处理"}

    @API("page_style", version="1")
    async def page_style(self, style: str):
        self.require_ready()
        return {"message": await self.set_style(style)}

    @API("page_up_info", version="1")
    async def page_up_info(self, uid: str):
        self.require_ready()
        info, _ = await self.client.get_user_info(parse_bilibili_uid(uid))
        if not info:
            raise ValueError("无法取得UP主资料，请检查UID及B站登录状态")
        return {"uid": str(info.get("mid")), "name": str(info.get("name") or ""), "profile_url": f"https://space.bilibili.com/{info.get('mid')}"}

    @API("page_logo_upload", version="1")
    async def page_logo_upload(self, upload_id: str):
        self.require_ready()
        upload = await self.ctx.webui.claim_upload(upload_id)
        path = Path(upload["path"]).resolve()
        data_dir = Path(self.ctx.paths.data_dir).resolve()
        if not path.is_relative_to(data_dir):
            raise ValueError("上传资源不在本插件的数据目录")
        try:
            async with self.logo._mutation_lock:
                temporary = self.logo.custom_path.with_suffix(".tmp")
                try:
                    await asyncio.to_thread(self.logo._convert_to_png, path, temporary)
                    await asyncio.to_thread(os.replace, temporary, self.logo.custom_path)
                finally:
                    temporary.unlink(missing_ok=True)
        finally:
            path.unlink(missing_ok=True)
        return {"message": "卡片头像已更新"}

    @API("page_logo_reset", version="1")
    async def page_logo_reset(self):
        self.require_ready()
        await self.logo.reset()
        return {"message": "已恢复默认卡片头像"}


def attach_tool(tool_class):
    prototype = tool_class()
    allowed = set(inspect.signature(prototype.call).parameters) - {"context"}

    async def handler(self, **kwargs):
        self.require_ready()
        try:
            result = await self.tools[prototype.name].call(None, **{k: v for k, v in kwargs.items() if k in allowed})
            return {"name": prototype.name, "content": result}
        except Exception as exc:
            logger.warning("Bilibili工具失败 %s: %s", prototype.name, type(exc).__name__)
            return {"name": prototype.name, "content": f"查询失败（{type(exc).__name__}），请稍后重试，不要编造结果。"}

    handler.__name__ = prototype.name
    setattr(BilibiliPlugin, prototype.name, Tool(prototype.name, description=prototype.description,
                                               parameters=prototype.parameters, timeout_ms=120000)(handler))


for _tool in (BgmAdvancedSubjectSearchTool, BgmRecommendHotSubjectsTool, BgmDailyTool, BiliSearchHotVideosTool, BiliUserDynamicsTool):
    attach_tool(_tool)


def create_plugin():
    return BilibiliPlugin()
