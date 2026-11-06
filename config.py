from maibot_sdk import Field, PluginConfigBase


class PluginSettings(PluginConfigBase):
    __ui_label__ = "基础设置"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="启用或停用哔哩哔哩插件", json_schema_extra={"label": "启用插件"})
    config_version: str = Field(default="1.0.0", description="插件配置格式版本，由插件维护", json_schema_extra={"label": "配置版本", "disabled": True})


class AccessSettings(PluginConfigBase):
    __ui_label__ = "权限管理"
    __ui_order__ = 1

    admin_users: list[str] = Field(default_factory=list, description="填写拥有扫码登录和全局订阅管理权限的管理员 QQ 号", json_schema_extra={"label": "插件管理员"})
    group_admin_can_manage: bool = Field(default=True, description="允许群主及群管理员管理本群订阅", json_schema_extra={"label": "允许群管理员管理订阅"})
    allow_member_subscribe: bool = Field(default=False, description="允许普通群成员修改本群订阅；个人 @ 提醒始终允许", json_schema_extra={"label": "允许普通群成员管理订阅"})


class BiliSettings(PluginConfigBase):
    __ui_label__ = "账号与网络"
    __ui_order__ = 2

    sessdata: str = Field(default="", description="可选，填写 B站 SESSDATA；优先使用扫码登录保存的凭据", json_schema_extra={"label": "哔哩哔哩登录凭据"})
    proxy: str = Field(default="", description="B站请求使用的代理地址，留空则直接连接", json_schema_extra={"label": "网络代理地址"})
    bangumi_token: str = Field(default="", description="可选，填写番组计划（Bangumi）访问令牌", json_schema_extra={"label": "番组计划访问令牌"})


class PushSettings(PluginConfigBase):
    __ui_label__ = "订阅推送"
    __ui_order__ = 3

    interval_secs: int = Field(default=300, ge=30, description="同一 UP 主两次检测之间的间隔，单位为秒", json_schema_extra={"label": "动态检测间隔（秒）"})
    task_gap_secs: float = Field(default=20, ge=1, description="相邻两个 UP 主请求之间的最小间隔，单位为秒", json_schema_extra={"label": "请求间隔（秒）"})
    dynamic_limit: int = Field(default=5, ge=1, le=20, description="每条订阅每轮最多推送的动态数量", json_schema_extra={"label": "单轮动态推送上限"})
    recent_dynamic_cache: int = Field(default=64, ge=16, le=512, description="每条订阅保留的动态去重记录数", json_schema_extra={"label": "动态去重记录数量"})
    render_as_image: bool = Field(default=True, description="将动态渲染为图片卡片；渲染不可用时发送文字和原图", json_schema_extra={"label": "启用图片卡片"})
    renderer_template: str = Field(default="template_2", description="经典风格填写 template_1，B站粉风格填写 template_2，简约风格填写 simple；也可在订阅管理页选择", json_schema_extra={"label": "卡片渲染样式", "hint": "共三种：template_1 经典渐变、template_2 B站粉（默认）、simple 简约白底。订阅管理页提供中文下拉选项。"})
    forward: bool = Field(default=False, description="以合并转发发送推送内容；@ 提醒在转发外发送", json_schema_extra={"label": "使用合并转发推送"})
    plain_push_template: str = Field(default="", description="留空使用默认格式。可用变量：{name} 作者、{uid} 用户编号、{action} 动作、{title} 标题、{text} 正文、{url} 链接", json_schema_extra={"label": "文字推送模板", "x-widget": "textarea", "rows": 5, "hint": "仅文字模式或卡片渲染失败时使用。留空即可，无预设名称需要填写；可直接输入含变量的多行文本。", "placeholder": "📣 {name} {action}\n{title}\n{text}\n{url}"})
    plain_push_forward_template: str = Field(default="", description="留空使用默认格式。可用变量：{name} 原作者、{uid} 用户编号、{title} 标题、{text} 原文、{url} 链接", json_schema_extra={"label": "转发原文模板", "x-widget": "textarea", "rows": 4, "hint": "用于文字模式下被转发的B站原动态，和QQ合并转发开关无关。留空使用默认格式。", "placeholder": "原作者：{name}\n{title}\n{text}\n{url}"})
    reconnect_silent: bool = Field(default=False, description="距上次成功推送超过6小时，启动时静默一轮并跳过积压动态", json_schema_extra={"label": "重连时静默处理积压"})


class ParseSettings(PluginConfigBase):
    __ui_label__ = "链接解析"
    __ui_order__ = 4

    enable_parse_BV: bool = Field(default=True, description="自动识别并解析 BV 号和视频链接", json_schema_extra={"label": "自动解析视频链接"})
    enable_parse_miniapp: bool = Field(default=True, description="解析适配器保留的 B站小程序分享链接", json_schema_extra={"label": "解析小程序分享"})
    cooldown_secs: int = Field(default=60, ge=0, description="同一聊天中同一视频重复解析的冷却时间，单位为秒", json_schema_extra={"label": "重复解析冷却时间（秒）"})


class AISettings(PluginConfigBase):
    __ui_label__ = "智能摘要"
    __ui_order__ = 5

    enable_summary: bool = Field(default=False, description="动态推送后额外调用模型生成摘要", json_schema_extra={"label": "启用动态智能摘要"})
    model: str = Field(default="", description="MaiBot 1.3.16 留空使用 utils 辅助任务；填写 replyer 使用回复任务，或填写已在 MaiBot 模型配置中定义的模型名称", json_schema_extra={"label": "摘要使用的模型", "hint": "复用 MaiBot 已配置的模型，无需另填接口或密钥。留空＝utils；replyer＝回复任务；指定单个模型请填写模型配置中的 name，而非服务商的模型标识符。", "placeholder": "留空使用辅助任务，或填写 replyer"})
    include_images: bool = Field(default=False, description="将动态原图一并提供给摘要模型，需要所选模型支持视觉", json_schema_extra={"label": "摘要包含图片内容"})
    summary_prompt: str = Field(default="", description="留空使用默认提示词，可使用 {content} 插入动态内容", json_schema_extra={"label": "摘要提示词", "x-widget": "textarea", "rows": 7, "hint": "留空即可。自定义时请保留 {content}，插件会将它替换为动态正文。摘要只处理动态提供的内容，不会自动读取视频正文。", "placeholder": "请简洁总结以下B站动态，先用一句话概括，再列2至4条要点。不要编造未提供的信息。\n{content}"})


class BilibiliConfig(PluginConfigBase):
    plugin: PluginSettings = Field(default_factory=PluginSettings)
    access: AccessSettings = Field(default_factory=AccessSettings)
    bili: BiliSettings = Field(default_factory=BiliSettings)
    push: PushSettings = Field(default_factory=PushSettings)
    parse: ParseSettings = Field(default_factory=ParseSettings)
    ai: AISettings = Field(default_factory=AISettings)
