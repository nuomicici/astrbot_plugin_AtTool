<div align="center">
<img style="width:70%" src="https://count.getloli.com/@astrbot_plugin_AtTool?name=astrbot_plugin_AtTool&theme=booru&padding=5&offset=0&align=top&scale=1&pixelated=1&darkmode=auto" alt=":name">

# 好感度/关系管理插件

<div align="left" style="width:70%">


让你的 AstrBot 拥有“真正”艾特群成员的能力。

本插件通过 赋予 LLM 获取群成员列表和发送真实 At 消息（非纯文本）的能力。解决了 LLM 无法知道群友 QQ 号从而无法艾特的问题。

## 简介

在默认情况下，LLM 只能输出 "@张三" 这样的纯文本，无法触发 QQ 的真实艾特提醒。
本插件提供了一套完整的解决方案：
1.  **信息获取**：LLM 可以通过工具查询群成员列表（包含昵称、群名片、QQ号）。
2.  **意图识别**：当用户要求“艾特某人”或“提醒某人”时，LLM 自动查找 ID。
3.  **真实艾特**：通过中间件将 LLM 输出的标签转换为真实的 `At` 消息组件。

## 功能特性

- **智能查找 ID**：LLM 自动调用 `get_info_to_at` 获取群成员信息，可以不用群成员查询了。
- **真实 At 组件**：拦截消息流，将 `[mention:12345]` 标签无缝转换为真实的 QQ 艾特消息。
- **Token 节省**：针对 LLM 优化的紧凑型 JSON 返回格式，大幅减少 Token 消耗。
- **多名匹配**：自动聚合群名片、昵称等信息，提高 LLM 找人的准确率。

### 安装步骤
- 直接在astrbot的插件市场搜索“糯米茨”找到目标插件，点击安装，等待完成即可

- 也可以克隆源码到插件文件夹：

```bash
# 克隆仓库到插件目录
cd /AstrBot/data/plugins
git clone https://github.com/nuomicici/astrbot_plugin_AtTool

# 控制台重启AstrBot
```

## 使用示例

安装完成后，无需额外配置，直接在群聊中与 Bot 对话即可。

**场景 1：直接艾特**
> **用户**：@Bot 帮我艾特一下张三，喊他上线打游戏。
>
> **Bot (后台动作)**：
> 1. 调用 `get_info_to_at` 获取群成员列表。
> 2. 在列表中找到“张三”对应的 QQ 号 (例如 10086)。
> 3. 调用 `mention_user(user_id="10086")`。
>
> **Bot (实际回复)**：@张三 喊他上线打游戏。

**场景 2：模糊指代**
> **用户**：@Bot 提醒一下群主。
>
> **Bot (后台动作)**：
> 1. 调用 `get_info_to_at`。
> 2. 识别 `role` 为 `owner` 的成员 ID。
> 3. 调用 `mention_user`。
>
> **Bot (实际回复)**：@群主 (真实艾特)


## 注意事项

1.  **平台限制**：本插件依赖 `AiocqhttpMessageEvent`，目前**仅支持 QQ 平台**（通过 OneBot V11 协议，如 Napcat[也就测了这个] 等）。
2.  **群聊限制**：获取群成员列表功能仅在群聊环境中有效，私聊无法使用。
3.  **权限要求**：Bot 账号需要有获取群成员列表的权限（通常默认都有）。

## 更新日志
### 2025.12.07
- 心累，稍微修改了一下工具描述，让LLM可以更加明确的调用工具。作为工具而不是prompt注入，能让LLM可以更灵活使用（大概）
### 2025.12.06
- 修复了必须添加群成员查询才能用的bug

---

**作者**: 糯米茨  
**联系方式**: 
- [GitHub Issues](https://github.com/nuomicici/astrbot_plugin_GroupMemberQuery/issues)  
- [QQ](https://qm.qq.com/q/wMGXYfKKoS)
