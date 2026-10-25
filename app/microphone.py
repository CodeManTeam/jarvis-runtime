from __future__ import annotations

import re


_DIRECT_ADDRESS = ("jarvis", "贾维斯", "助手", "小助手")
_REQUEST_MARKERS = (
    "帮我", "请", "提醒", "记录", "查询", "查一下", "告诉我", "打开",
    "关闭", "设置", "安排", "播放", "发送", "执行", "取消", "删除",
    "确认", "能不能", "可以吗", "可以", "你能", "你知道", "你帮",
    "你回答", "你告诉", "你看", "现在几点", "什么时间", "什么是",
)
_DIRECT_QUESTIONS = (
    "能听到我", "听得到我", "你在吗", "你到底", "我怎么", "我在哪",
    "我现在", "现在呢", "怎么还", "为什么你", "到底能不能",
)
_QUESTION_MARKERS = (
    "什么", "怎么", "哪里", "哪儿", "几时", "几点", "多少", "为什么",
    "是否", "天气", "时间", "可以吗", "好吗", "吗",
)
_AMBIENT_FRAGMENTS = {
    "嗯", "嗯嗯", "啊", "哦", "哦哦", "好", "好的",
    "你好", "你好你好", "什么情况", "什么情况啊", "打招呼吗", "会接受我吗",
}


def normalize_transcript(text: str) -> str:
    return re.sub(r"\s+", "", text or "").strip("，。！？；：、 ")


def microphone_gate_reason(text: str) -> str:
    """Return a cheap routing reason before spending an Agent request."""
    normalized = normalize_transcript(text).lower()
    if not normalized:
        return "empty"
    if normalized in _AMBIENT_FRAGMENTS:
        return "ambient_fragment"
    if any(marker in normalized for marker in _DIRECT_ADDRESS):
        return "direct_address"
    if any(marker in normalized for marker in _REQUEST_MARKERS):
        return "request_or_follow_up"
    if any(marker in normalized for marker in _DIRECT_QUESTIONS):
        return "direct_question"
    if len(normalized) >= 4 and (
        any(marker in normalized for marker in _QUESTION_MARKERS)
        or normalized.startswith(("喂", "你", "我", "现在"))
    ):
        return "likely_conversation"
    return "ambient_or_incomplete"


def should_send_microphone_to_agent(text: str) -> bool:
    return microphone_gate_reason(text) not in {
        "empty", "ambient_fragment", "ambient_or_incomplete"
    }
