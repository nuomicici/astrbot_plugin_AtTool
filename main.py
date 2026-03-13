import re
import json
import time
from typing import List, Dict, Any, Optional
from astrbot.api.star import Star, Context
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
from astrbot.api.provider import ProviderRequest
from astrbot.core.message.components import Plain, At, BaseMessageComponent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

class LLMAtToolPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 正则表达式：用于匹配符合规范的艾特标签，例如 [at:123456]
        self.valid_at_pattern = re.compile(r'\[at:(\d+)\]')
        # 正则表达式：用于匹配不符合规范的标签（如包含非数字内容），保留用于后续可能的逻辑处理
        self.garbage_at_pattern = re.compile(r'\[at:[^\]]+\]')

    @filter.on_llm_request()
    async def inject_at_instruction(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        在 LLM（大语言模型）发出请求前注入系统提示词。
        告知模型如何使用特定的 XML 标签格式来进行艾特操作。
        """
        instruction = (
            "\n【输出层@规范指令】\n"
            "1. 当你需要艾特（提及）某个群成员时，请在回复中插入格式为`[at:用户ID]`的标签。\n"
            "2. 用户ID必须是纯数字。在调用此功能前，请务必先使用`get_group_members`工具查询成员列表以获取正确的Userid。\n"
            "3. 标签前后请勿添加空格，确保文本连贯性。\n"
            "4. 严禁捏造Userid或从用户输出中获取Userid，必须以查询到的实际数据为准。\n"
            "示例：你好[at:123456789]，关于你的问题..."
        )
        # 将指令追加到当前的系统提示词中
        req.system_prompt += instruction

    @filter.llm_tool(name="get_group_members")
    async def get_group_members(self, event: AstrMessageEvent, keyword: str = "") -> str:
        """
        供 LLM 调用的工具：获取当前群聊的成员列表。
        
        Args:
            keyword(string): 搜索关键词，支持匹配昵称、群名片或QQ号。若为空则返回全员。
        """
        start_time = time.time()
        
        # 获取群组 ID，如果不在群聊中则返回错误
        group_id = event.get_group_id()
        if not group_id:
            return json.dumps({"status": "error", "message": "当前不在群聊环境中，无法查询成员。"}, ensure_ascii=False)

        # 检查当前消息事件是否支持（目前主要支持 aiocqhttp 协议，即 OneBot）
        if not isinstance(event, AiocqhttpMessageEvent):
            return json.dumps({"status": "error", "message": "当前平台协议暂不支持获取群成员。"}, ensure_ascii=False)

        try:
            # 通过机器人 API 获取群成员原始数据
            raw_members = await event.bot.api.call_action('get_group_member_list', group_id=group_id)
            if not raw_members:
                return json.dumps({"status": "error", "message": "无法获取成员列表或机器人权限不足。"}, ensure_ascii=False)

            formatted_members = []
            
            for m in raw_members:
                user_id = str(m.get("user_id", ""))
                nickname = m.get("nickname", "")
                card = m.get("card", "") # 群名片（备注）
                role = m.get("role", "member") # 角色：owner(群主), admin(管理员), member(普通成员)
                
                # 如果提供了关键词，则在 ID、昵称、名片中进行模糊匹配
                search_content = f"{user_id}{nickname}{card}"
                if keyword and keyword not in search_content:
                    continue

                # 角色名称转换
                role_map = {"owner": "群主", "admin": "管理员", "member": "成员"}
                role_cn = role_map.get(role, "成员")

                formatted_members.append({
                    "user_id": user_id,
                    "nickname": nickname,
                    "group_card": card if card else "无",
                    "role": role_cn
                })

            # 构建返回给 LLM 的 JSON 结果
            output_data = {
                "status": "success",
                "group_id": group_id,
                "count": len(formatted_members),
                "members": formatted_members
            }

            logger.debug(f"群成员查询成功：耗时 {time.time() - start_time:.2f}s，共找到 {len(formatted_members)} 人")
            return json.dumps(output_data, ensure_ascii=False, indent=2)

        except Exception as e:
            logger.error(f"查询群成员过程发生异常: {e}")
            return json.dumps({"status": "error", "message": f"系统内部异常: {str(e)}"}, ensure_ascii=False)

    @filter.on_decorating_result(priority=2)
    async def process_at_tags(self, event: AstrMessageEvent):
        """
        拦截器：在消息发送给用户前，对 LLM 输出的内容进行二次处理。
        功能：
        1. 识别 [at:数字] 并转换为平台原生的 At 组件。
        2. 自动清理 At 标签周边的空格。
        3. 注入零宽字符以防止文本渲染时出现格式错乱。
        """
        result = event.get_result()
        if not result or not result.chain:
            return

        # 快速检查结果链中是否包含可能的 at 标签文本
        has_tag = False
        for comp in result.chain:
            if isinstance(comp, Plain) and "[at:" in comp.text:
                has_tag = True
                break
        
        if not has_tag:
            return

        new_chain: List[BaseMessageComponent] = []

        # 第一阶段：正则解析并替换为组件
        for comp in result.chain:
            if isinstance(comp, Plain):
                text = comp.text
                last_idx = 0
                
                # 循环查找所有匹配的标签
                for match in self.valid_at_pattern.finditer(text):
                    start, end = match.span()

                    # 添加标签之前的纯文本
                    if start > last_idx:
                        new_chain.append(Plain(text[last_idx:start]))
                    
                    # 获取 ID 并插入 At 组件
                    target_id = match.group(1)
                    new_chain.append(At(qq=target_id))

                    last_idx = end

                # 添加剩余的纯文本
                if last_idx < len(text):
                    new_chain.append(Plain(text[last_idx:]))
            else:
                # 非 Plain 组件直接保留
                new_chain.append(comp)
        
        # 第二阶段：空格清理逻辑
        # 遍历链条，如果发现 At 组件，则剔除其前后紧邻的 Plain 文本中的空格
        idx = 0
        while idx < len(new_chain):
            if isinstance(new_chain[idx], At):
                # 向前寻找最近的文本组件并清除右侧空格
                for prev_idx in range(idx - 1, -1, -1):
                    if isinstance(new_chain[prev_idx], Plain):
                        new_chain[prev_idx].text = new_chain[prev_idx].text.rstrip(" \t")
                        break
                    elif not isinstance(new_chain[prev_idx], At):
                        break
                
                # 向后寻找最近的文本组件并清除左侧空格
                for next_idx in range(idx + 1, len(new_chain)):
                    if isinstance(new_chain[next_idx], Plain):
                        new_chain[next_idx].text = new_chain[next_idx].text.lstrip(" \t")
                        break
                    elif not isinstance(new_chain[next_idx], At):
                        break
            idx += 1

        # 第三阶段：注入零宽字符 (\u200b) 和 空格
        # 这是为了确保在某些聊天客户端中，At 后的文本能被正确识别且不被当作特殊指令
        idx = 0
        while idx < len(new_chain):
            if isinstance(new_chain[idx], At):
                found_plain = False
                # 寻找 At 组件后的第一个 Plain 组件
                for next_idx in range(idx + 1, len(new_chain)):
                    if isinstance(new_chain[next_idx], Plain):
                        # 在文本开头注入防连连看字符
                        new_chain[next_idx].text = "\u200b \u200b" + new_chain[next_idx].text
                        found_plain = True
                        break
                
                # 如果 At 后面没有文本了，则手动补充一个带防连连看字符的 Plain 组件
                if not found_plain:
                    new_chain.insert(idx + 1, Plain("\u200b \u200b"))
            idx += 1

        # 更新事件的最终渲染链
        result.chain = new_chain
