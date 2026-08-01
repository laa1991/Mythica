# 架构治理：从上帝类到模块化

## 问题

桌面端 GUI 入口 `ui.py` 最初是一个上帝类——所有 UI 逻辑、数据处理、AI 管线、对话引擎、群聊引擎、文件监控全塞在一个文件里。随着功能增长，它膨胀到 **11,887 行**。

改动任何一个功能都要在这个文件里上下翻。多个功能域的代码混在一起，新人（包括三个月后的自己）无法快速定位"这段逻辑在哪"。合并冲突是常态。

## 方案

按功能域拆成 Mixin 类，每个 Mixin 负责一个独立的领域。`MythicaApp` 通过多重继承组合所有 Mixin：

```
MythicaApp(
    _MythicaDataToolsMixin,       # 数据处理（42 个 @staticmethod）
    _MythicaWidgetFactoryMixin,   # UI 控件工厂
    _MythicaHouseholdEditorMixin, # 家庭编辑
    _MythicaGameTuningMixin,      # 游戏设置
    _MythicaSettingsTabsMixin,    # API/连接设置
    _MythicaInnerVoiceMixin,      # 内心独白管道
    _MythicaMonitorMixin,         # 文件监控 + 日志
    _MythicaAutoDialogueMixin,    # 自动对话
    _MythicaGroupChatMixin,       # 群聊引擎
    _MythicaStoryOfflineMixin,    # 离线故事生成
    _MythicaCharacterCardsMixin,  # 角色卡管理
    _MythicaQualityMonitorMixin,  # 质量监控
)
```

**为什么用 Mixin 而不是拆成独立模块：** 所有 Mixin 通过 MRO 共享 `self`——方法之间可直接互调，无需显式传参或维护跨模块引用。若拆为独立模块，每个功能域需要显式传入共享依赖（config、recorder、logger 等），导致函数签名膨胀。代价是 Mixin 之间通过 `self` 隐式耦合，新增方法时需确认依赖的 Mixin 已加载。

拆分过程中发现了大量被 MRO 遮蔽的死代码——同一个方法在两处各有一份拷贝，MRO 决定了哪个生效，另一个是永远不执行的死副本。配套写了 `verify_mixin_shadowing.py` 校验脚本防止复发。

## 结果

`ui.py` 从 **11,887 行 → 631 行**（-95%）。12 个 Mixin 各司其职，每个文件可以独立阅读和修改。游戏端同样走了一轮拆分：`my_script.py` 从 6,631 行拆为 4,404 行核心管线 + 20+ 个功能模块。

核心经验：拆分不是一次完成的。第一轮拆了 4 个 Mixin，之后每轮按需再拆——聊天从对话里拆出来、群聊从聊天里拆出来、角色卡从故事里拆出来。**渐进式重构的节奏是"每次只拆一个维度，拆完跑测试，确认无回归再拆下一个"。**
