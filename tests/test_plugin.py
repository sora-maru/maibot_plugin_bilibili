import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from maibot_sdk.context import PluginContext, PluginPaths

from maibot_plugin_bilibili.core.data_manager import DataManager
from maibot_plugin_bilibili.core.models import SubscriptionRecord
from maibot_plugin_bilibili.plugin import create_plugin
from maibot_plugin_bilibili.services.transport import DeliveryUncertain


def dynamic(dyn_id="101", text="新作品", kind="DYNAMIC_TYPE_WORD"):
    return {"id_str": dyn_id, "type": kind, "modules": {
        "module_author": {"name": "测试UP", "face": "", "pub_time": "刚刚"},
        "module_dynamic": {"major": {"opus": {"summary": {"text": text, "rich_text_nodes": []}, "pics": [], "jump_url": f"//www.bilibili.com/opus/{dyn_id}"}}},
    }}


class Host:
    """Exercise real SDK serialization/normalization against host-shaped RPC results."""
    def __init__(self):
        self.calls = []
        self.sent = []
        self.confirmed = True
        self.streams = [
            {"stream_id": "group-stream", "platform": "qq", "group_id": "100", "group_name": "测试群", "account_id": "999", "user_id": "", "scope": ""},
            {"stream_id": "private-stream", "platform": "qq", "group_id": "", "user_id": "1", "user_nickname": "管理员", "account_id": "999", "scope": ""},
        ]

    async def rpc(self, method, plugin_id="", payload=None, **kwargs):
        self.calls.append((method, payload))
        if method != "cap.call":
            raise AssertionError(method)
        cap, args = payload["capability"], payload["args"]
        if cap == "chat.get_all_streams":
            return {"success": True, "streams": self.streams}
        if cap == "chat.open_session":
            return {"success": True, **self.streams[0], "stream": self.streams[0]}
        if cap.startswith("send."):
            self.sent.append((cap, copy.deepcopy(args)))
            return {"success": self.confirmed, "sent": self.confirmed, "message_id": "123" if self.confirmed else None}
        if cap == "api.call":
            name = args["api_name"]
            if name.endswith("get_group_member_info"):
                return {"success": True, "result": {"role": "admin" if args["args"]["user_id"] in {1, 999} else "member"}}
            if name.endswith("get_group_at_all_remain"):
                return {"success": True, "result": {"can_at_all": True, "remain_at_all_count_for_self": 2, "remain_at_all_count_for_group": 2}}
            if name.endswith("action.call"):
                return {"success": True, "result": {"retcode": 0, "status": "ok"}}
            raise AssertionError(name)
        if cap == "render.html2png":
            return {"success": False, "error": "renderer intentionally unavailable in tests"}
        if cap == "llm.generate":
            return {"success": True, "response": "测试摘要"}
        raise AssertionError(cap)


@pytest.fixture
async def plugin(tmp_path):
    host = Host()
    p = create_plugin()
    p.set_plugin_config({"plugin": {"config_version": "1.0.0"}, "access": {"admin_users": ["1"]}, "push": {"render_as_image": False}})
    p._set_context(PluginContext("github.sora-maru.bilibili", rpc_call=host.rpc, paths=PluginPaths(tmp_path / "data", tmp_path / "runtime")))
    await p.on_load()
    # Keep the lifecycle initialization real, but drive polling deterministically.
    for task in list(p.tasks):
        task.cancel()
    await asyncio.gather(*list(p.tasks), return_exceptions=True)
    p.client.get_latest_dynamics = AsyncMock(return_value={"items": [dynamic("100")]})
    p.client.get_live_info_by_uids = AsyncMock(return_value=None)
    yield p, host
    await p.on_unload()


async def subscribe(p, stream="group-stream", options=""):
    await p.subscribe(stream, 42, options, "1")
    return p.store.get_subscription(stream, 42)


def message(text, *, card=False, own=False, session="group-stream"):
    return {"platform": "qq", "account_id": "999", "session_id": session,
            "message_info": {"user_info": {"user_id": "999" if own else "2"}, "group_info": {"group_id": "100"}, "additional_config": {}},
            "processed_plain_text": ("[小程序] 哔哩哔哩 链接: " if card else "") + text, "raw_message": []}


def test_registration_and_schema():
    p = create_plugin()
    p.set_plugin_config({})
    components = p.get_components()
    names = {item["name"] for item in components}
    assert {"bili_search_hot_videos", "bili_get_user_dynamics", "bgm_search_subjects_advanced", "bgm_recommend_hot_subjects", "get_bgm_daily_schedule", "bilibili_link_parser", "bilibili_commands"} <= names
    assert len(names) == len(components)
    assert p.config.access.admin_users == []
    assert p.build_config_schema()
    assert "astrbot" not in __import__("sys").modules


