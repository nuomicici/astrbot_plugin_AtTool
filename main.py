import re
import json
import time
from typing import List, Optional, Tuple
from astrbot.api.star import Star, Context
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger, AstrBotConfig
from astrbot.api.provider import ProviderRequest
from astrbot.core.message.components import Plain, At, BaseMessageComponent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


class LLMAtToolPlugin(Star):
    AT_ALL_PERMISSION_CACHE_KEY = "_at_all_permission_result"

    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        # 正则表达式：用于匹配符合规范的艾特标签，例如 [at:123456] 或 [at:all]
        self.valid_at_pattern = re.compile(r"\[at:(\d+|all)\]")
        # 正则表达式：用于匹配不符合规范的标签（如包含非数字内容），保留用于后续可能的逻辑处理
        self.garbage_at_pattern = re.compile(r"\[at:[^\]]+\]")
        self.permission_verification = self.config.get("permission_verification", True)
        self.llm_prompt = self._normalize_editor_text(self.config.get("llm_prompt", ""))

    @staticmethod
    def _normalize_editor_text(text: object) -> str:
        if not isinstance(text, str):
            return ""

        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        if "\n" not in normalized and (
            "\\r\\n" in normalized or "\\n" in normalized or "\\r" in normalized
        ):
            normalized = (
                normalized.replace("\\r\\n", "\n")
                .replace("\\r", "\n")
                .replace("\\n", "\n")
            )
        return normalized

    def _is_bot_super_admin(self, event: AstrMessageEvent) -> bool:
        is_admin_attr = getattr(event, "is_admin", None)
        try:
            if callable(is_admin_attr):
                return bool(is_admin_attr())
            return bool(is_admin_attr)
        except Exception as exc:
            logger.warning(f"检查 Bot 超级管理员权限失败: {exc}")
            return False

    @staticmethod
    def _build_sender_identity_reason(
        sender_id: object | None, reason_suffix: str
    ) -> str:
        if not sender_id:
            return "无法识别他的身份,拒绝执行"
        return f"{sender_id}{reason_suffix}"

    async def _check_at_all_permission(
        self, event: AstrMessageEvent
    ) -> Tuple[bool, str]:
        group_id = event.get_group_id()
        if not group_id:
            return False, "非群聊场景"

        if self._is_bot_super_admin(event):
            return True, ""

        if not isinstance(event, AiocqhttpMessageEvent):
            return False, "当前平台不支持@全体权限校验"

        sender_getter = getattr(event, "get_sender_id", None)
        sender_id = sender_getter() if callable(sender_getter) else None
        if not sender_id:
            return False, self._build_sender_identity_reason(
                sender_id, "的身份,拒绝执行"
            )

        try:
            group_member_info = await event.bot.api.call_action(
                "get_group_member_info",
                group_id=group_id,
                user_id=sender_id,
            )
        except Exception as exc:
            logger.warning(
                f"查询@全体权限失败: group_id={group_id}, user_id={sender_id}, error={exc}"
            )
            return False, "查询群成员权限失败"

        role = str(group_member_info.get("role", "member")).lower()
        if role in {"owner", "admin"}:
            return True, ""
        return False, self._build_sender_identity_reason(
            sender_id, " 不是群主、管理员或 Bot 超级管理员"
        )

    async def _get_at_all_permission_result(
        self, event: AstrMessageEvent
    ) -> Tuple[bool, str]:
        cached_result = event.get_extra(self.AT_ALL_PERMISSION_CACHE_KEY)
        if (
            isinstance(cached_result, tuple)
            and len(cached_result) == 2
            and isinstance(cached_result[1], str)
        ):
            return bool(cached_result[0]), cached_result[1]

        permission_result = await self._check_at_all_permission(event)
        event.set_extra(self.AT_ALL_PERMISSION_CACHE_KEY, permission_result)
        return permission_result

    @filter.on_llm_request()
    async def inject_at_instruction(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """
        在 LLM（大语言模型）发出请求前注入系统提示词。
        告知模型如何使用特定的 XML 标签格式来进行艾特操作。
        """
        instruction = self.llm_prompt
        # 将指令追加到当前的系统提示词中
        req.system_prompt = (req.system_prompt or "") + instruction

        if not self.permission_verification:
            return

        allowed, deny_message = await self._get_at_all_permission_result(event)
        if allowed:
            req.system_prompt += (
                "\n当前操作者具备@全体权限。"
                "\n如用户明确要求且场景确有必要，你可以输出 [at:all]。"
            )
            return

        req.system_prompt += (
            "\n当前操作者不具备@全体权限。"
            f"\n原因：{deny_message}"
            "\n禁止输出 [at:all]。"
            "\n如果用户要求@全体，请直接用自然语言说明无法执行，不要输出任何 @全体 标签。"
        )

    @filter.llm_tool(name="get_group_members")
    async def get_group_members(
        self, event: AstrMessageEvent, keyword: str = ""
    ) -> str:
        """
        供 LLM 调用的工具：获取当前群聊的成员列表。

        Args:
            keyword(string): 搜索关键词，支持匹配昵称、群名片或QQ号。若为空则返回全员。
        """
        start_time = time.time()

        # 获取群组 ID，如果不在群聊中则返回错误
        group_id = event.get_group_id()
        if not group_id:
            return json.dumps(
                {"status": "error", "message": "当前不在群聊环境中，无法查询成员。"},
                ensure_ascii=False,
            )

        # 检查当前消息事件是否支持（目前主要支持 aiocqhttp 协议，即 OneBot）
        if not isinstance(event, AiocqhttpMessageEvent):
            return json.dumps(
                {"status": "error", "message": "当前平台协议暂不支持获取群成员。"},
                ensure_ascii=False,
            )

        try:
            # 通过机器人 API 获取群成员原始数据
            raw_members = await event.bot.api.call_action(
                "get_group_member_list", group_id=group_id
            )
            if not raw_members:
                return json.dumps(
                    {
                        "status": "error",
                        "message": "无法获取成员列表或机器人权限不足。",
                    },
                    ensure_ascii=False,
                )

            formatted_members = []

            for m in raw_members:
                user_id = str(m.get("user_id", ""))
                nickname = m.get("nickname", "")
                card = m.get("card", "")  # 群名片（备注）
                role = m.get(
                    "role", "member"
                )  # 角色：owner(群主), admin(管理员), member(普通成员)

                # 如果提供了关键词，则在 ID、昵称、名片中进行模糊匹配
                search_content = f"{user_id}{nickname}{card}"
                if keyword and keyword not in search_content:
                    continue

                # 角色名称转换
                role_map = {"owner": "群主", "admin": "管理员", "member": "成员"}
                role_cn = role_map.get(role, "成员")

                formatted_members.append(
                    {
                        "user_id": user_id,
                        "nickname": nickname,
                        "group_card": card if card else "无",
                        "role": role_cn,
                    }
                )

            # 构建返回给 LLM 的 JSON 结果
            output_data = {
                "status": "success",
                "group_id": group_id,
                "count": len(formatted_members),
                "members": formatted_members,
            }

            logger.debug(
                f"群成员查询成功：耗时 {time.time() - start_time:.2f}s，共找到 {len(formatted_members)} 人"
            )
            return json.dumps(output_data, ensure_ascii=False, indent=2)

        except Exception as e:
            logger.error(f"查询群成员过程发生异常: {e}")
            return json.dumps(
                {"status": "error", "message": f"系统内部异常: {str(e)}"},
                ensure_ascii=False,
            )

    @filter.on_decorating_result(priority=2)
    async def process_at_tags(self, event: AstrMessageEvent):
        """
        拦截器：在消息发送给用户前，对LLM输出的内容进行二次处理。
        功能：
        1. 识别[at:数字]并转换为平台原生的At组件。
        2. 自动清理 At 标签周边的空格。
        3. 注入零宽字符以防止文本渲染时出现格式错乱。
        """
        result = event.get_result()
        if not result or not result.chain:
            return

        # 快速检查结果链中是否包含可能的 at 标签文本
        has_tag = False
        has_at_all_tag = False
        for comp in result.chain:
            if isinstance(comp, Plain) and "[at:" in comp.text:
                has_tag = True
                if "[at:all]" in comp.text:
                    has_at_all_tag = True

        if not has_tag:
            return

        at_all_allowed = True
        if self.permission_verification and has_at_all_tag:
            at_all_allowed, deny_message = await self._get_at_all_permission_result(
                event
            )
            if not at_all_allowed:
                logger.info(
                    "拦截越权@全体并降级为普通文本: "
                    f"group_id={event.get_group_id()}, "
                    f"sender_id={getattr(event, 'get_sender_id', lambda: None)()}, "
                    f"reason={deny_message}"
                )

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

                    # 获取 ID 并插入 At 组件；越权的 @全体 会降级为普通文本兜底
                    target_id = match.group(1)
                    if target_id == "all" and not at_all_allowed:
                        new_chain.append(Plain("@全体成员"))
                        last_idx = end
                        continue

                    new_chain.append(At(qq=target_id))
                    # 可以考虑在@后加一个空格，避免粘连
                    new_chain.append(Plain(" "))

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
                        new_chain[prev_idx].text = new_chain[prev_idx].text.rstrip(
                            " \t"
                        )
                        break
                    elif not isinstance(new_chain[prev_idx], At):
                        break

                # 向后寻找最近的文本组件并清除左侧空格
                for next_idx in range(idx + 1, len(new_chain)):
                    if isinstance(new_chain[next_idx], Plain):
                        new_chain[next_idx].text = new_chain[next_idx].text.lstrip(
                            " \t"
                        )
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
                        new_chain[next_idx].text = (
                            "\u200b \u200b" + new_chain[next_idx].text
                        )
                        found_plain = True
                        break

                # 如果 At 后面没有文本了，则手动补充一个带防连连看字符的 Plain 组件
                if not found_plain:
                    new_chain.insert(idx + 1, Plain("\u200b \u200b"))
            idx += 1

        # 更新事件的最终渲染链
        result.chain = new_chain
