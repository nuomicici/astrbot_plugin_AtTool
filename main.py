import re
import json
import time
from typing import List, Dict, Any, Optional
from astrbot.api.star import Star, register, Context
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
from astrbot.core.message.components import Plain, At, BaseMessageComponent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

@register("llm_at_tool", "YourName", "智能群成员艾特工具", "1.1.0")
class LLMAtToolPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 匹配 [mention:123456] 格式
        self.mention_pattern = re.compile(r'\[mention:(\d+)\]')

    @filter.llm_tool(name="search_group_member")
    async def search_group_member(self, event: AstrMessageEvent, keyword: str) -> str:
        """
        根据关键词搜索群成员。
        当用户想要艾特(@)某人，但你不知道那个人的 user_id 时，请先调用此工具进行搜索。
        
        Args:
            keyword (str): 搜索关键词。可以是昵称、群名片、QQ号或部分名字。
            
        Returns:
            JSON字符串，包含匹配到的成员列表（最多返回10个最匹配的结果）。
        """
        # 1. 环境检查
        if not isinstance(event, AiocqhttpMessageEvent):
            return json.dumps({"error": "此功能仅支持QQ群聊环境"})
        
        group_id = event.get_group_id()
        if not group_id:
            return json.dumps({"error": "无法获取群号，请确保在群聊中使用"})

        # 2. 获取群成员列表 (这一步在Python侧处理，不消耗LLM Token)
        try:
            # 调用 OneBot API 获取列表
            full_list = await event.bot.api.call_action('get_group_member_list', group_id=group_id)
            if not full_list:
                return json.dumps({"result": [], "msg": "群成员列表为空或获取失败"})
        except Exception as e:
            logger.error(f"获取群成员失败: {e}")
            return json.dumps({"error": f"API调用异常: {str(e)}"})

        # 3. Python侧进行过滤 (核心优化点)
        keyword_str = str(keyword).lower().strip()
        matched_members = []
        
        for member in full_list:
            user_id = str(member.get('user_id', ''))
            nickname = str(member.get('nickname', '')).lower()
            card = str(member.get('card', '')).lower()
            role = member.get('role', 'member')
            
            # 匹配逻辑：QQ号精确匹配 或 昵称/名片包含关键词
            if (keyword_str == user_id) or (keyword_str in nickname) or (keyword_str in card):
                matched_members.append({
                    "user_id": user_id,
                    "name": card if card else nickname, # 优先显示群名片
                    "role": role,
                    "match_reason": "id" if keyword_str == user_id else "name"
                })

        # 4. 结果截断 (防止返回太多Token)
        # 如果完全匹配QQ号，通常只有一个，直接返回
        # 如果是名字匹配，限制返回前10个，防止搜"a"出来几百人
        limit = 10
        result_count = len(matched_members)
        final_result = matched_members[:limit]

        response_data = {
            "search_keyword": keyword,
            "total_match": result_count,
            "members": final_result,
            "tips": "如果找到了目标，请使用 mention_user 工具传入 user_id。如果没找到，请询问用户更准确的名字。" if result_count > 0 else "未找到匹配成员"
        }

        return json.dumps(response_data, ensure_ascii=False)

    @filter.llm_tool(name="mention_user")
    async def mention_user(self, event: AstrMessageEvent, user_id: str) -> str:
        """
        生成艾特(At)标签。
        只有在你知道确切的 user_id 后才能调用此工具。
        
        Args:
            user_id (str): 目标用户的数字ID (QQ号)。
            
        Returns:
            str: 一个特殊的标记字符串，系统会自动将其转换为艾特消息。
        """
        # 这是一个简单的格式化工具，LLM输出这个字符串后，会被下方的 process_at_tags 捕获
        return f"[mention:{user_id}]"

    @filter.on_decorating_result(priority=2)
    async def process_at_tags(self, event: AstrMessageEvent):
        """
        结果装饰器：拦截 LLM 的回复，将 [mention:123] 替换为真实的 At 组件。
        """
        result = event.get_result()
        if not result or not result.chain:
            return

        # 快速检查是否有标记，避免无意义的循环
        has_tag = False
        for comp in result.chain:
            if isinstance(comp, Plain) and "[mention:" in comp.text:
                has_tag = True
                break
        
        if not has_tag:
            return

        new_chain: List[BaseMessageComponent] = []
        
        for comp in result.chain:
            if isinstance(comp, Plain):
                text = comp.text
                last_idx = 0
                # 使用正则查找所有标记
                for match in self.mention_pattern.finditer(text):
                    start, end = match.span()
                    
                    # 添加标记前的文本
                    if start > last_idx:
                        new_chain.append(Plain(text[last_idx:start]))
                    
                    target_id = match.group(1)
                    
                    # 插入 At 组件
                    # 注意：某些平台可能需要前后加空格防止解析错误，这里加了零宽空格美化
                    new_chain.append(At(qq=target_id))
                    
                    last_idx = end
                
                # 添加标记后的剩余文本
                if last_idx < len(text):
                    new_chain.append(Plain(text[last_idx:]))
            else:
                # 非文本组件直接保留（如图片等）
                new_chain.append(comp)

        # 替换原消息链
        result.chain = new_chain
