from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable

from .domain import AgentCapability, AutonomyLevel, ExecutionResult


class Agent(ABC):
    name: str
    description: str
    capabilities: dict[str, AgentCapability]

    @abstractmethod
    async def execute(
        self,
        capability: str,
        payload: dict[str, Any],
    ) -> ExecutionResult:
        raise NotImplementedError


class AgentRegistry:
    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}

    def register(self, agent: Agent) -> None:
        self.agents[agent.name] = agent

    def find(self, agent_name: str, capability: str) -> Agent | None:
        agent = self.agents.get(agent_name)
        if agent and capability in agent.capabilities:
            return agent
        return None

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": agent.name,
                "description": agent.description,
                "capabilities": [
                    {
                        "name": capability.name,
                        "description": capability.description,
                        "required_permissions": sorted(capability.required_permissions),
                        "autonomy_level": capability.autonomy_level.value,
                    }
                    for capability in agent.capabilities.values()
                ],
            }
            for agent in self.agents.values()
        ]


class ReviewAgent(Agent):
    name = "review"
    description = "审核设备脚本内容，允许安全的信息采集脚本在设备上执行。"
    capabilities = {
        "device_script": AgentCapability(
            name="device_script",
            description="审核设备脚本请求并返回允许范围",
            input_schema={"device_id": "string", "script_id": "string"},
            output_schema={"approved": "boolean", "reason": "string"},
            autonomy_level=AutonomyLevel.OBSERVE,
        ),
    }

    def __init__(
        self, review_script: Callable[[str, str, str], dict[str, Any]]
    ) -> None:
        self._review_script = review_script

    async def execute(
        self,
        capability: str,
        payload: dict[str, Any],
    ) -> ExecutionResult:
        if capability != "device_script":
            return ExecutionResult(False, error=f"ReviewAgent 不支持能力：{capability}")
        device_id = str(payload.get("device_id") or "")
        script_id = str(payload.get("script_id") or "")
        script = str(payload.get("script") or "")
        if not device_id or not script:
            return ExecutionResult(False, error="审核请求缺少设备或脚本")
        result = self._review_script(device_id, script_id, script)
        return ExecutionResult(
            success=bool(result.get("approved")),
            output=result,
            error=None if result.get("approved") else result.get("reason"),
        )


class ReminderAgent(Agent):
    name = "reminder"
    description = "创建、查询、完成和取消 Jarvis 提醒任务。"
    capabilities = {
        "create": AgentCapability(
            name="create",
            description="创建提醒任务",
            input_schema={"title": "string", "run_at": "ISO-8601|null"},
            output_schema={"task_id": "string"},
            autonomy_level=AutonomyLevel.LOW_RISK_AUTO,
        ),
        "complete": AgentCapability(
            name="complete",
            description="完成提醒任务",
            input_schema={"task_id": "string"},
            required_permissions={"tasks.write"},
            autonomy_level=AutonomyLevel.LOW_RISK_AUTO,
        ),
        "cancel": AgentCapability(
            name="cancel",
            description="取消提醒任务",
            input_schema={"task_id": "string"},
            required_permissions={"tasks.write"},
            autonomy_level=AutonomyLevel.PROPOSE,
        ),
    }

    def __init__(
        self,
        create_task: Callable[[str, str | None, dict[str, Any] | None], dict[str, Any]],
        complete_task: Callable[[str], dict[str, Any] | None],
        cancel_task: Callable[[str], dict[str, Any] | None],
    ) -> None:
        self._create_task = create_task
        self._complete_task = complete_task
        self._cancel_task = cancel_task

    async def execute(
        self,
        capability: str,
        payload: dict[str, Any],
    ) -> ExecutionResult:
        if capability == "create":
            task = self._create_task(
                str(payload.get("title") or ""),
                payload.get("run_at"),
                payload,
            )
            return ExecutionResult(
                success=True,
                output={"task": task},
            )
        if capability == "complete":
            task = self._complete_task(str(payload.get("task_id") or ""))
            return ExecutionResult(
                success=task is not None,
                output={"task": task} if task else None,
                error=None if task else "任务不存在",
            )
        if capability == "cancel":
            task = self._cancel_task(str(payload.get("task_id") or ""))
            return ExecutionResult(
                success=task is not None,
                output={"task": task} if task else None,
                error=None if task else "任务不存在",
            )
        return ExecutionResult(False, error=f"ReminderAgent 不支持能力：{capability}")


