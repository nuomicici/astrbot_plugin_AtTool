import re
import json
import time
from typing import List, Dict, Any, Optional

from astrbot.api.star import Star, Context
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
from astrbot.core.message.components import Plain, At, BaseMessageComponent
from astrbot.api.provider import ProviderRequest
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

class LLMAtPromptPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 1. 严格匹配有效ID: <at id="123456"/>
        self.valid_at_pattern = re.compile(r'<at\s+id="(\d+)"\s*/>')
        # 2. 宽泛匹配疑似标签（用于除杂）: 匹配 <at ...> 或 <at ... />
        self.noise_pattern = re.compile(r'<at[^>]*?>')

    @filter.on_llm_request()
    async def inject_group_info(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        在 LLM 请求发出前，获取群成员信息并注入到 System Prompt 中。
        """
        # 仅在群聊且是 aiocqhttp (OneBot) 协议下生效
        if not isinstance(event, AiocqhttpMessageEvent):
            return
        
        group_id = event.get_group_id()
        if not group_id:
            return

        # 获取群成员列表
        members_info = await self._get_group_members_internal(event)
        if not members_info:
            return

        # 构建精简的成员映射表 (Name -> ID)
        # 限制数量防止 Context 溢出 (例如只取前 50 个或活跃用户，这里演示取全部)
        member_map = []
        for m in members_info:
            user_id = str(m.get("user_id", ""))
            name = m.get("card") or m.get("nickname") or f"用户{user_id}"
            if user_id:
                member_map.append(f'{name}(ID:{user_id})')

        # 将列表转换为字符串，为了节省 Token，可以只用简单的文本列表
        member_list_str = ", ".join(member_map)

        # 构建注入的 Prompt
        # 使用 XML 标签格式，LLM 理解能力更强
        injection_prompt = (
            f"\n\n[当前群聊环境信息]\n"
            f"群号: {group_id}\n"
            f"群成员列表: {member_list_str}\n"
            f"[指令要求]\n"
            f"如果你认为必须引起特定群成员的注意（艾特/提醒），请务必在回复中插入 XML 标签：<at id=\"用户ID\"/>\n"
            f"例如：<at id=\"123456\"/>\n"
            f"严禁编造不存在的用户ID。不要输出多余的解释文本，直接嵌入标签即可。"
        )

        # 追加到 System Prompt
        req.system_prompt += injection_prompt
        
        # Debug 日志
        # logger.debug(f"已注入群成员 Prompt: {len(member_map)} 人")

    @filter.on_decorating_result(priority=2)
    async def process_at_tags(self, event: AstrMessageEvent, *args):
        """
        拦截消息，处理 XML 标签：
        1. 将 <at id="123"/> 转换为真实 At 组件
        2. 移除格式错误的 <at ...> 标签（除杂）
        """
        result = event.get_result()
        if not result or not result.chain:
            return

        # 快速检查是否有类似标签，避免无意义循环
        has_tag = False
        for comp in result.chain:
            if isinstance(comp, Plain) and "<at" in comp.text:
                has_tag = True
                break
        
        if not has_tag:
            return

        new_chain: List[BaseMessageComponent] = []

        for comp in result.chain:
            if isinstance(comp, Plain):
                text = comp.text
                
                # --- 第一步：处理有效的 At 标签 ---
                # 使用 split 技巧进行分割处理
                # finditer 也可以，这里演示另一种逻辑：先找有效，再清洗无效
                
                last_idx = 0
                # 查找所有有效标签
                for match in self.valid_at_pattern.finditer(text):
                    start, end = match.span()
                    
                    # 处理标签前的文本
                    if start > last_idx:
                        pre_text = text[last_idx:start]
                        # 递归清洗这段文本中的无效标签（除杂）
                        cleaned_pre_text = self._clean_noise(pre_text)
                        if cleaned_pre_text:
                            new_chain.append(Plain(cleaned_pre_text))

                    # 添加 At 组件
                    target_id = match.group(1)
                    new_chain.append(Plain("\u200b")) # 零宽空格防吞
                    new_chain.append(At(qq=target_id))
                    new_chain.append(Plain("\u200b"))

                    last_idx = end

                # 处理剩余文本
                if last_idx < len(text):
                    remaining_text = text[last_idx:]
                    cleaned_remaining = self._clean_noise(remaining_text)
                    if cleaned_remaining:
                        new_chain.append(Plain(cleaned_remaining))
            else:
                new_chain.append(comp)

        result.chain = new_chain

    def _clean_noise(self, text: str) -> str:
        """
        除杂处理：移除所有匹配 <at ...> 但未被识别为有效 ID 的标签。
        防止 LLM 输出 <at id="unknown"/> 或 <at name="张三"/> 等无效内容显示给用户。
        """
        return self.noise_pattern.sub("", text)

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
