import json
import time
import base64
from datetime import datetime, timedelta
from typing import Optional

import httpx

from astrbot.api import logger

from .models import APIConfig, ModerationResult


class LLMClient:
    """多API调用客户端，支持故障转移"""

    def __init__(self, apis: list[APIConfig]):
        self.apis = apis
        self.current_index = 0
        self._client: Optional[httpx.AsyncClient] = None
        self._request_timestamps: list[float] = []
        self.max_rpm = 30

    async def start(self):
        """启动HTTP客户端"""
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self):
        """关闭HTTP客户端"""
        if self._client:
            await self._client.aclose()
            self._client = None

    def update_apis(self, apis: list[APIConfig]):
        """更新API配置"""
        self.apis = apis
        self.current_index = 0

    def _check_rate_limit(self) -> bool:
        """检查是否超过频率限制"""
        now = time.time()
        self._request_timestamps = [t for t in self._request_timestamps if now - t < 60]
        return len(self._request_timestamps) < self.max_rpm

    def _record_request(self):
        """记录一次请求"""
        self._request_timestamps.append(time.time())

    def _mark_api_unhealthy(self, api: APIConfig):
        """标记API为不健康"""
        api.is_healthy = False
        api.last_fail = datetime.now()
        api.fail_count += 1
        logger.warning(f"API [{api.name}] 标记为不健康，失败次数: {api.fail_count}")

    def _mark_api_healthy(self, api: APIConfig):
        """标记API为健康"""
        api.is_healthy = True
        api.fail_count = 0

    def _get_next_healthy_api(self, is_vision: bool = False) -> Optional[APIConfig]:
        """获取下一个健康的API"""
        if not self.apis:
            return None

        healthy_apis = [a for a in self.apis if a.is_healthy]
        if not healthy_apis:
            for api in self.apis:
                if api.last_fail and datetime.now() - api.last_fail > timedelta(minutes=5):
                    api.is_healthy = True
                    api.fail_count = 0
            healthy_apis = [a for a in self.apis if a.is_healthy]

        if not healthy_apis:
            logger.error("所有API均不可用")
            return None

        return healthy_apis[0]

    async def chat_completion(
        self,
        system_prompt: str,
        user_message: str,
        is_vision: bool = False,
        image_base64: Optional[str] = None,
    ) -> ModerationResult:
        """调用LLM进行审核，支持故障转移"""
        if not self._check_rate_limit():
            logger.warning("已达到API调用频率限制")
            return ModerationResult(violation=False, reason="API调用频率限制")

        api = self._get_next_healthy_api(is_vision)
        if not api:
            return ModerationResult(violation=False, reason="所有API均不可用")

        model = api.effective_vision_model if is_vision else api.model
        url = f"{api.base_url.rstrip('/')}/chat/completions"

        messages = [{"role": "system", "content": system_prompt}]

        if is_vision and image_base64:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请审核这张图片"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                        },
                    ],
                }
            )
        else:
            messages.append({"role": "user", "content": user_message})

        headers = {
            "Authorization": f"Bearer {api.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 500,
        }

        for attempt in range(len(self.apis)):
            current_api = self._get_next_healthy_api(is_vision)
            if not current_api:
                break

            try:
                self._record_request()
                current_url = f"{current_api.base_url.rstrip('/')}/chat/completions"
                current_headers = {
                    "Authorization": f"Bearer {current_api.api_key}",
                    "Content-Type": "application/json",
                }
                payload["model"] = current_api.effective_vision_model if is_vision else current_api.model

                response = await self._client.post(
                    current_url, headers=current_headers, json=payload
                )
                response.raise_for_status()

                result = response.json()
                content = result["choices"][0]["message"]["content"]

                self._mark_api_healthy(current_api)

                parsed = self._parse_response(content, current_api.name)
                return parsed

            except httpx.HTTPStatusError as e:
                logger.error(f"API [{current_api.name}] HTTP错误: {e.response.status_code}")
                self._mark_api_unhealthy(current_api)
                continue
            except httpx.RequestError as e:
                logger.error(f"API [{current_api.name}] 请求错误: {e}")
                self._mark_api_unhealthy(current_api)
                continue
            except (KeyError, json.JSONDecodeError) as e:
                logger.error(f"API [{current_api.name}] 响应解析错误: {e}")
                self._mark_api_unhealthy(current_api)
                continue
            except Exception as e:
                logger.error(f"API [{current_api.name}] 未知错误: {e}")
                self._mark_api_unhealthy(current_api)
                continue

        return ModerationResult(violation=False, reason="所有API调用失败")

    def _parse_response(self, content: str, api_name: str) -> ModerationResult:
        """解析LLM响应"""
        try:
            json_str = content.strip()
            if "```json" in json_str:
                start = json_str.index("```json") + 7
                end = json_str.index("```", start)
                json_str = json_str[start:end].strip()
            elif "```" in json_str:
                start = json_str.index("```") + 3
                end = json_str.index("```", start)
                json_str = json_str[start:end].strip()

            if json_str.startswith("{") and json_str.endswith("}"):
                data = json.loads(json_str)
                result = ModerationResult.from_json(data)
                return result

            data = json.loads(json_str)
            return ModerationResult.from_json(data)
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"解析LLM响应失败: {e}, 原始内容: {content}")
            return ModerationResult(violation=False, reason=f"响应解析失败: {e}")

    async def download_image(self, url: str) -> Optional[str]:
        """下载图片并转为base64"""
        if not self._client:
            return None
        try:
            response = await self._client.get(url, timeout=10.0)
            response.raise_for_status()
            return base64.b64encode(response.content).decode("utf-8")
        except Exception as e:
            logger.error(f"下载图片失败: {e}")
            return None

    def get_api_status(self) -> list[dict]:
        """获取所有API的状态"""
        return [
            {
                "name": api.name,
                "model": api.model,
                "healthy": api.is_healthy,
                "fail_count": api.fail_count,
                "last_fail": api.last_fail.isoformat() if api.last_fail else "从未",
            }
            for api in self.apis
        ]
