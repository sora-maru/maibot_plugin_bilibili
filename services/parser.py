from __future__ import annotations
import re
from typing import Any, Dict, List, Optional, Tuple
from ..log import logger
from ..core.models import DynamicParseResult, RenderPayload, SubscriptionRecord
from ..core.utils import create_qrcode
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600

class DynamicParser:
    def __init__(self, renderer):
        self.renderer = renderer

    @staticmethod
    def _parse_live_start_timestamp(live_room: Dict[str, Any]) -> int:
        try:
            live_start_ts = int(live_room.get("live_time", 0) or 0)
        except (TypeError, ValueError):
            return 0
        if live_start_ts <= 0:
            return 0
        return live_start_ts


    @staticmethod
    def _calc_live_duration_seconds(current_ts: int, live_start_ts: int) -> int:
        if current_ts <= 0 or live_start_ts <= 0:
            return 0
        if current_ts <= live_start_ts:
            return 0
        return current_ts - live_start_ts


    @staticmethod
    def _format_live_duration_text(duration_seconds: int) -> str:
        if duration_seconds <= 0:
            return ""

        hours = duration_seconds // SECONDS_PER_HOUR
        minutes = (duration_seconds % SECONDS_PER_HOUR) // SECONDS_PER_MINUTE
        seconds = duration_seconds % SECONDS_PER_MINUTE
        if hours > 0:
            return f"{hours}小时{minutes}分钟{seconds}秒"
        if minutes > 0:
            return f"{minutes}分钟{seconds}秒"
        return f"{seconds}秒"


    @staticmethod
    def _evaluate_live_transition(
        sub_data: SubscriptionRecord, live_room: Dict[str, Any]
    ) -> Tuple[bool, bool, bool]:
        is_live_now = live_room.get("live_status", "") == 1
        is_live_started = is_live_now and not sub_data.is_live
        is_live_ended = (not is_live_now) and sub_data.is_live
        return is_live_now, is_live_started, is_live_ended


    def _get_dynamic_items(self, dyn: Dict[str, Any], data: SubscriptionRecord):
        """获取动态条目列表。"""
        last = data.last
        items = dyn["items"]
        recent_ids = data.recent_ids
        known_ids = {x for x in ([last] + recent_ids) if x}
        new_items = []

        for item in items:
            if "modules" not in item:
                continue
            # 过滤置顶
            if (
                item["modules"].get("module_tag")
                and item["modules"]["module_tag"].get("text") == "置顶"
            ):
                continue

            if item["id_str"] in known_ids:
                break
            new_items.append(item)

        return new_items


    def _match_filter_regex(
        self, text: Optional[str], filter_regex: List[str], log_template: str
    ) -> bool:
        """检测文本是否命中过滤正则"""
        if not text or not filter_regex:
            return False

        for regex_pattern in filter_regex:
            try:
                if re.search(regex_pattern, text):
                    logger.info(log_template.format(regex_pattern=regex_pattern))
                    return True
            except re.error:
                logger.warning(f"无效的正则表达式: {regex_pattern}")
                continue

        return False


    def _parse_and_filter_dynamics(
        self, dyn: Dict[str, Any], data: SubscriptionRecord
    ) -> List[DynamicParseResult]:
        """
        解析并过滤动态。
        """
        filter_types = data.filter_types
        filter_regex = data.filter_regex
        uid = str(data.uid)
        items = self._get_dynamic_items(dyn, data)  # 不含last及置顶的动态列表
        result_list: List[DynamicParseResult] = []
        if not items:
            return result_list

        for item in items:
            dyn_id = item["id_str"]
            item_type = item.get("type")

            if item_type == "DYNAMIC_TYPE_FORWARD":
                result = self._handle_forward_dynamic(
                    item, dyn_id, uid, filter_types, filter_regex
                )
            elif item_type in ("DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_WORD"):
                result = self._handle_draw_or_word_dynamic(
                    item, dyn_id, uid, filter_types, filter_regex
                )
            elif item_type == "DYNAMIC_TYPE_AV":
                result = self._handle_video_dynamic(item, dyn_id, uid, filter_types)
            elif item_type == "DYNAMIC_TYPE_ARTICLE":
                result = self._handle_article_dynamic(item, dyn_id, uid, filter_types)
            else:
                # dyn_id记为None，避免未识别类型挤占正常动态缓存
                result = DynamicParseResult.skip(None, "unsupported type")

            if result.has_payload() and self._match_filter_regex(
                "\n".join(str(v or "") for v in (result.payload.title, result.payload.summary, result.payload.text)),
                filter_regex, "动态文本匹配过滤正则 {regex_pattern}。",
            ):
                result = DynamicParseResult.skip(dyn_id, "regex")
            result_list.append(result)

        return result_list


    def _handle_forward_dynamic(
        self,
        item: Dict,
        dyn_id: str,
        uid: str,
        filter_types: List[str],
        filter_regex: List[str],
    ) -> DynamicParseResult:
        """处理转发动态的过滤与渲染数据准备。"""
        try:
            is_forward_lottery = (
                item["orig"]["modules"]["module_dynamic"]["major"]["opus"]["summary"][
                    "rich_text_nodes"
                ][0].get("text")
                == "互动抽奖"
            )
        except (KeyError, TypeError):
            is_forward_lottery = False

        if "forward_lottery" in filter_types and is_forward_lottery:
            logger.info(f"转发互动抽奖在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "forward_lottery")

        if "forward" in filter_types:
            logger.info(f"转发类型在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "forward")

        try:
            content_text = item["modules"]["module_dynamic"]["desc"]["text"]
        except (TypeError, KeyError):
            content_text = ""

        if "lottery" in filter_types and re.search(
            r"恭喜.*等\d+位同学中奖，已私信通知，详情请点击抽奖查看。",
            content_text,
        ):
            logger.info(f"转发内容为抽奖在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "lottery")

        if self._match_filter_regex(
            content_text, filter_regex, "转发内容匹配正则 {regex_pattern}。"
        ):
            return DynamicParseResult.skip(dyn_id, "regex")

        render_data = self.renderer.build_render_data(item)
        render_data.uid = uid
        render_data.url = f"https://t.bilibili.com/{dyn_id}"
        render_data.qrcode = create_qrcode(render_data.url)

        render_forward = self.renderer.build_render_data(
            item.get("orig", {}), is_forward=True
        )
        if render_forward.image_urls:
            render_forward.image_urls = [render_forward.image_urls[0]]
        render_data.forward = render_forward.to_forward_payload()
        return DynamicParseResult.deliver(render_data, dyn_id)


    def _handle_draw_or_word_dynamic(
        self,
        item: Dict,
        dyn_id: str,
        uid: str,
        filter_types: List[str],
        filter_regex: List[str],
    ) -> DynamicParseResult:
        """处理图文/文字动态。"""
        if "draw" in filter_types:
            logger.info(f"图文类型在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "draw")

        major = item.get("modules", {}).get("module_dynamic", {}).get("major", {})
        if major.get("type") == "MAJOR_TYPE_BLOCKED":
            logger.info(f"图文动态 {dyn_id} 为充电专属。")
            return DynamicParseResult.skip(dyn_id, "major_blocked")

        opus = major.get("opus", {})
        summary = opus.get("summary", {})
        summary_text = summary.get("text", "")
        rich_nodes = summary.get("rich_text_nodes", [])
        first_node_text = rich_nodes[0].get("text") if rich_nodes else ""

        if first_node_text == "互动抽奖" and "lottery" in filter_types:
            logger.info(f"互动抽奖在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "lottery")

        if self._match_filter_regex(
            summary_text,
            filter_regex,
            f"图文动态 {dyn_id} 的 summary 匹配正则 '{{regex_pattern}}'。",
        ):
            return DynamicParseResult.skip(dyn_id, "regex")

        render_data = self.renderer.build_render_data(item)
        render_data.uid = uid
        return DynamicParseResult.deliver(render_data, dyn_id)


    def _handle_video_dynamic(
        self, item: Dict, dyn_id: str, uid: str, filter_types: List[str]
    ) -> DynamicParseResult:
        """处理视频动态。"""
        if "video" in filter_types:
            logger.info(f"视频类型在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "video")

        render_data = self.renderer.build_render_data(item)
        render_data.uid = uid
        return DynamicParseResult.deliver(render_data, dyn_id)


    def _handle_article_dynamic(
        self, item: Dict, dyn_id: str, uid: str, filter_types: List[str]
    ) -> DynamicParseResult:
        """处理专栏文章动态。"""
        if "article" in filter_types:
            logger.info(f"文章类型在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "article")

        major = item.get("modules", {}).get("module_dynamic", {}).get("major", {})
        if major.get("type") == "MAJOR_TYPE_BLOCKED":
            logger.info(f"文章 {dyn_id} 为充电专属。")
            return DynamicParseResult.skip(dyn_id, "major_blocked")

        render_data = self.renderer.build_render_data(item)
        render_data.uid = uid
        return DynamicParseResult.deliver(render_data, dyn_id)