class DeviceAgent(Agent):
    name = "device"
    description = "查询和控制已注册的个人设备。"
    capabilities = {
        "status": AgentCapability(
            name="status",
            description="查询设备在线、电量、网络和存储状态",
            input_schema={"device_id": "string"},
            output_schema={"device": "object"},
            autonomy_level=AutonomyLevel.OBSERVE,
        ),
        "vibrate": AgentCapability(
            name="vibrate",
            description="让设备震动",
            input_schema={"device_id": "string"},
            required_permissions={"devices.write"},
            autonomy_level=AutonomyLevel.LOW_RISK_AUTO,
            can_interrupt=True,
        ),
        "notify": AgentCapability(
            name="notify",
            description="在设备上显示本地通知",
            input_schema={"device_id": "string", "text": "string"},
            required_permissions={"devices.write"},
            autonomy_level=AutonomyLevel.PROPOSE,
            can_interrupt=True,
        ),
        "open_url": AgentCapability(
            name="open_url",
            description="在设备上打开链接",
            input_schema={"device_id": "string", "url": "string"},
            required_permissions={"devices.write"},
            autonomy_level=AutonomyLevel.PROPOSE,
        ),
    }

    def __init__(
        self,
        get_device: Callable[[str], dict[str, Any] | None],
        send_command: Callable[[str, str, dict[str, Any] | None], Any],
    ) -> None:
        self._get_device = get_device
        self._send_command = send_command

    async def execute(
        self,
        capability: str,
        payload: dict[str, Any],
    ) -> ExecutionResult:
        device_id = str(payload.get("device_id") or "")
        if not device_id:
            return ExecutionResult(False, error="缺少 device_id")
        if capability == "status":
            device = self._get_device(device_id)
            return ExecutionResult(
                success=device is not None,
                output={"device": device} if device else None,
                error=None if device else "设备不存在",
            )
        actions = {
            "vibrate": ("vibrate", {}),
            "notify": (
                "show_notification",
                {"text": str(payload.get("text") or "Jarvis")},
            ),
            "open_url": (
                "open_url",
                {"url": str(payload.get("url") or "")},
            ),
        }
        if capability not in actions:
            return ExecutionResult(False, error=f"DeviceAgent 不支持能力：{capability}")
        action, command_payload = actions[capability]
        if capability == "open_url" and not command_payload["url"]:
            return ExecutionResult(False, error="缺少 url")
        delivered = await self._send_command(device_id, action, command_payload)
        if isinstance(delivered, dict):
            return ExecutionResult(
                # Queue acceptance means dispatch succeeded; device completion
                # is represented separately by the command ACK lifecycle.
                success=bool(delivered.get("accepted", True)),
                output={
                    "device_id": device_id,
                    "action": action,
                    **delivered,
                    "completed": False,
                },
                error=None,
            )
        return ExecutionResult(
            success=bool(delivered),
            output={"device_id": device_id, "action": action, "delivered": delivered},
            error=None if delivered else "设备当前不在线，命令已进入离线队列",
        )


class NotificationAgent(Agent):
    name = "notification"
    description = "处理来自 Android 的通知事件，并提供通知摘要能力。"
    capabilities = {
        "summarize": AgentCapability(
            name="summarize",
            description="整理通知内容供 Jarvis 理解",
            input_schema={"title": "string", "text": "string", "package": "string"},
            output_schema={"summary": "string"},
            autonomy_level=AutonomyLevel.OBSERVE,
        ),
    }

    async def execute(
        self,
        capability: str,
        payload: dict[str, Any],
    ) -> ExecutionResult:
        if capability != "summarize":
            return ExecutionResult(
                False,
                error=f"NotificationAgent 不支持能力：{capability}",
            )
        title = str(payload.get("title") or "")
        text = str(payload.get("text") or "")
        package = str(payload.get("package") or "")
        return ExecutionResult(
            True,
            output={
                "summary": f"{title}: {text}".strip(": "),
                "package": package,
            },
        )


class CalendarAgent(Agent):
    name = "calendar"
    description = "理解来自设备日历的日程事件。"
    capabilities = {
        "summarize": AgentCapability(
            name="summarize",
            description="整理日历事件供 Jarvis 理解",
            input_schema={"title": "string", "start": "ISO-8601", "location": "string"},
            output_schema={"summary": "string"},
            autonomy_level=AutonomyLevel.OBSERVE,
        ),
    }

    async def execute(
        self,
        capability: str,
        payload: dict[str, Any],
    ) -> ExecutionResult:
        if capability != "summarize":
            return ExecutionResult(False, error=f"CalendarAgent 不支持能力：{capability}")
        title = str(payload.get("title") or "未命名日程")
        start = str(payload.get("start") or "")
        location = str(payload.get("location") or "")
        suffix = f" @ {location}" if location else ""
        return ExecutionResult(
            True,
            output={"summary": f"{title} {start}{suffix}".strip()},
        )


class LocationAgent(Agent):
    name = "location"
    description = "处理设备位置变化事件。"
    capabilities = {
        "summarize": AgentCapability(
            name="summarize",
            description="整理位置变化供 Jarvis 理解",
            input_schema={"latitude": "number", "longitude": "number", "provider": "string"},
            output_schema={"summary": "string"},
            autonomy_level=AutonomyLevel.OBSERVE,
        ),
    }

    async def execute(
        self,
        capability: str,
        payload: dict[str, Any],
    ) -> ExecutionResult:
        if capability != "summarize":
            return ExecutionResult(False, error=f"LocationAgent 不支持能力：{capability}")
        latitude = payload.get("latitude")
        longitude = payload.get("longitude")
        provider = str(payload.get("provider") or "unknown")
        return ExecutionResult(
            True,
            output={
                "summary": f"位置变化：{latitude}, {longitude} ({provider})",
                "latitude": latitude,
                "longitude": longitude,
            },
        )