async def test_subscription_baseline_does_not_push_history(plugin):
    p, host = plugin
    await subscribe(p)
    await p.engine.check("group-stream", 42, {"items": [dynamic("100")]}, None)
    assert not host.sent
    assert p.store.get_subscription("group-stream", 42).last == "100"


async def test_new_dynamic_sent_once_persisted_after_restart(plugin):
    p, host = plugin
    await subscribe(p)
    dyn = {"items": [dynamic("102"), dynamic("101"), dynamic("100")]}
    await p.engine.check("group-stream", 42, dyn, None)
    await p.engine.check("group-stream", 42, dyn, None)
    assert len(host.sent) == 2
    assert "101" in host.sent[0][1]["segments"][0]["content"]
    reloaded = DataManager(Path(p.store.path).parent)
    assert reloaded.get_subscription("group-stream", 42).last == "102"
    assert not p.engine.pending()


async def test_uncertain_send_is_persistent_and_blocks_retry(plugin):
    p, host = plugin
    await subscribe(p)
    host.confirmed = False
    dyn = {"items": [dynamic(), dynamic("100")]}
    await p.engine.check("group-stream", 42, dyn, None)
    assert p.store.get_subscription("group-stream", 42).last == "100"
    assert p.engine.pending()
    assert DataManager(Path(p.store.path).parent).data["pending_deliveries"]
    await p.engine.check("group-stream", 42, dyn, None)
    assert len(host.sent) == 1
    host.confirmed = True
    await p.engine.resolve_pending("group-stream", 42, "retry")
    await p.engine.check("group-stream", 42, dyn, None)
    assert len(host.sent) == 2
    assert p.store.get_subscription("group-stream", 42).last == "101"


async def test_ack_uncertain_send_advances_without_resend(plugin):
    p, host = plugin
    await subscribe(p)
    host.confirmed = False
    dyn = {"items": [dynamic(), dynamic("100")]}
    await p.engine.check("group-stream", 42, dyn, None)
    await p.engine.resolve_pending("group-stream", 42, "ack")
    await p.engine.check("group-stream", 42, dyn, None)
    assert len(host.sent) == 1
    assert p.store.get_subscription("group-stream", 42).last == "101"


async def test_failed_initial_query_establishes_later_baseline(plugin):
    p, host = plugin
    p.client.get_latest_dynamics.return_value = None
    result = await p.subscribe("group-stream", 42, "", "1")
    assert not result.initialized
    await p.engine.check("group-stream", 42, {"items": [dynamic("120")]}, None)
    assert not host.sent
    assert p.store.get_subscription("group-stream", 42).last == "120"


async def test_filters_and_filtered_cursor(plugin):
    p, host = plugin
    await subscribe(p, options="广告")
    await p.engine.check("group-stream", 42, {"items": [dynamic(text="广告内容"), dynamic("100")]}, None)
    assert not host.sent
    assert p.store.get_subscription("group-stream", 42).last == "101"


async def test_video_title_regex_is_filtered(plugin):
    p, _ = plugin
    item = dynamic(kind="DYNAMIC_TYPE_AV")
    item["modules"]["module_dynamic"]["major"] = {"archive": {"title": "广告视频", "bvid": "BV1xx411c7mD", "cover": ""}}
    sub = SubscriptionRecord(uid=42, filter_regex=["广告"])
    parsed = p.parser._parse_and_filter_dynamics({"items": [item]}, sub)
    assert not parsed[0].has_payload()


async def test_forward_type_filter(plugin):
    p, _ = plugin
    result = p.parser._parse_and_filter_dynamics({"items": [dynamic(kind="DYNAMIC_TYPE_FORWARD")]}, SubscriptionRecord(uid=42, filter_types=["forward"]))
    assert result[0].reason == "forward"


async def test_personal_at_does_not_change_group_filters_or_atall(plugin):
    p, _ = plugin
    sub = await subscribe(p, options="lottery live_atall at_all 广告")
    await p.command(stream_id="group-stream", user_id="2", group_id="100", text="/bili_sub 42 at_sub")
    assert sub.filter_types == ["lottery"] and sub.filter_regex == ["广告"]
    assert sub.live_atall and sub.at_all and "2" in sub.at_sub_users


async def test_member_cannot_create_subscription(plugin):
    p, host = plugin
    await p.command(stream_id="group-stream", user_id="2", group_id="100", text="/bili_sub 42")
    assert not p.store.get_subscription("group-stream", 42)
    assert "权限不足" in host.sent[-1][1]["text"]


