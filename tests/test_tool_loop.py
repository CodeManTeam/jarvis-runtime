import asyncio
import unittest
from copy import deepcopy

from app.tool_loop import AgentLoop


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_final_text_without_tool_calls(self) -> None:
        seen = []

        async def llm(messages, tools):
            seen.append((deepcopy(messages), deepcopy(tools)))
            return {"role": "assistant", "content": "完成"}

        async def executor(name, arguments):
            raise AssertionError("executor should not be called")

        original = [{"role": "user", "content": "你好"}]
        schemas = [{"type": "function", "function": {"name": "clock"}}]
        result = await AgentLoop(llm=llm, tool_executor=executor).run(
            original, schemas
        )

        self.assertTrue(result.completed)
        self.assertEqual(result.final_text, "完成")
        self.assertEqual(result.rounds, 1)
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(len(seen[0][0]), 1)
        self.assertEqual(original, [{"role": "user", "content": "你好"}])
        self.assertEqual(schemas[0]["function"]["name"], "clock")


    async def test_runs_multiple_tool_rounds_and_returns_context(self) -> None:
        responses = iter(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "weather.get",
                                "arguments": '{"city":"Shanghai"}',
                            },
                        }
                    ],
                },
                {"role": "assistant", "content": "上海晴朗，25°C"},
            ]
        )
        calls = []

        async def llm(messages, tools):
            return next(responses)

        async def executor(name, arguments):
            calls.append((name, arguments))
            return {"temperature": 25, "condition": "sunny"}

        result = await AgentLoop(
            llm=llm,
            tool_executor=executor,
            max_rounds=2,
        ).run([{"role": "user", "content": "天气"}])

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.final_text, "上海晴朗，25°C")
        self.assertEqual(result.rounds, 2)
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(calls, [("weather.get", {"city": "Shanghai"})])
        self.assertEqual(
            result.messages[-1],
            {"role": "assistant", "content": "上海晴朗，25°C"},
        )
        self.assertEqual(result.messages[-2]["role"], "tool")
        self.assertIn('"temperature": 25', result.messages[-2]["content"])


    async def test_tool_failure_is_returned_to_model_and_can_recover(self) -> None:
        responses = iter(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-fail",
                            "function": {
                                "name": "location.get",
                                "arguments": {},
                            },
                        }
                    ],
                },
                {"role": "assistant", "content": "暂时无法获取位置"},
            ]
        )

        async def llm(messages, tools):
            return next(responses)

        async def executor(name, arguments):
            raise RuntimeError("permission denied")

        result = await AgentLoop(llm=llm, tool_executor=executor).run([])

        self.assertTrue(result.completed)
        self.assertEqual(result.final_text, "暂时无法获取位置")
        self.assertEqual(result.tool_errors[0]["name"], "location.get")
        self.assertIn("permission denied", result.messages[-2]["content"])


    async def test_llm_timeout_is_structured(self) -> None:
        async def llm(messages, tools):
            await asyncio.sleep(0.05)

        async def executor(name, arguments):
            return None

        result = await AgentLoop(
            llm=llm,
            tool_executor=executor,
            llm_timeout=0.001,
        ).run([])

        self.assertEqual(result.status, "llm_timeout")
        self.assertIsNone(result.final_text)
        self.assertEqual(result.rounds, 1)

    async def test_tool_timeout_is_returned_to_model(self) -> None:
        responses = iter(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-timeout",
                            "function": {"name": "slow", "arguments": {}},
                        }
                    ],
                },
                {"role": "assistant", "content": "工具超时，暂时无法完成"},
            ]
        )

        async def llm(messages, tools):
            return next(responses)

        async def executor(name, arguments):
            await asyncio.sleep(0.05)

        result = await AgentLoop(
            llm=llm,
            tool_executor=executor,
            tool_timeout=0.001,
        ).run([])

        self.assertTrue(result.completed)
        self.assertEqual(result.tool_errors[0]["name"], "slow")
        self.assertIn("timed out", result.messages[-2]["content"])


    async def test_max_rounds_stops_repeating_tool_calls(self) -> None:
        async def llm(messages, tools):
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": f"call-{len(messages)}",
                        "function": {"name": "ping", "arguments": "{}"},
                    }
                ],
            }

        async def executor(name, arguments):
            return {"ok": True}

        result = await AgentLoop(
            llm=llm,
            tool_executor=executor,
            max_rounds=2,
        ).run([])

        self.assertEqual(result.status, "max_rounds")
        self.assertEqual(result.rounds, 2)
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(result.error, "maximum tool loop rounds reached (2)")
