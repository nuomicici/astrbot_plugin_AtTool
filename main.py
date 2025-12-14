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
        # 匹配合法的 [at:123456]
        self.valid_at_pattern = re.compile(r'\[at:(\d+)\]')
        # 匹配疑似标签但格式错误的，用于除杂 (例如 [at:某人], [at:unknown])
        self.garbage_at_pattern = re.compile(r'\[at:[^\]]+\]')

    @filter.on_llm_request()
    async def inject_at_instruction(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        在 LLM 请求前注入 System Prompt。
        使用 XML 格式定义艾特协议。
        """
        instruction = (
            "\n\n"
            "<at_mention_protocol>\n"
            "    <description>协议用于在群聊中艾特(At)特定成员以引起注意。</description>\n"
            "    <workflow>\n"
            "        <step index='1'>判断是否需要艾特某人（如回复特定提问、提醒）。</step>\n"
            "        <step index='2'>检查是否已知目标成员的 user_id (QQ号)。</step>\n"
            "        <step index='3'>若未知，必须调用工具 `get_group_members` 获取成员列表。</step>\n"
            "        <step index='4'>获取 user_id 后，在回复文本中直接插入标签。</step>\n"
            "    </workflow>\n"
            "    <output_format>\n"
            "        <tag_syntax>[at:user_id]</tag_syntax>\n"
            "        <requirement>直接输出标签，不要使用 Markdown 链接或 @昵称。</requirement>\n"
            "    </output_format>\n"
            "    <examples>\n"
            "        <correct>好的 [at:123456] 我明白了。</correct>\n"
            "        <incorrect>@张三 , [at:张三]</incorrect>\n"
            "    </examples>\n"
            "</at_mention_protocol>\n"
        )
        req.system_prompt += instruction

    # 群成员查询工具 (无限制返回)
    @filter.llm_tool(name="get_group_members")
    async def get_group_members(self, event: AstrMessageEvent, keyword: str = "") -> str:
        """
        查询群成员列表。当需要艾特(@)某人但不知道其 user_id 时调用此工具。
        
        Args:
            keyword(string): 可选。搜索关键词（昵称/群名片/QQ号）。如果不填则返回所有成员。
        """
        start_time = time.time()
        
        # 环境检查
        group_id = event.get_group_id()
        if not group_id:
            return json.dumps({"status": "error", "message": "当前不在群聊环境中，无法查询成员。"}, ensure_ascii=False)

        if not isinstance(event, AiocqhttpMessageEvent):
            return json.dumps({"status": "error", "message": "当前平台不支持获取群成员列表。"}, ensure_ascii=False)

        try:
            # 获取原始数据
            raw_members = await event.bot.api.call_action('get_group_member_list', group_id=group_id)
            if not raw_members:
                return json.dumps({"status": "error", "message": "获取成员列表为空或权限不足。"}, ensure_ascii=False)

            # 数据清洗与格式化
            formatted_members = []
            
            for m in raw_members:
                user_id = str(m.get("user_id", ""))
                nickname = m.get("nickname", "")
                card = m.get("card", "") # 群名片
                role = m.get("role", "member") # owner, admin, member
                
                # 搜索过滤
                search_content = f"{user_id}{nickname}{card}"
                if keyword and keyword not in search_content:
                    continue

                # 角色中文映射
                role_map = {"owner": "群主", "admin": "管理员", "member": "成员"}
                role_cn = role_map.get(role, "成员")

                formatted_members.append({
                    "user_id": user_id,
                    "nickname": nickname,
                    "group_card": card if card else "无",
                    "role": role_cn
                })

            result_members = formatted_members

            output_data = {
                "status": "success",
                "group_id": group_id,
                "count": len(result_members),
                "members": result_members
            }

            logger.debug(f"群成员查询成功，耗时 {time.time() - start_time:.2f}s，返回 {len(result_members)} 人")
            # 返回 JSON 格式
            return json.dumps(output_data, ensure_ascii=False, indent=2)

        except Exception as e:
            logger.error(f"查询群成员失败: {e}")
            return json.dumps({"status": "error", "message": f"系统异常: {str(e)}"}, ensure_ascii=False)

    # 消息处理与除杂
    @filter.on_decorating_result(priority=2)
    async def process_at_tags(self, event: AstrMessageEvent):
        """
        拦截消息：
        1. 将 [at:123456] 转换为真实 At 组件。
        2. 清除格式错误的 [at:xxx] 标签（除杂）。
        """
        result = event.get_result()
        if not result or not result.chain:
            return

        # 快速检查是否有相关字符，避免无意义循环
        has_tag = False
        for comp in result.chain:
            if isinstance(comp, Plain) and "[at:" in comp.text:
                has_tag = True
                break
        
        if not has_tag:
            return

        new_chain: List[BaseMessageComponent] = []

        for comp in result.chain:
            if isinstance(comp, Plain):
                text = comp.text
                
                last_idx = 0
                # 查找所有合法的 [at:数字]
                for match in self.valid_at_pattern.finditer(text):
                    start, end = match.span()

                    # 处理标签前的文本
                    if start > last_idx:
                        pre_text = text[last_idx:start]
                        # 移除那些长得像标签但不是合法ID的文本，例如 [at:unknown]
                        pre_text = self.garbage_at_pattern.sub('', pre_text)
                        if pre_text:
                            new_chain.append(Plain(pre_text))

                    target_id = match.group(1)
                    
                    # 插入真实组件
                    new_chain.append(At(qq=target_id))

                    last_idx = end

                # 处理剩余文本
                if last_idx < len(text):
                    remain_text = text[last_idx:]
                    # 同样对剩余文本进行除杂
                    remain_text = self.garbage_at_pattern.sub('', remain_text)
                    if remain_text:
                        new_chain.append(Plain(remain_text))
            else:
                new_chain.append(comp)

        result.chain = new_chain