async def test_login_is_admin_private_only(plugin):
    p, host = plugin
    await p.command(stream_id="group-stream", user_id="1", group_id="100", text="/bili_login")
    assert p.login_task is None
    assert "私聊" in host.sent[-1][1]["text"]
    await p.command(stream_id="private-stream", user_id="2", text="/bili_login")
    assert p.login_task is None


async def test_page_overwrite_can_disable_atall_and_replace_users(plugin):
    p, _ = plugin
    await subscribe(p, options="at_all live_atall at_sub")
    await p.page_save("group-stream", "42", options="", at_users="2 3")
    sub = p.store.get_subscription("group-stream", 42)
    assert not sub.at_all and not sub.live_atall
    assert sorted(sub.at_sub_users) == ["2", "3"]


async def test_page_delete_cleans_pending_and_baseline(plugin):
    p, _ = plugin
    await subscribe(p)
    await p.engine.mark_pending("group-stream", 42, category="dynamic", dyn_id="101")
    await p.page_delete("group-stream", "42")
    assert not p.engine.pending()
    assert "group-stream/42" not in p.store.data["baselines"]


async def test_sub_test_does_not_advance_or_at(plugin):
    p, host = plugin
    sub = await subscribe(p, options="at_all")
    sub.last = "100"
    p.client.get_latest_dynamics.return_value = {"items": [dynamic()]}
    await p.command(stream_id="group-stream", user_id="1", group_id="100", text="/bili_sub_test 42")
    assert sub.last == "100"
    assert len(host.sent) == 1
    assert all(s["type"] != "at" for s in host.sent[0][1]["segments"])


async def test_live_start_and_end_have_correct_at_behavior(plugin):
    p, host = plugin
    sub = await subscribe(p, options="live_atall")
    live = {"live_status": 1, "live_time": 1000, "room_id": 99, "uname": "测试UP", "title": "直播标题"}
    await p.engine.check("group-stream", 42, None, live)
    assert sub.is_live
    assert host.sent[0][1]["segments"][0] == {"type": "at", "data": {"target_user_id": "all"}}
    await p.engine.check("group-stream", 42, None, {**live, "live_status": 0})
    assert not sub.is_live and sub.last_live_start_ts == 0
    assert all(s["type"] != "at" for s in host.sent[-1][1]["segments"])


async def test_live_end_filter_still_updates_state(plugin):
    p, host = plugin
    sub = await subscribe(p, options="live_end")
    sub.is_live = True
    await p.engine.check("group-stream", 42, None, {"live_status": 0})
    assert not sub.is_live and not host.sent


async def test_uncertain_live_does_not_flip_state_until_ack(plugin):
    p, host = plugin
    sub = await subscribe(p)
    host.confirmed = False
    await p.engine.check("group-stream", 42, None, {"live_status": 1, "room_id": 99, "live_time": 1000})
    assert not sub.is_live
    await p.engine.resolve_pending("group-stream", 42, "ack")
    assert sub.is_live and sub.last_live_start_ts == 1000


async def test_reconnect_silent_consumes_without_sending(plugin):
    p, host = plugin
    await subscribe(p)
    p.engine.silent_until = float("inf")
    await p.engine.check("group-stream", 42, {"items": [dynamic(), dynamic("100")]}, None)
    assert not host.sent and p.store.get_subscription("group-stream", 42).last == "101"


async def test_limit_keeps_unprocessed_items_for_next_poll(plugin):
    p, host = plugin
    p.config.push.dynamic_limit = 1
    await subscribe(p)
    dyn = {"items": [dynamic("102"), dynamic("101"), dynamic("100")]}
    await p.engine.check("group-stream", 42, dyn, None)
    assert p.store.get_subscription("group-stream", 42).last == "101"
    await p.engine.check("group-stream", 42, dyn, None)
    assert p.store.get_subscription("group-stream", 42).last == "102" and len(host.sent) == 2


async def test_forward_mentions_outside_nodes(plugin):
    p, host = plugin
    p.config.push.forward = True
    await subscribe(p, options="at_sub")
    await p.engine.check("group-stream", 42, {"items": [dynamic(), dynamic("100")]}, None)
    assert [c[0] for c in host.sent] == ["send.hybrid", "send.forward"]
    assert host.sent[0][1]["segments"][0]["type"] == "at"
    assert all(s["type"] != "at" for s in host.sent[1][1]["messages"][0]["segments"])


