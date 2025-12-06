import re
import json
import time
from typing import List, Dict, Any, Optional
from astrbot.api.star import Star, Context
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
from astrbot.core.message.components import Plain, At, BaseMessageComponent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

class LLMAtToolPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 匹配 @:123456 格式
        self.mention_pattern = re.compile(r'@:(\d+)')

    @filter.llm_tool(name="search_member_for_at")
    async def search_member_for_at(self, event: AstrMessageEvent, keyword: str) -> str:
        """
        当用户想要“提醒”、“@”、“找一下”、“问一下”某人，或者需要获取某人的QQ号(user_id)时调用此工具。
        
        Args:
            keyword (str): 想要查找的群成员的昵称、群名片或部分名字。
            
        Returns:
            str: 包含匹配到的成员信息的JSON数据，以及给LLM的格式化输出指令。
        """
        # 1. 环境检查
        group_id = event.get_group_id()
        if not group_id:
            return "错误：此功能仅在群聊中可用。"
        
        # 目前仅适配 aiocqhttp (OneBot)
        if not isinstance(event, AiocqhttpMessageEvent):
            return "错误：当前平台不支持获取群成员列表。"

        # 2. 获取群成员列表 (API调用)
        try:
            # 注意：如果群人数过多，这里可能会慢，实际生产环境建议做缓存
            member_list = await event.bot.api.call_action('get_group_member_list', group_id=group_id)
        except Exception as e:
            logger.error(f"获取群成员失败: {e}")
            return f"系统错误：无法获取群成员列表 ({e})"

        if not member_list:
            return "未获取到任何群成员信息。"

        # 3. Python侧进行模糊搜索 (节省Token的关键步骤)
        # 不把几千人的列表发给LLM，而是只返回匹配的人
        keyword = str(keyword).strip().lower()
        matched_members = []
        
        for member in member_list:
            user_id = str(member.get('user_id', ''))
            nickname = str(member.get('nickname', '')).lower()
            card = str(member.get('card', '')).lower()
            
            # 简单的包含匹配，也可以换成 fuzzywuzzy 等库
            if keyword in nickname or keyword in card or keyword == user_id:
                matched_members.append({
                    "user_id": user_id,
                    "name": card if card else nickname, # 优先显示群名片
                    "role": member.get('role', 'member')
                })

        # 4. 限制返回数量，防止Token溢出
        matched_members = matched_members[:10] 

        if not matched_members:
            return f"未找到包含关键词 '{keyword}' 的群成员。请尝试使用更准确的名字。"

        # 5. 构造返回给 LLM 的数据
        # 重点：在返回结果中直接注入 System Tip，强制 LLM 使用指定格式
        result_data = {
            "status": "success",
            "matched_users": matched_members,
            "instruction": "请从上述列表中选择最匹配的用户。在你的回复中，必须严格使用 '@:user_id' 的格式来提及该用户。例如：'@:123456'。不要使用 [At:...] 或 @昵称。"
        }

        return json.dumps(result_data, ensure_ascii=False)

    @filter.on_decorating_result(priority=2)
    async def process_at_tags(self, event: AstrMessageEvent):
        """
        拦截 LLM 的回复，将文本中的 @:123456 替换为真实的 At 组件。
        """
        result = event.get_result()
        if not result or not result.chain:
            return

        # 快速检查是否有需要处理的标签，避免无意义的循环
        has_tag = False
        for comp in result.chain:
            if isinstance(comp, Plain) and "@:" in comp.text:
                has_tag = True
                break
        
        if not has_tag:
            return

        new_chain: List[BaseMessageComponent] = []
        
        for comp in result.chain:
            if isinstance(comp, Plain):
                text = comp.text
                last_idx = 0
                # 使用正则查找所有 @:userid
                for match in self.mention_pattern.finditer(text):
                    start, end = match.span()
                    
                    # 添加标签前的文本
                    if start > last_idx:
                        new_chain.append(Plain(text[last_idx:start]))
                    
                    # 添加 At 组件
                    target_id = match.group(1)
                    # 前后加零宽空格防止某些客户端显示异常
                    new_chain.append(Plain("\u200b")) 
                    new_chain.append(At(qq=target_id))
                    new_chain.append(Plain("\u200b ")) # At后面通常跟个空格比较自然
                    
                    last_idx = end
                
                # 添加剩余文本
                if last_idx < len(text):
                    new_chain.append(Plain(text[last_idx:]))
            else:
                # 图片等其他组件直接保留
                new_chain.append(comp)

        # 更新消息链
        result.chain = new_chain
