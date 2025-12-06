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
        # 编译正则，匹配 [at:123456] 格式
        self.at_pattern = re.compile(r'\[at:(\d+)\]')

    @filter.llm_tool(name="at_user")
    async def at_user(self, event: AstrMessageEvent, user_id: str) -> str:
        """
        生成艾特(At)用户的标签。
        仅在用户明确要求“艾特”、“提醒”、“呼叫”或“@”某人，或你判断必须引起特定群成员注意时调用。
        
        Args:
            user_id(str): 用户的唯一识别标识(QQ号)，必须为阿拉伯数字字符串。
            
        Returns:
            str: 格式化后的标签。请务必将此工具的返回结果包含在你的最终回复中，否则无法成功艾特。
        """
        return f"[at:{user_id}]"

    @filter.llm_tool(name="get_info_to_at")
    async def get_group_members(self, event: AstrMessageEvent) -> str:
        """
        获取群成员列表。
        仅在用户明确要求“艾特”、“提醒”、“呼叫”或“@”某人，但你不知道对方 user_id 时调用。
        调用后，请从返回的 JSON 中筛选出对应成员的 user_id，然后调用 at_user 工具。
        """
        start_time = time.time()
        
        try:
            group_id = event.get_group_id()
            if not group_id:
                return json.dumps({"error": "非群聊环境，无法获取成员列表"})
            
            # 检查是否为 aiocqhttp 事件，因为只有该平台支持此 API
            if not isinstance(event, AiocqhttpMessageEvent):
                return json.dumps({"error": "仅支持QQ群聊(aiocqhttp)"})

            members_info = await self._get_group_members_internal(event)
            if not members_info:
                return json.dumps({"error": "获取失败(权限不足或网络错误)"})
            
            processed_members = [
                {
                    "user_id": str(member.get("user_id", "")),
                    "names": [  # 聚合所有可能的名称，方便LLM搜索
                        member.get("card", ""), 
                        member.get("nickname", ""), 
                        f"用户{member.get('user_id')}"
                    ],
                    "role": member.get("role", "member")
                }
                for member in members_info if member.get("user_id")
            ]
            
            # 稍微精简返回结构
            final_members = []
            for m in processed_members:
                m["names"] = [n for n in m["names"] if n]
                final_members.append(m)

            group_info = {
                "group_id": group_id,
                "count": len(final_members),
                "members": final_members
            }
            
            elapsed_time = time.time() - start_time
            logger.debug(f"获取群成员成功: {len(final_members)}人, 耗时{elapsed_time:.2f}s")
            
            return json.dumps(group_info, ensure_ascii=False)
        except Exception as e:
            logger.error(f"获取群成员错误: {e}")
            return json.dumps({"error": str(e)})

    async def _get_group_members_internal(self, event: AiocqhttpMessageEvent) -> Optional[List[Dict[str, Any]]]:
        """内部函数：调用API获取群成员列表"""
        try:
            group_id = event.get_group_id()
            if not group_id:
                return None
            # 调用 OneBot API
            return await event.bot.api.call_action('get_group_member_list', group_id=group_id)
        except Exception as e:
            logger.error(f"API调用失败: {e}")
            return None

    @filter.on_decorating_result(priority=2)
    async def process_at_tags(self, event: AstrMessageEvent):
        """
        拦截消息，将文本中的 [at:id] 转换为真实 At 组件。
        priority=2 确保在默认处理之后、发送之前执行。
        """
        result = event.get_result()
        if not result or not result.chain:
            return

        # 快速检查，避免无意义遍历
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
                # 正则查找所有标签
                for match in self.at_pattern.finditer(text):
                    start, end = match.span()
                    
                    # 添加标签前的文本
                    if start > last_idx:
                        pre_text = text[last_idx:start]
                        if pre_text:
                            new_chain.append(Plain(pre_text))
                    
                    target_id = match.group(1)
                    
                    # 添加 At 组件，前后加零宽空格防止粘连
                    new_chain.append(Plain("\u200b")) 
                    new_chain.append(At(qq=target_id))
                    new_chain.append(Plain("\u200b"))
                    
                    last_idx = end
                
                # 添加剩余文本
                if last_idx < len(text):
                    new_chain.append(Plain(text[last_idx:]))
            else:
                # 保留图片等其他组件
                new_chain.append(comp)

        # 替换原消息链
        result.chain = new_chain
