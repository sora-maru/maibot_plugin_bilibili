# 来源与移植记录

本插件包含 [Soulter/astrbot_plugin_bilibili](https://github.com/Soulter/astrbot_plugin_bilibili) 的源码与资源。上游 metadata 的作者为 **Flartiny & Soulter**；其他贡献者与模板作者的署名保留在原始资源和项目历史中。

复用部分：Bilibili / Bangumi 客户端，动态模型，过滤及直播时长算法，扫码登录和凭据维护，订阅存储与变更逻辑，五个查询工具及其参数，头像处理，以及 assets 内的原始卡片模板和图片。

移植修改：移除 AstrBot 导入；用 MaiBot SDK 实现配置、命令、Hook、API、工具注册、图片渲染和生命周期；新增投递待确认状态、聊天流适配、网页管理和回归测试；修正个人提醒对订阅条件的覆盖、标题过滤及部分字段兼容处理。

本地取得的上游快照未包含顶层 LICENSE 文件，未擅自为上游源码和资源追加新的开源授权。清单中的 `LicenseRef-Upstream-Unspecified` 反映此状态。相关依赖保留各自的授权条件。本移植版仓库为 [sora-maru/maibot_plugin_bilibili](https://github.com/sora-maru/maibot_plugin_bilibili)，维护者为 sora-maru；本插件尚未提交到插件市场。上游来源以本文开头的链接为准。

精确源码版本见 `VALIDATION.md`。
