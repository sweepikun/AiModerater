import re
from datetime import datetime
from typing import Optional

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Image, Plain, At
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .lib.db import ViolationDB
from .lib.llm_client import LLMClient
from .lib.moderator import Moderator
from .lib.models import APIConfig, ViolationRecord, PunishmentInfo


@register(
    "astrbot_plugin_ai_moderator",
    "AiFilter",
    "基于大模型的群消息AI审核插件",
    "1.0.0",
    "",
)
class AIModeratorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.db: Optional[ViolationDB] = None
        self.llm_client: Optional[LLMClient] = None
        self.moderator: Optional[Moderator] = None

    async def initialize(self):
        """插件初始化"""
        data_path = get_astrbot_data_path() / "plugin_data" / "ai_moderator"
        db_path = str(data_path / "violations.db")

        self.db = ViolationDB(db_path)
        await self.db.init()

        apis = self._parse_api_configs()
        self.llm_client = LLMClient(apis)
        self.llm_client.max_rpm = self.config.get("max_rpm", 30)
        await self.llm_client.start()

        self.moderator = Moderator(
            db=self.db,
            llm_client=self.llm_client,
            config=self.config,
        )

        logger.info(f"AI审核插件已初始化，配置了 {len(apis)} 个API")

    def _parse_api_configs(self) -> list[APIConfig]:
        """解析API配置"""
        apis = []
        api_list = self.config.get("llm_apis", [])
        for item in api_list:
            if isinstance(item, dict):
                apis.append(
                    APIConfig(
                        name=item.get("name", "未命名"),
                        api_key=item.get("api_key", ""),
                        base_url=item.get("base_url", ""),
                        model=item.get("model", ""),
                        vision_model=item.get("vision_model", ""),
                    )
                )
        return apis

    def _get_group_setting(self, group_id: str) -> dict:
        """获取群组特定配置"""
        settings = self.config.get("group_settings", [])
        for item in settings:
            if isinstance(item, dict) and item.get("group_id") == group_id:
                return item
        return {"check_text": True, "check_image": False, "image_mode": "vision"}

    def _is_in_enabled_groups(self, group_id: str) -> bool:
        """检查群是否在启用列表中"""
        enabled = self.config.get("enabled_groups", [])
        return group_id in enabled

    def _is_whitelisted(self, event: AstrMessageEvent) -> bool:
        """检查用户是否在白名单中"""
        user_id = event.get_sender_id()

        whitelist_admin = self.config.get("whitelist_admin", True)
        if whitelist_admin and event.is_admin():
            return True

        whitelist_users = self.config.get("whitelist_users", [])
        if user_id in whitelist_users:
            return True

        return False

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """拦截群消息进行审核"""
        if not self.moderator or self.moderator.is_paused:
            return

        group_id = event.get_group_id()
        if not group_id:
            return

        if not self._is_in_enabled_groups(group_id):
            return

        if self._is_whitelisted(event):
            return

        group_setting = self._get_group_setting(group_id)
        check_text = group_setting.get("check_text", True)
        check_image = group_setting.get("check_image", False)
        image_mode = group_setting.get("image_mode", "vision")

        message_obj = event.message_obj
        if not message_obj:
            return

        system_prompt = self.config.get(
            "moderation_prompt",
            "你是一个内容审核员。请判断消息是否违规。",
        )
        image_prompt = self.config.get(
            "image_moderation_prompt",
            "请审核这张图片是否违规。",
        )
        confidence_threshold = self.config.get("confidence_threshold", 0.7)
        expire_days = self.config.get("violation_expire_days", 30)

        for component in message_obj.message:
            component_type = type(component).__name__

            if component_type == "Plain" and check_text:
                text = component.text if hasattr(component, "text") else str(component)
                if not text.strip():
                    continue

                result = await self.moderator.check_text(
                    text, system_prompt, confidence_threshold
                )
                if result.violation:
                    await self._handle_violation(
                        event, text, "text", result, group_id, expire_days
                    )
                    event.stop_event()
                    return

            elif component_type == "Image" and check_image:
                image_url = ""
                if hasattr(component, "url") and component.url:
                    image_url = component.url
                elif hasattr(component, "file") and component.file:
                    image_url = component.file

                if not image_url:
                    continue

                result = await self.moderator.check_image(
                    image_url, image_prompt, confidence_threshold, image_mode
                )
                if result.violation:
                    await self._handle_violation(
                        event, "[图片消息]", "image", result, group_id, expire_days
                    )
                    event.stop_event()
                    return

    async def _handle_violation(
        self,
        event: AstrMessageEvent,
        content: str,
        content_type: str,
        result,
        group_id: str,
        expire_days: int,
    ):
        """处理违规"""
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        group_name = group_id

        violation_count = await self.db.get_violation_count(user_id, group_id, expire_days)
        violation_count += 1

        punishment = await self.moderator.get_punishment(user_id, group_id, expire_days)

        record = ViolationRecord(
            user_id=user_id,
            user_name=user_name,
            group_id=group_id,
            group_name=group_name,
            content=content,
            content_type=content_type,
            reason=result.reason,
            category=result.category,
            confidence=result.confidence,
            timestamp=datetime.now(),
            punishment=punishment.display_name,
            violation_count=violation_count,
            api_used="",
        )
        await self.db.add_violation(record)

        delete_msg = self.config.get("delete_violation_msg", True)
        if delete_msg:
            try:
                await event.send(event.plain_result("该消息已被审核系统撤回"))
            except Exception:
                pass

        await self._execute_punishment(event, punishment)

        notify_user = self.config.get("notify_user", True)
        if notify_user:
            user_template = self.config.get(
                "notify_user_template",
                "你在群 {group} 的消息因 {reason} 被处理，处罚: {punishment}，累计违规 {count} 次",
            )
            user_msg = self.moderator.format_notify_message(user_template, record, punishment)
            try:
                await event.send(event.plain_result(user_msg))
            except Exception as e:
                logger.warning(f"私聊通知用户失败: {e}")

        await self._send_notify(record, punishment)

        logger.info(
            f"违规处理完成: 用户={user_name}({user_id}) 群={group_id} "
            f"类型={content_type} 原因={result.reason} 处罚={punishment.display_name} "
            f"累计={violation_count}次"
        )

    async def _execute_punishment(self, event: AstrMessageEvent, punishment: PunishmentInfo):
        """执行处罚"""
        try:
            if punishment.action == "warn":
                await event.send(event.plain_result("⚠️ 你的消息已被警告，原因：违规内容。请遵守群规。"))
            elif punishment.action == "mute":
                await event.send(
                    event.plain_result(
                        f"🔇 你已被禁言 {punishment.duration} 秒，原因：违规内容。累计违规。"
                    )
                )
            elif punishment.action == "kick":
                await event.send(event.plain_result("👢 你已被移出群聊，原因：多次违规。"))
            elif punishment.action == "ban":
                await event.send(event.plain_result("🚫 你已被拉黑，原因：严重违规。"))
        except Exception as e:
            logger.error(f"执行处罚失败: {e}")

    async def _send_notify(self, record: ViolationRecord, punishment: PunishmentInfo):
        """发送违规通知到指定目标"""
        target = self.config.get("notify_target", "")
        if not target:
            return

        template = self.config.get(
            "notify_template",
            "【违规举报】用户: {user}({user_id}) 群: {group} 内容: {content} 原因: {reason} 处罚: {punishment} 累计: {count}次",
        )
        msg = self.moderator.format_notify_message(template, record, punishment)

        try:
            await self.context.send_message(
                target,
                [Plain(msg)],
            )
        except Exception as e:
            logger.warning(f"发送违规通知到 {target} 失败: {e}")

    @filter.command_group("审核")
    def mod(self):
        """审核管理指令组"""
        pass

    @mod.command("pause")
    async def mod_pause(self, event: AstrMessageEvent, minutes: int = 0):
        """暂停审核 - /审核 pause [分钟数]"""
        if not self.moderator:
            yield event.plain_result("审核插件未初始化")
            return
        self.moderator.pause(minutes)
        if minutes > 0:
            yield event.plain_result(f"✅ 审核已暂停 {minutes} 分钟")
        else:
            yield event.plain_result("✅ 审核已暂停（需手动恢复）")

    @mod.command("resume")
    async def mod_resume(self, event: AstrMessageEvent):
        """恢复审核 - /审核 resume"""
        if not self.moderator:
            yield event.plain_result("审核插件未初始化")
            return
        self.moderator.resume()
        yield event.plain_result("✅ 审核已恢复")

    @mod.command("status")
    async def mod_status(self, event: AstrMessageEvent):
        """查看审核状态 - /审核 status"""
        if not self.moderator:
            yield event.plain_result("审核插件未初始化")
            return

        status_lines = ["📋 AI审核状态"]
        status_lines.append(f"暂停状态: {'已暂停' if self.moderator.is_paused else '运行中'}")
        status_lines.append(
            f"启用群组: {', '.join(self.config.get('enabled_groups', [])) or '无'}"
        )

        api_status = self.llm_client.get_api_status()
        status_lines.append(f"\nAPI状态 ({len(api_status)}个):")
        for api in api_status:
            health = "✅" if api["healthy"] else "❌"
            status_lines.append(f"  {health} {api['name']} ({api['model']})")

        yield event.plain_result("\n".join(status_lines))

    @mod.command("query")
    async def mod_query(
        self,
        event: AstrMessageEvent,
        user_id: Optional[str] = None,
        date: Optional[str] = None,
    ):
        """查询违规记录 - /审核 query [@用户] [日期YYYY-MM-DD]"""
        if not self.db:
            yield event.plain_result("数据库未初始化")
            return

        actual_user_id = user_id
        if user_id and user_id.startswith("[CQ:at,qq="):
            match = re.search(r"qq=(\d+)", user_id)
            if match:
                actual_user_id = match.group(1)

        start_time = None
        end_time = None
        if date:
            start_time = f"{date} 00:00:00"
            end_time = f"{date} 23:59:59"

        records = await self.db.query_violations(
            user_id=actual_user_id,
            start_time=start_time,
            end_time=end_time,
            limit=10,
        )

        if not records:
            yield event.plain_result("📭 未找到违规记录")
            return

        lines = [f"📋 违规记录 (共{len(records)}条)"]
        for i, r in enumerate(records, 1):
            lines.append(
                f"\n{i}. [{r.timestamp.strftime('%m-%d %H:%M')}] "
                f"{r.user_name}({r.user_id})"
            )
            lines.append(f"   群: {r.group_id} | 类型: {r.content_type}")
            lines.append(f"   内容: {r.content[:50]}{'...' if len(r.content) > 50 else ''}")
            lines.append(f"   原因: {r.reason} | 处罚: {r.punishment}")
            lines.append(f"   置信度: {r.confidence:.0%} | 累计: {r.violation_count}次")

        yield event.plain_result("\n".join(lines))

    @mod.command("stats")
    async def mod_stats(self, event: AstrMessageEvent, group_id: Optional[str] = None):
        """查看统计信息 - /审核 stats [群号]"""
        if not self.db:
            yield event.plain_result("数据库未初始化")
            return

        stats = await self.db.get_stats(group_id)
        lines = ["📊 违规统计"]
        lines.append(f"总违规数: {stats['total']}")
        lines.append(f"今日违规: {stats['today']}")
        lines.append(f"涉及用户: {stats['unique_users']}")

        if stats["categories"]:
            lines.append("\n违规类型分布:")
            for cat, cnt in stats["categories"].items():
                lines.append(f"  {cat}: {cnt}次")

        yield event.plain_result("\n".join(lines))

    @mod.command("whitelist")
    async def mod_whitelist(self, event: AstrMessageEvent, action: str = "", user_id: str = ""):
        """管理白名单 - /审核 whitelist add/remove 用户ID"""
        if action not in ("add", "remove", "list"):
            yield event.plain_result("用法: /审核 whitelist add|remove|list [用户ID]")
            return

        whitelist = self.config.get("whitelist_users", [])

        if action == "list":
            if not whitelist:
                yield event.plain_result("白名单为空")
            else:
                yield event.plain_result(f"白名单用户: {', '.join(whitelist)}")
            return

        if not user_id:
            yield event.plain_result("请指定用户ID")
            return

        if user_id.startswith("[CQ:at,qq="):
            match = re.search(r"qq=(\d+)", user_id)
            if match:
                user_id = match.group(1)

        if action == "add":
            if user_id not in whitelist:
                whitelist.append(user_id)
                self.config["whitelist_users"] = whitelist
                self.config.save_config()
                yield event.plain_result(f"✅ 已添加 {user_id} 到白名单")
            else:
                yield event.plain_result(f"用户 {user_id} 已在白名单中")
        elif action == "remove":
            if user_id in whitelist:
                whitelist.remove(user_id)
                self.config["whitelist_users"] = whitelist
                self.config.save_config()
                yield event.plain_result(f"✅ 已从白名单移除 {user_id}")
            else:
                yield event.plain_result(f"用户 {user_id} 不在白名单中")

    @mod.command("test")
    async def mod_test(self, event: AstrMessageEvent, text: str = ""):
        """测试审核文本 - /审核 test 待审核文本"""
        if not text:
            yield event.plain_result("请提供要测试的文本")
            return

        if not self.moderator:
            yield event.plain_result("审核插件未初始化")
            return

        system_prompt = self.config.get("moderation_prompt", "")
        threshold = self.config.get("confidence_threshold", 0.7)

        result = await self.moderator.check_text(text, system_prompt, threshold)

        lines = ["🧪 审核测试结果"]
        lines.append(f"输入: {text[:100]}")
        lines.append(f"违规: {'是 ❌' if result.violation else '否 ✅'}")
        lines.append(f"原因: {result.reason or '无'}")
        lines.append(f"类别: {result.category or '无'}")
        lines.append(f"置信度: {result.confidence:.0%}")

        yield event.plain_result("\n".join(lines))

    @mod.command("cleanup")
    async def mod_cleanup(self, event: AstrMessageEvent, days: Optional[int] = None):
        """清理过期记录 - /审核 cleanup [天数]"""
        if not self.db:
            yield event.plain_result("数据库未初始化")
            return

        expire_days = days if days else self.config.get("violation_expire_days", 30)
        if expire_days <= 0:
            yield event.plain_result("请指定大于0的天数")
            return

        count = await self.db.cleanup_expired(expire_days)
        yield event.plain_result(f"✅ 已清理 {count} 条超过 {expire_days} 天的违规记录")

    async def terminate(self):
        """插件卸载时清理资源"""
        if self.db:
            await self.db.close()
        if self.llm_client:
            await self.llm_client.close()
        logger.info("AI审核插件已卸载")
