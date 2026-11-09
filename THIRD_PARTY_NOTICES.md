# 来源与移植记录

本插件包含 [Soulter/astrbot_plugin_bilibili](https://github.com/Soulter/astrbot_plugin_bilibili) 的源码与资源。上游 metadata 的作者为 **Flartiny & Soulter**；其他贡献者与模板作者的署名保留在原始资源和项目历史中。

复用部分：Bilibili / Bangumi 客户端，动态模型，过滤及直播时长算法，扫码登录和凭据维护，订阅存储与变更逻辑，五个查询工具及其参数，头像处理，以及 assets 内的原始卡片模板和图片。

移植修改：移除 AstrBot 导入；用 MaiBot SDK 实现配置、命令、Hook、API、工具注册、图片渲染和生命周期；新增投递待确认状态、聊天流适配、网页管理和回归测试；修正个人提醒对订阅条件的覆盖、标题过滤及部分字段兼容处理。

2026-11-09，维护者 sora-maru 表示已向上游作者确认原插件采用 **AGPL-3.0**。本项目据该确认将授权声明更新为 **AGPL-3.0-only**，完整许可证见 [LICENSE](LICENSE)；未取得“或任何后续版本”的说明，因此未声明为 AGPL-3.0-or-later。此记录来源为维护者提供的确认，并非引用上游仓库中已有的 LICENSE 文件。

上游源码、模板内的作者署名和版权声明均保留。AGPL-3.0 声明不替代第三方素材原有的独立授权，也不扩大上游或维护者对第三方素材的授权范围；存在独立授权条件的内容仍遵循其原有条款。Python 依赖保留各自的许可证。

本移植版仓库为 [sora-maru/maibot_plugin_bilibili](https://github.com/sora-maru/maibot_plugin_bilibili)，维护者为 sora-maru；上游来源以本文开头的链接为准。

精确源码版本见 `VALIDATION.md`。