async def test_hook_extracts_video_and_cooldown(plugin):
    p, host = plugin
    p.client.get_video_info = AsyncMock(return_value={"info": {"title": "测试视频", "owner": {"name": "UP"}, "stat": {}}, "online": {}})
    await p.receive(message("看看 BV1xx411c7mD"))
    await p.receive(message("看看 BV1xx411c7mD"))
    assert p.client.get_video_info.await_count == 1 and len(host.sent) == 1


async def test_hook_smallapp_shortlink_and_session_resolution(plugin):
    p, host = plugin
    p.client.b23_to_bv = AsyncMock(return_value="https://www.bilibili.com/video/BV1xx411c7mD")
    p.client.get_video_info = AsyncMock(return_value={"info": {"title": "视频", "stat": {}}, "online": {}})
    await p.receive(message("https://b23.tv/abc123", card=True, session=""))
    assert p.client.get_video_info.await_count == 1 and len(host.sent) == 1
    assert any(call[1].get("capability") == "chat.open_session" for call in host.calls)


async def test_hook_ignores_self_and_disabled_cards(plugin):
    p, host = plugin
    p.client.get_video_info = AsyncMock()
    await p.receive(message("BV1xx411c7mD", own=True))
    p.config.parse.enable_parse_miniapp = False
    await p.receive(message("BV1xx411c7mD", card=True))
    p.client.get_video_info.assert_not_awaited()


async def test_render_failure_uses_plain_without_losing_delivery(plugin):
    p, host = plugin
    p.config.push.render_as_image = True
    await subscribe(p)
    await p.engine.check("group-stream", 42, {"items": [dynamic(), dynamic("100")]}, None)
    assert len(host.sent) == 1 and p.store.get_subscription("group-stream", 42).last == "101"


async def test_ai_summary_after_success_only(plugin):
    p, host = plugin
    p.config.ai.enable_summary = True
    await subscribe(p)
    await p.engine.check("group-stream", 42, {"items": [dynamic(), dynamic("100")]}, None)
    assert len(host.sent) == 2
    assert "测试摘要" in host.sent[-1][1]["segments"][0]["content"]


async def test_api_does_not_disclose_credentials(plugin):
    p, _ = plugin
    await p.store.set_credential({"sessdata": "SECRET-COOKIE"})
    assert "SECRET-COOKIE" not in json.dumps(await p.page_state())


async def test_style_persistence(plugin):
    p, _ = plugin
    await p.set_style("simple")
    assert DataManager(Path(p.store.path).parent).data["renderer_template"] == "simple"
    with pytest.raises(ValueError):
        await p.set_style("../../unexpected")


async def test_failed_sdk_receipt_is_not_truthy_success(plugin):
    p, host = plugin
    host.confirmed = False
    with pytest.raises(DeliveryUncertain):
        await p.transport.send("group-stream", [{"type": "text", "content": "test"}])


async def test_lifecycle_cancels_login_and_worker(plugin):
    p, _ = plugin
    task = p.spawn(asyncio.sleep(1000))
    await p.on_unload()
    assert task.cancelled() and not p.ready and not p.tasks


def test_invalid_regex_rejected():
    with pytest.raises(Exception):
        create_plugin().parse_options("[invalid")


async def test_file_uses_upload_action_with_base64(plugin, tmp_path):
    p, host = plugin
    path = tmp_path / "image.png"
    path.write_bytes(b"sample")
    await p.transport.send_file("group-stream", str(path), "card.png")
    args = host.calls[-1][1]["args"]["args"]
    assert args["action_name"] == "upload_group_file"
    assert args["params"]["file"] == "base64://c2FtcGxl"


async def test_distinct_datastores_do_not_share_default_dict(tmp_path):
    a, b = DataManager(tmp_path / "a"), DataManager(tmp_path / "b")
    await a.add_subscription("a", SubscriptionRecord(uid=42))
    assert not b.get_all_subscriptions()


async def test_scan_login_persists_credentials_and_cleans_qr(plugin, monkeypatch, tmp_path):
    p, host = plugin
    qr = tmp_path / "login.png"
    qr.write_bytes(b"qr-image")
    credential = {"sessdata": "test-cookie", "bili_jct": "csrf", "dedeuserid": "1"}
    fake = SimpleNamespace(generate_qrcode=AsyncMock(return_value=SimpleNamespace(image_path=str(qr))),
                           poll=AsyncMock(return_value=SimpleNamespace(is_done=True, credential=credential)))
    monkeypatch.setattr("maibot_plugin_bilibili.plugin.BiliQrLoginClient", lambda **kwargs: fake)
    await p.login("private-stream")
    assert p.store.get_credential() == credential
    assert DataManager(Path(p.store.path).parent).get_credential() == credential
    assert not qr.exists()
    assert "test-cookie" not in json.dumps(host.sent)


