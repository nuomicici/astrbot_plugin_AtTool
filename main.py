import re
import json
import time
from typing import List, Dict, Any, Optional

from astrbot.api.star import Star, Context, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.core.message.components import Plain, At, BaseMessageComponent
# 注意：为了通用性，建议尽量使用 AstrMessageEvent，特定平台逻辑再进行类型检查
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

@register("llm_at_tool", "YourName", "让不支持FC的模型也能实现智能艾特", "1.1.0")
class LLMAtToolPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 匹配 [at:123456]
        self.at_pattern = re.compile(r'\[at:(\d+)\]')
        # 匹配 [SEARCH:关键词]
        self.search_pattern = re.compile(r'\[SEARCH:(.*?)\]')

    # -------------------------------------------------------------------------
    # 1. Prompt 注入：教导模型如何使用工具
    # -------------------------------------------------------------------------
    @filter.on_llm_request()
    async def inject_tools_instruction(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        在 LLM 请求发出前，向 system_prompt 中注入伪工具协议。
        """
        # 仅在群聊中启用
        if not event.message_obj.group_id:
            return

        instruction = (
            "\n\n【工具使用指南】\n"
            "你具有查询群成员和艾特(At)群成员的能力。请严格遵守以下协议，不要输出多余的解释：\n"
            "1. 如果你需要艾特某人且知道其QQ号，请在回复中包含：[at:QQ号]\n"
            "2. 如果你需要艾特某人但不知道QQ号，请只输出：[SEARCH:成员昵称]\n"
            "3. 收到搜索结果后，请根据结果重新生成包含 [at:QQ号] 的回复。\n"
        )
        # 追加到系统提示词
        req.system_prompt += instruction

    # -------------------------------------------------------------------------
    # 2. 响应拦截：处理 [SEARCH:...] 指令
    # -------------------------------------------------------------------------
    @filter.on_llm_response()
    async def handle_search_intent(self, event: AstrMessageEvent, resp: LLMResponse):
        """
        监听 LLM 回复，如果发现搜索指令，则执行搜索并进行二次生成。
        """
        text = resp.completion_text
        match = self.search_pattern.search(text)
        
        if match:
            keyword = match.group(1).strip()
            logger.info(f"检测到 LLM 搜索意图: {keyword}")

            # 1. 执行搜索逻辑
            search_result_json = await self.search_group_member(event, keyword)
            
            # 2. 构建二次请求的 Prompt
            # 这里我们将原始问题、搜索结果组合起来，让模型重新生成
            retry_prompt = (
                f"用户原始消息：{event.message_str}\n"
                f"你刚才尝试搜索：{keyword}\n"
                f"系统返回的搜索结果：{search_result_json}\n"
                f"现在，请根据搜索结果，回复用户。如果找到了目标用户，请使用 [at:QQ号] 格式。"
            )

            # 3. 获取当前会话的 Provider ID (v4.5.7+)
            umo = event.unified_msg_origin
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)

            if not provider_id:
                logger.warning("无法获取 Provider ID，跳过二次生成")
                resp.completion_text = f"搜索结果：{search_result_json} (请手动艾特)"
                return

            # 4. 调用 LLM 进行二次生成 (Re-generation)
            try:
                new_resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=retry_prompt,
                    # 可以选择带上 system_prompt 强化指令，也可以不带
                    system_prompt="你是一个群聊助手。请根据提供的搜索结果，生成最终回复。使用 [at:QQ号] 进行艾特。"
                )
                
                # 5. 替换原始回复
                if new_resp and new_resp.completion_text:
                    resp.completion_text = new_resp.completion_text
                    logger.info("二次生成成功，已替换回复")
                else:
                    resp.completion_text = "搜索执行成功，但生成回复失败。"

            except Exception as e:
                logger.error(f"二次生成出错: {e}")
                resp.completion_text = f"搜索出错: {e}"

    # -------------------------------------------------------------------------
    # 3. 核心逻辑：获取并搜索群成员
    # -------------------------------------------------------------------------
    async def search_group_member(self, event: AstrMessageEvent, keyword: str) -> str:
        """
        获取群成员并进行模糊搜索
        """
        try:
            # 检查平台兼容性
            if not isinstance(event, AiocqhttpMessageEvent):
                return "错误：当前平台不支持获取群成员列表。"

            group_id = event.get_group_id()
            if not group_id:
                return "错误：非群聊环境。"

            # 调用 OneBot API 获取列表
            # 注意：这里可能会有缓存问题，如果群成员很多，建议自行实现缓存机制
            member_list = await event.bot.api.call_action('get_group_member_list', group_id=group_id)
            
            if not member_list:
                return "错误：无法获取成员列表或列表为空。"

            # 搜索逻辑
            found_members = []
            for m in member_list:
                user_id = str(m.get('user_id', ''))
                nickname = m.get('nickname', '')
                card = m.get('card', '')
                
                # 简单的模糊匹配
                if keyword in nickname or keyword in card or keyword == user_id:
                    found_members.append({
                        "user_id": user_id,
                        "name": card if card else nickname
                    })
            
            # 限制返回数量，防止 Token 爆炸
            found_members = found_members[:10] 

            if not found_members:
                return f"未找到包含 '{keyword}' 的成员。"
            
            return json.dumps(found_members, ensure_ascii=False)

        except Exception as e:
            logger.error(f"搜索群成员异常: {e}")
            return f"搜索过程发生系统错误: {e}"

    # -------------------------------------------------------------------------
    # 4. 消息渲染：将 [at:id] 转为真实组件 (保留原逻辑)
    # -------------------------------------------------------------------------
    @filter.on_decorating_result(priority=2)
    async def process_at_tags(self, event: AstrMessageEvent):
        """
        拦截消息，将文本中的 [at:id] 转换为真实 At 组件。
        """
        result = event.get_result()
        if not result or not result.chain:
            return

        # 快速检查是否有 tag，避免不必要的遍历
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
                for match in self.at_pattern.finditer(text):
                    start, end = match.span()

                    if start > last_idx:
                        pre_text = text[last_idx:start]
                        if pre_text:
                            new_chain.append(Plain(pre_text))

                    target_id = match.group(1)
                    # 前后加零宽空格防止粘连
                    new_chain.append(Plain("\u200b")) 
                    new_chain.append(At(qq=target_id))
                    new_chain.append(Plain("\u200b"))

                    last_idx = end

                if last_idx < len(text):
                    new_chain.append(Plain(text[last_idx:]))
            else:
                new_chain.append(comp)

        result.chain = new_chain