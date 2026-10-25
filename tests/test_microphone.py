from app.microphone import should_send_microphone_to_agent


def test_microphone_prefilter_drops_obvious_ambient_fragments():
    assert not should_send_microphone_to_agent("什么情况啊")
    assert not should_send_microphone_to_agent("会接受我吗")
    assert not should_send_microphone_to_agent("打招呼吗")


def test_microphone_prefilter_keeps_direct_requests():
    assert should_send_microphone_to_agent("帮我记录一下")
    assert should_send_microphone_to_agent("Jarvis，你是 Jarvis 吗")
    assert should_send_microphone_to_agent("请提醒我晚上八点开会")
    assert should_send_microphone_to_agent("能听到我说话吗")
    assert should_send_microphone_to_agent("现在天气怎么样")


def test_microphone_prefilter_keeps_natural_questions_and_follow_ups():
    assert should_send_microphone_to_agent("你可以自动调用我的消息来看我的这个定位事件吗")
    assert should_send_microphone_to_agent("你知道我现在在哪个位置吗")
    assert should_send_microphone_to_agent("长沙哪里")
    assert should_send_microphone_to_agent("喂，到底什么情况？你到底能不能回答我")
    assert should_send_microphone_to_agent("我怎么还在武汉？你可以自")


def test_microphone_prefilter_still_drops_noise():
    assert not should_send_microphone_to_agent("你好你好")
    assert not should_send_microphone_to_agent("好的")
