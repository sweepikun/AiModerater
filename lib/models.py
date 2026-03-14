from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class ViolationRecord:
    """违规记录"""
    id: Optional[int] = None
    user_id: str = ""
    user_name: str = ""
    group_id: str = ""
    group_name: str = ""
    content: str = ""
    content_type: str = "text"
    reason: str = ""
    category: str = ""
    confidence: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)
    punishment: str = ""
    violation_count: int = 0
    api_used: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "content": self.content,
            "content_type": self.content_type,
            "reason": self.reason,
            "category": self.category,
            "confidence": self.confidence,
            "timestamp": self.timestamp.isoformat() if isinstance(self.timestamp, datetime) else self.timestamp,
            "punishment": self.punishment,
            "violation_count": self.violation_count,
            "api_used": self.api_used,
        }


@dataclass
class ModerationResult:
    """审核结果"""
    violation: bool = False
    reason: str = ""
    category: str = ""
    confidence: float = 0.0
    raw_response: str = ""

    @classmethod
    def from_json(cls, data: dict) -> "ModerationResult":
        return cls(
            violation=data.get("violation", False),
            reason=data.get("reason", ""),
            category=data.get("category", ""),
            confidence=float(data.get("confidence", 0.0)),
            raw_response=str(data),
        )


@dataclass
class APIConfig:
    """API配置"""
    name: str = ""
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    vision_model: str = ""
    is_healthy: bool = True
    last_fail: Optional[datetime] = None
    fail_count: int = 0

    @property
    def effective_vision_model(self) -> str:
        return self.vision_model if self.vision_model else self.model


@dataclass
class PunishmentInfo:
    """处罚信息"""
    level: int = 0
    action: str = ""
    duration: int = 0
    display_name: str = ""

    @staticmethod
    def parse(punishment_str: str, level: int = 0) -> "PunishmentInfo":
        if punishment_str == "warn":
            return PunishmentInfo(level=level, action="warn", duration=0, display_name="警告")
        elif punishment_str.startswith("mute_"):
            try:
                duration = int(punishment_str.split("_")[1])
                return PunishmentInfo(level=level, action="mute", duration=duration, display_name=f"禁言{duration}秒")
            except (IndexError, ValueError):
                return PunishmentInfo(level=level, action="mute", duration=600, display_name="禁言10分钟")
        elif punishment_str == "kick":
            return PunishmentInfo(level=level, action="kick", duration=0, display_name="踢出群聊")
        elif punishment_str == "ban":
            return PunishmentInfo(level=level, action="ban", duration=0, display_name="拉黑")
        else:
            return PunishmentInfo(level=level, action="unknown", duration=0, display_name=punishment_str)