async def test_logout_restores_configured_sessdata(plugin):
    p, _ = plugin
    p.config.bili.sessdata = "configured-cookie"
    await p.store.set_credential({"sessdata": "scan-cookie"})
    p.client.logout = AsyncMock(return_value=True)
    await p.command(stream_id="private-stream", user_id="1", text="/bili_logout")
    assert p.store.get_credential() is None
    assert p.client.credential.sessdata == "configured-cookie"


async def test_renderer_uses_real_png_response_and_removes_temporary_card(plugin):
    import base64
    import io
    from PIL import Image
    p, host = plugin
    p.config.push.render_as_image = True
    buffer = io.BytesIO()
    Image.new("RGB", (630, 500), "pink").save(buffer, format="PNG")
    p.renderer.context = SimpleNamespace(render=SimpleNamespace(html2png=AsyncMock(return_value={"image_base64": base64.b64encode(buffer.getvalue()).decode()})))
    await subscribe(p)
    await p.engine.check("group-stream", 42, {"items": [dynamic(), dynamic("100")]}, None)
    assert host.sent[0][1]["segments"][0]["type"] == "image"
    assert not list(p.renderer.cache_dir.glob("card-*.png"))


async def test_bgm_tool_returns_original_data_through_sdk_component(plugin, monkeypatch):
    p, _ = plugin
    fake = AsyncMock(return_value=[{"id": 1, "name": "测试番剧", "name_cn": "测试番剧", "date": "2026-01-01", "rating": {"score": 8.5, "rank": 100}}])
    monkeypatch.setattr("maibot_plugin_bilibili.bgm_client.BangumiApiClient.search_subjects", fake)
    result = await p.bgm_search_subjects_advanced(keyword="测试", filters={"subject_type": "anime"}, stream_id="group-stream")
    assert "测试番剧" in result["content"]
    assert fake.await_args.kwargs["filter_payload"]["type"] == [2]


async def test_bili_tool_filters_runtime_extra_arguments(plugin):
    p, _ = plugin
    p.client.get_hot_videos = AsyncMock(return_value={"list": [{"title": "测试热门", "bvid": "BV1xx411c7mD", "owner": {"name": "UP"}, "stat": {}}]})
    result = await p.bili_search_hot_videos(keyword="", limit=1, stream_id="group-stream", context={"extra": True})
    assert "BV1xx411c7mD" in result["content"]


async def test_avatar_upload_validates_and_replaces_atomically(plugin, tmp_path):
    from PIL import Image
    p, _ = plugin
    source = Path(p.ctx.paths.data_dir) / "uploads" / "logo.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (80, 80), "pink").save(source)
    p._ctx.webui = SimpleNamespace(claim_upload=AsyncMock(return_value={"path": str(source)}))
    await p.page_logo_upload("test-token")
    assert p.logo.custom_path.is_file() and not source.exists()
    with Image.open(p.logo.custom_path) as image:
        assert image.size == (80, 80)


async def test_member_cannot_enable_atall_even_with_member_subscribe(plugin):
    p, _ = plugin
    p.config.access.allow_member_subscribe = True
    await p.command(stream_id="group-stream", user_id="2", group_id="100", text="/bili_sub 42 at_all")
    assert not p.store.get_subscription("group-stream", 42)


async def test_groups_are_isolated(plugin):
    p, host = plugin
    await subscribe(p, "group-stream", "广告")
    await subscribe(p, "private-stream")
    dyn = {"items": [dynamic(text="广告内容"), dynamic("100")]}
    await p.engine.check("group-stream", 42, dyn, None)
    await p.engine.check("private-stream", 42, dyn, None)
    assert len(host.sent) == 1 and host.sent[0][1]["stream_id"] == "private-stream"


async def test_polling_shares_uid_requests_between_chats(plugin):
    p, _ = plugin
    await subscribe(p, "group-stream")
    await subscribe(p, "private-stream")
    p.client.credential = SimpleNamespace()
    p.client.get_latest_dynamics.reset_mock()
    p.client.get_live_info_by_uids.reset_mock()
    done = asyncio.Event()
    checked = []

    async def check(stream_id, uid, dyn, live):
        checked.append(stream_id)
        if len(checked) == 2:
            done.set()

    p.engine.check = check
    task = p.spawn(p.engine.start())
    await asyncio.wait_for(done.wait(), timeout=3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert set(checked) == {"group-stream", "private-stream"}
    p.client.get_latest_dynamics.assert_awaited_once_with(42)
    p.client.get_live_info_by_uids.assert_awaited_once_with([42])
