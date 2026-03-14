import re
from datetime import datetime
from typing import Optional

from astrbot.api import logger

from .models import ModerationResult, ViolationRecord, PunishmentInfo, APIConfig
from .llm_client import LLMClient
from .db import ViolationDB


class Moderator:
    """审核核心引擎"""

    def __init__(
        self,
        db: ViolationDB,
        llm_client: LLMClient,
        config: dict,
    ):
        self.db = db
        self.llm_client = llm_client
        self.config = config
        self._compiled_patterns: list[re.Pattern] = []
        self._paused = False
        self._pause_until: Optional[datetime] = None
        self._compile_regex()

    def _compile_regex(self):
        """编译正则预过滤规则"""
        self._compiled_patterns = []
        regex_text = self.config.get("regex_pre_filter", "")
        if not regex_text:
            return
        for line in regex_text.strip().split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    self._compiled_patterns.append(re.compile(line))
                except re.error as e:
                    logger.error(f"正则表达式编译失败: {line}, 错误: {e}")

    def update_config(self, config: dict):
        """更新配置"""
        self.config = config
        self._compile_regex()

    def pause(self, minutes: int = 0):
        """暂停审核"""
        self._paused = True
        if minutes > 0:
            from datetime import timedelta
            self._pause_until = datetime.now() + timedelta(minutes=minutes)
        logger.info(f"审核已暂停{f', {minutes}分钟后恢复' if minutes > 0 else ''}")

    def resume(self):
        """恢复审核"""
        self._paused = False
        self._pause_until = None
        logger.info("审核已恢复")

    @property
    def is_paused(self) -> bool:
        if not self._paused:
            return False
        if self._pause_until and datetime.now() >= self._pause_until:
            self.resume()
            return False
        return True

    def regex_check(self, text: str) -> Optional[ModerationResult]:
        """正则预过滤检查"""
        for pattern in self._compiled_patterns:
            match = pattern.search(text)
            if match:
                return ModerationResult(
                    violation=True,
                    reason=f"命中预过滤规则: {pattern.pattern}",
                    category="regex_filter",
                    confidence=1.0,
                )
        return None

    async def check_text(
        self,
        text: str,
        system_prompt: str,
        confidence_threshold: float,
    ) -> ModerationResult:
        """审核文本消息"""
        regex_result = self.regex_check(text)
        if regex_result:
            return regex_result

        result = await self.llm_client.chat_completion(
            system_prompt=system_prompt,
            user_message=text,
            is_vision=False,
        )

        if result.violation and result.confidence < confidence_threshold:
            logger.info(
                f"AI判定违规但置信度({result.confidence})低于阈值({confidence_threshold})，放行"
            )
            result.violation = False
            result.reason = f"置信度不足({result.confidence} < {confidence_threshold})"

        return result

    async def check_image(
        self,
        image_url: str,
        system_prompt: str,
        confidence_threshold: float,
        mode: str = "vision",
    ) -> ModerationResult:
        """审核图片消息"""
        image_base64 = await self.llm_client.download_image(image_url)
        if not image_base64:
            return ModerationResult(violation=False, reason="图片下载失败")

        if mode == "ocr":
            ocr_prompt = "请提取这张图片中的所有文字内容，只返回文字，不要添加任何解释。"
            ocr_result = await self.llm_client.chat_completion(
                system_prompt=ocr_prompt,
                user_message="",
                is_vision=True,
                image_base64=image_base64,
            )
            if ocr_result.raw_response and not ocr_result.violation:
                extracted_text = ocr_result.raw_response
                return await self.check_text(
                    extracted_text, system_prompt, confidence_threshold
                )
            return ModerationResult(violation=False, reason="OCR提取失败")
        else:
            result = await self.llm_client.chat_completion(
                system_prompt=system_prompt,
                user_message="",
                is_vision=True,
                image_base64=image_base64,
            )

            if result.violation and result.confidence < confidence_threshold:
                result.violation = False
                result.reason = f"置信度不足({result.confidence})"

            return result

    async def get_punishment(
        self, user_id: str, group_id: str, expire_days: int = 0
    ) -> PunishmentInfo:
        """根据违规次数获取处罚"""
        count = await self.db.get_violation_count(user_id, group_id, expire_days)
        chain = self.config.get("punishment_chain", ["warn", "mute_600", "mute_3600", "kick"])

        idx = min(count, len(chain) - 1)
        punishment_str = chain[idx] if chain else "warn"
        return PunishmentInfo.parse(punishment_str, level=idx)

    def format_notify_message(
        self,
        template: str,
        record: ViolationRecord,
        punishment: PunishmentInfo,
    ) -> str:
        """格式化通知消息"""
        try:
            return template.format(
                user=record.user_name,
                user_id=record.user_id,
                group=record.group_name,
                group_id=record.group_id,
                content=record.content[:200],
                reason=record.reason,
                category=record.category,
                confidence=f"{record.confidence:.0%}",
                time=record.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                count=record.violation_count,
                punishment=punishment.display_name,
            )
        except KeyError as e:
            logger.error(f"通知模板格式化失败，缺少变量: {e}")
            return template
