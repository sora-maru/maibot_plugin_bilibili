import asyncio
import base64
import uuid
from pathlib import Path
from typing import Any, Dict

from jinja2 import Environment, BaseLoader, select_autoescape

from ..log import logger

from ..core.constant import (
    BANNER_PATH,
    CARD_TEMPLATES,
    DEFAULT_TEMPLATE,
    get_template_path,
)
from ..core.models import RenderPayload
from ..core.utils import create_qrcode, image_to_base64, parse_rich_text


def load_template(style: str) -> str:
    """加载指定样式的模板内容"""
    path = get_template_path(style)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class Renderer:
    """
    负责将动态数据渲染成图片。
    """

    def __init__(self, context, cache_dir, rai: bool = True, style: str = DEFAULT_TEMPLATE):
        """
        初始化渲染器。
        """
        self.context = context
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._render_lock = asyncio.Lock()
        self.env = Environment(loader=BaseLoader(), autoescape=select_autoescape(default=True))
        self.rai = rai
        self.style = style
        # 预加载所有模板
        self._templates: Dict[str, str] = {}
        self._load_all_templates()

    def _load_all_templates(self):
        """预加载所有注册的模板"""
        for template_id in CARD_TEMPLATES:
            try:
                self._templates[template_id] = load_template(template_id)
            except Exception as e:
                logger.warning(f"加载模板 {template_id} 失败: {e}")

    def reload_templates(self):
        """重新加载所有模板（用于热更新）"""
        self._templates.clear()
        self._load_all_templates()

    def get_template(self, style: str | None = None) -> str:
        """获取指定样式的模板内容"""
        target_style = style or self.style
        if target_style not in self._templates:
            target_style = DEFAULT_TEMPLATE
        return self._templates.get(target_style, "")

    async def render_dynamic(self, payload: RenderPayload, style: str | None = None):
        """
        将渲染数据字典渲染成最终图片。
        这是该类的主要入口方法。
        """
        tmpl = self.get_template(style)
        context = payload.to_template_context()
        width = {"template_1": 700, "template_2": 630, "simple": 640}.get(style or self.style, 630)
        async with self._render_lock:
            try:
                html = await asyncio.to_thread(lambda: self.env.from_string(tmpl).render(**context))
                # With a one-pixel initial viewport a vertical scrollbar would steal
                # card width before the full-page capture, clipping the right edge.
                html = html.replace("</head>", "<style>html{scrollbar-width:none}::-webkit-scrollbar{display:none}</style></head>")
                result = await self.context.render.html2png(
                    html, selector="body", viewport={"width": width, "height": 1},
                    device_scale_factor=1.5, full_page=True, allow_network=True,
                    wait_until="networkidle", render_timeout_ms=45000,
                )
                if not isinstance(result, dict) or not result.get("image_base64"):
                    raise RuntimeError("宿主 HTML 渲染未返回图片；检查 MaiBot 浏览器渲染服务")
                path = self.cache_dir / f"card-{uuid.uuid4().hex}.png"
                data = base64.b64decode(result["image_base64"], validate=True)
                await asyncio.to_thread(path.write_bytes, data)
                if not await asyncio.to_thread(self._validate_image, str(path)):
                    path.unlink(missing_ok=True)
                    raise RuntimeError("渲染结果不是有效图片")
                return str(path)
            except Exception as exc:
                logger.warning("卡片渲染失败，改用文字和原图：%s", type(exc).__name__)
                return None

    @staticmethod
    def _validate_image(img_path: str) -> bool:
        """验证图片文件是否有效可读"""
        try:
            from PIL import Image

            with Image.open(img_path) as img:
                img.verify()
            return True
        except Exception as e:
            logger.warning(f"图片验证失败 {img_path}: {e}")
            return False

    @staticmethod
    def _build_base_payload(item: Dict[str, Any]) -> RenderPayload:
        author_module = item.get("modules", {}).get("module_author") or {}
        return RenderPayload(
            banner=image_to_base64(BANNER_PATH),
            name=str(author_module.get("name") or ""),
            avatar=str(author_module.get("face") or ""),
            pendant=str((author_module.get("pendant") or {}).get("image") or ""),
            type=str(item.get("type") or ""),
        )

    def _fill_video_payload(
        self, payload: RenderPayload, item: Dict[str, Any], is_forward: bool
    ) -> RenderPayload:
        archive = item["modules"]["module_dynamic"]["major"]["archive"]
        title = str(archive["title"])
        bv = str(archive["bvid"])
        cover_url = str(archive["cover"])
        desc = item.get("modules", {}).get("module_dynamic", {}).get("desc")
        topic = item.get("modules", {}).get("module_dynamic", {}).get("topic")
        content_text = (desc or {}).get("text")

        payload.title = title
        payload.image_urls = [cover_url]
        payload.text = (
            f"投稿了新视频<br>{parse_rich_text(desc, topic)}"
            if content_text
            else "投稿了新视频<br>"
        )
        if not is_forward:
            payload.url = f"https://www.bilibili.com/video/{bv}"
            payload.qrcode = create_qrcode(payload.url)
        return payload

    def _fill_opus_payload(
        self, payload: RenderPayload, item: Dict[str, Any], is_forward: bool
    ) -> RenderPayload:
        opus = item["modules"]["module_dynamic"]["major"]["opus"]
        summary = opus["summary"]
        jump_url = str(opus["jump_url"])
        topic = item["modules"]["module_dynamic"].get("topic")

        payload.summary = str(summary.get("text") or "")
        payload.text = parse_rich_text(summary, topic)
        payload.title = str(opus.get("title") or "")
        payload.image_urls = [str(pic["url"]) for pic in opus.get("pics", [])[:9]]
        if not is_forward:
            payload.url = f"https:{jump_url}" if jump_url.startswith("//") else jump_url
            payload.qrcode = create_qrcode(payload.url)
        return payload

    @staticmethod
    def _fill_forward_payload(
        payload: RenderPayload, item: Dict[str, Any]
    ) -> RenderPayload:
        desc = item.get("modules", {}).get("module_dynamic", {}).get("desc")
        topic = item.get("modules", {}).get("module_dynamic", {}).get("topic")
        content_text = (desc or {}).get("text")
        if content_text:
            payload.text = parse_rich_text(desc, topic)
        return payload

    def build_render_data(
        self, item: Dict[str, Any], is_forward: bool = False
    ) -> RenderPayload:
        """
        根据从B站API获取的单个动态项目，构建用于渲染的对象。
        is_forward: 标记是否正在处理转发动态
        """
        payload = self._build_base_payload(item)
        item_type = item.get("type")
        if item_type == "DYNAMIC_TYPE_AV":
            return self._fill_video_payload(payload, item, is_forward)
        if item_type in (
            "DYNAMIC_TYPE_DRAW",
            "DYNAMIC_TYPE_WORD",
            "DYNAMIC_TYPE_ARTICLE",
        ):
            return self._fill_opus_payload(payload, item, is_forward)
        if item_type == "DYNAMIC_TYPE_FORWARD":
            return self._fill_forward_payload(payload, item)
        return payload
