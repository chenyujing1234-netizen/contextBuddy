"""chat_sorter_core 的单元测试：解析 / 扫描 / LLM 分类 / 写入去重。"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chat_sorter_core as core


SAMPLE_TXT = """会话: 文件传输助手 (filehelper)
账号: wxid_demo
导出时间: 2026-10-18 16:16:19

[2026-07-22 20:16:11] 余井(wxid_demo): 时代不一样了，两种方式各有缘由。
[2026-07-23 13:47:19] 余井(wxid_demo): 2.txt 6 0 0 0 8283 @cdn_305f02 abc 0 0
[2026-07-23 19:24:07] 余井(wxid_demo): 喜乐今天KET考试考得不错
[2026-07-25 09:10:33] 余井(wxid_demo): 步骤(1): 先做喜乐的复习计划
[2026-07-26 10:00:00] 老爸(wxid_lao): 老妈的检查报告出来了
"""


def _make_candidates(root: Path) -> list[core.CandidateDir]:
    (root / "喜乐").mkdir(parents=True)
    (root / "喜乐的KET考试").mkdir(parents=True)
    (root / "老妈的病情").mkdir(parents=True)
    (root / "老爸").mkdir(parents=True)
    (root / "周围邻居" / "陈轩").mkdir(parents=True)
    (root / ".workbuddy" / "memory").mkdir(parents=True)
    (root / "wxid_demo123").mkdir(parents=True)
    return core.scan_candidate_dirs(root)


def _msg(time: str, content: str, sender: str = "余井", username: str = "wxid_demo") -> core.ChatMessage:
    return core.ChatMessage(
        time_str=time,
        sender_display=sender,
        sender_username=username,
        content=content,
        line=f"[{time}] {sender}({username}): {content}",
    )


def test_parse_chat_txt(tmp_path: Path) -> None:
    src = tmp_path / "文件传输助手.txt"
    src.write_text(SAMPLE_TXT, encoding="utf-8")
    messages = core.parse_chat_txt(src)
    assert len(messages) == 5
    first = messages[0]
    assert first.time_str == "2026-07-22 20:16:11"
    assert first.sender_display == "余井"
    assert first.sender_username == "wxid_demo"
    assert first.content == "时代不一样了，两种方式各有缘由。"
    assert first.line.startswith("[2026-07-22 20:16:11] 余井(wxid_demo): ")
    # 内容里带“(1): ”不会被误当作发送者
    assert messages[3].content == "步骤(1): 先做喜乐的复习计划"
    assert messages[4].sender_display == "老爸"


def test_scan_candidate_dirs(tmp_path: Path) -> None:
    candidates = _make_candidates(tmp_path)
    rels = {c.rel_display for c in candidates}
    assert "." in rels  # 根目录自身也是候选
    assert {"喜乐", "喜乐的KET考试", "老妈的病情", "老爸", "周围邻居/陈轩"} <= rels
    # 隐藏目录、微信 ID 目录整棵子树排除
    assert not any(r.startswith(".workbuddy") for r in rels)
    assert not any("wxid_" in r for r in rels)


def test_scan_candidate_dirs_max_depth(tmp_path: Path) -> None:
    _make_candidates(tmp_path)
    one = core.scan_candidate_dirs(tmp_path, max_depth=1)
    rels = {c.rel_display for c in one}
    # 深度 1：根目录自身 + 顶层目录，二层“周围邻居/陈轩”被剪枝
    assert rels == {".", "喜乐", "喜乐的KET考试", "老妈的病情", "老爸", "周围邻居"}
    two = core.scan_candidate_dirs(tmp_path, max_depth=2)
    assert "周围邻居/陈轩" in {c.rel_display for c in two}


def test_parse_llm_results() -> None:
    # 纯 JSON
    parsed = core.parse_llm_results('{"results":[{"index":1,"dir":"喜乐","reason":"提到孩子"}]}')
    assert parsed == {1: ("喜乐", "提到孩子")}
    # 带 Markdown 代码块包装
    wrapped = '```json\n{"results":[{"index":2,"dir":"","reason":"无关"}]}\n```'
    assert core.parse_llm_results(wrapped) == {2: ("", "无关")}
    # 非法编号被跳过
    bad = '{"results":[{"index":"x","dir":"喜乐","reason":""},{"index":3,"dir":"老爸","reason":"r"}]}'
    assert core.parse_llm_results(bad) == {3: ("老爸", "r")}


def test_build_llm_messages(tmp_path: Path) -> None:
    candidates = _make_candidates(tmp_path)
    messages = [_msg("2026-07-22 20:16:11", "内容A"), _msg("2026-07-22 20:20:00", "内容B")]
    llm_messages = core.build_llm_messages(candidates, messages)
    assert llm_messages[0]["role"] == "system"
    user_text = llm_messages[1]["content"]
    assert "候选目录列表" in user_text
    assert "(根目录)" in user_text  # 根目录用显示名
    assert "- 喜乐" in user_text
    assert "1. [2026-07-22 20:16:11] 内容A" in user_text
    assert "2. [2026-07-22 20:20:00] 内容B" in user_text


def test_classify_with_llm(tmp_path: Path) -> None:
    candidates = _make_candidates(tmp_path)
    messages = [
        _msg("2026-07-23 19:24:07", "喜乐今天KET考试考得不错"),
        _msg("2026-07-23 13:47:19", "2.txt 6 0 0 0 8283 @cdn_305f02 abc 0 0"),  # 系统附件
        _msg("2026-07-22 20:16:11", "随便聊聊天气"),
    ]
    progress = []

    def fake_call(llm_messages: list[dict]) -> str:
        user_text = llm_messages[1]["content"]
        assert "候选目录列表" in user_text and "喜乐" in user_text
        return (
            '{"results":['
            '{"index":1,"dir":"喜乐","reason":"提到孩子喜乐"},'
            '{"index":2,"dir":"不存在的目录","reason":"x"},'
            '{"index":9,"dir":"老爸","reason":"y"}]}'
        )

    results = core.classify_with_llm(
        messages,
        candidates,
        call_llm=fake_call,
        batch_size=20,
        concurrency=2,
        progress_cb=lambda d, t, f, chunk: progress.append((d, t, f, chunk)),
    )
    # 第一条：LLM 指向“喜乐”
    assert results[0].candidate is not None and results[0].candidate.rel_display == "喜乐"
    assert results[0].reason == "提到孩子喜乐"
    # 第二条：系统附件消息不发送 LLM，直接标未分类
    assert results[1].candidate is None
    assert results[1].reason.startswith("系统/附件")
    # 第三条：LLM 给了无效目录 -> 不采纳
    assert results[2].candidate is None
    assert "无效目录" in results[2].reason
    assert progress and progress[-1][:3] == (1, 1, 0)  # 1 批完成、0 失败
    # chunk 只含本批发给 LLM 的 2 条消息（系统附件不在批内）
    chunk = progress[-1][3]
    assert len(chunk) == 2
    assert {r.message.content for r in chunk} == {"喜乐今天KET考试考得不错", "随便聊聊天气"}


def test_classify_with_llm_cancel(tmp_path: Path) -> None:
    candidates = _make_candidates(tmp_path)
    messages = [_msg("2026-07-22 20:16:11", "随便聊聊天气"), _msg("2026-07-22 20:20:00", "再聊一句")]
    cancel_event = threading.Event()
    cancel_event.set()  # 分析前已取消：不应发起任何 LLM 调用
    calls = []

    def fake_call(llm_messages: list[dict]) -> str:
        calls.append(llm_messages)
        return '{"results":[]}'

    results = core.classify_with_llm(
        messages, candidates, call_llm=fake_call, cancel_event=cancel_event
    )
    assert not calls
    assert all(r.candidate is None and r.reason == "已取消" for r in results)
    # 摘要包含取消计数
    assert "已取消 2 条" in core.summarize(results)


def test_classify_with_llm_retry_then_fail(tmp_path: Path) -> None:
    candidates = _make_candidates(tmp_path)
    messages = [_msg("2026-07-22 20:16:11", "随便聊聊天气")]
    progress = []

    def bad_call(_llm_messages: list[dict]) -> str:
        raise OSError("network down")

    results = core.classify_with_llm(
        messages,
        candidates,
        call_llm=bad_call,
        concurrency=1,
        progress_cb=lambda d, t, f, chunk: progress.append((d, t, f, chunk)),
    )
    assert results[0].candidate is None
    assert results[0].reason.startswith("LLM调用失败")
    assert progress and progress[-1][:3] == (1, 1, 1)  # 重试耗尽的批计入失败数
    assert len(progress[-1][3]) == 1  # 失败批的结果也通过 chunk 传出（供实时显示）
    assert "LLM 调用失败 1 条" in core.summarize(results)


def test_message_key_stable() -> None:
    a = _msg("2026-07-22 20:16:11", "内容A")
    b = _msg("2026-07-22 20:16:11", "内容A")
    c = _msg("2026-07-22 20:16:11", "内容B")
    d = _msg("2026-07-22 20:16:12", "内容A")
    assert core.message_key(a) == core.message_key(b)
    assert core.message_key(a) != core.message_key(c)
    assert core.message_key(a) != core.message_key(d)


def test_is_noise_content() -> None:
    # 常规系统/附件标记
    assert core.is_noise_content('<?xml version="1.0"?><msg><title>标题</title></msg>')
    assert core.is_noise_content("2.txt 6 0 0 0 8283 @cdn_305f02 abc 0 0")
    assert core.is_noise_content("")  # 空内容
    # 抖音分享文案：自动废弃
    assert core.is_noise_content("喜乐的直播，复制打开抖音，看看【喜乐的作品】")
    assert core.is_noise_content("复制打开抖音搜索更多精彩内容")
    # 正常消息不废弃
    assert not core.is_noise_content("时代不一样了，两种方式各有缘由。")
    assert not core.is_noise_content("复制这个文档给喜乐看看，然后打开抖音说过的教程")


def test_set_noise_markers() -> None:
    try:
        core.set_noise_markers(["拼多多", "  ", "砍一刀"])
        assert core.is_noise_content("帮我看下这个拼多多链接")
        assert core.is_noise_content("来砍一刀呗")
        assert not core.is_noise_content('<?xml version="1.0"?>')  # 自定义完全替代默认
        assert not core.is_noise_content("时代不一样了，两种方式各有缘由。")
        # 大小写不敏感
        core.set_noise_markers(["Open"])
        assert core.is_noise_content("please OPEN the door")
    finally:
        core.set_noise_markers(core.DEFAULT_NOISE_MARKERS)


def test_ensure_self() -> None:
    # 涉及人员固定包含“我”，放最前
    assert core.ensure_self(["张三", "李四"]) == ["我", "张三", "李四"]
    # 已有“我”时去重，不重复添加
    assert core.ensure_self(["张三", "我"]) == ["我", "张三"]
    # 空标注保持为空（视为未标注，不导出、不写后缀）
    assert core.ensure_self([]) == []
    # 空名被清理
    assert core.ensure_self(["", "张三", "张三"]) == ["我", "张三"]


def test_cache_save_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "classify_cache.json"
    cache = {
        core.message_key(_msg("2026-07-22 20:16:11", "x")): {
            "dir": "喜乐", "reason": "提到喜乐", "confirmed": True
        }
    }
    core.save_cache(path, cache)
    assert core.load_cache(path) == cache
    # 损坏文件返回空而不是抛异常
    path.write_text("{bad json", encoding="utf-8")
    assert core.load_cache(path) == {}


def test_cache_lookup_variants(tmp_path: Path) -> None:
    candidates = _make_candidates(tmp_path)
    cand_index = {core.display_rel(c): c for c in candidates}
    msg = _msg("2026-07-22 20:16:11", "内容A")
    key = core.message_key(msg)

    # 未确认缓存命中：沿用上次目录与理由
    cache = {key: {"dir": "喜乐", "reason": "提到喜乐", "confirmed": False}}
    hit = core.cache_lookup(cache, msg, cand_index)
    assert hit is not None and hit.candidate is not None
    assert hit.candidate.rel_display == "喜乐"
    assert hit.reason == "上次分析：提到喜乐"

    # 已确认缓存：显示确认过的目录
    cache[key]["confirmed"] = True
    hit2 = core.cache_lookup(cache, msg, cand_index)
    assert hit2 is not None and hit2.candidate is not None
    assert "上次已确认写入" in hit2.reason

    # 上次目录已不存在：缓存失效，需重新分析
    cache[key] = {"dir": "不存在的目录", "reason": "r", "confirmed": False}
    assert core.cache_lookup(cache, msg, cand_index) is None

    # 未分类缓存（dir 为空）也命中，避免重复分析
    cache[key] = {"dir": "", "reason": "LLM判断为未分类", "confirmed": False}
    hit3 = core.cache_lookup(cache, msg, cand_index)
    assert hit3 is not None and hit3.candidate is None
    assert hit3.reason == "上次分析：LLM判断为未分类"

    # 无缓存
    assert core.cache_lookup({}, msg, cand_index) is None


def test_classify_with_llm_uses_cache(tmp_path: Path) -> None:
    candidates = _make_candidates(tmp_path)
    cached_msg = _msg("2026-07-22 20:16:11", "喜乐今天KET考得不错")
    fresh_msg = _msg("2026-07-22 20:20:00", "给老爸带的茶叶到了")
    cache = {
        core.message_key(cached_msg): {"dir": "喜乐", "reason": "上次理由", "confirmed": False}
    }
    calls = []

    def fake_call(llm_messages: list[dict]) -> str:
        calls.append(llm_messages)
        return '{"results":[{"index":1,"dir":"老爸","reason":"给老爸带的"}]}'

    results = core.classify_with_llm(
        [cached_msg, fresh_msg], candidates, call_llm=fake_call, cache=cache
    )
    # 缓存命中的消息不再发给 LLM，只有新消息进批
    assert len(calls) == 1
    assert "喜乐今天KET考得不错" not in str(calls[0])
    assert "茶叶到了" in str(calls[0])
    # 命中结果沿用缓存目录
    assert results[0].candidate is not None and results[0].candidate.rel_display == "喜乐"
    assert results[0].reason == "上次分析：上次理由"
    # 新结果写回缓存（未确认）
    entry = cache[core.message_key(fresh_msg)]
    assert entry["dir"] == "老爸" and entry["confirmed"] is False


def test_classify_with_llm_cancel_no_cache_pollution(tmp_path: Path) -> None:
    candidates = _make_candidates(tmp_path)
    messages = [_msg("2026-07-22 20:16:11", "随便聊聊天气")]
    cancel_event = threading.Event()
    cancel_event.set()
    cache: dict = {}

    results = core.classify_with_llm(
        messages, candidates, call_llm=lambda m: '{"results":[]}',
        cancel_event=cancel_event, cache=cache,
    )
    assert all(r.reason == "已取消" for r in results)
    assert not cache  # 取消的结果不进缓存，下次仍会正常分析


def test_apply_writes_dedupe_and_append(tmp_path: Path) -> None:
    person = tmp_path / "喜乐"
    person.mkdir()
    target = person / "微信聊天记录.txt"
    line_old = "[2026-01-01 09:00:00] 余井(wxid_demo): 已存在的内容"
    line_new = "[2026-07-23 19:24:07] 余井(wxid_demo): 喜乐今天KET考试考得不错"
    line_early = "[2026-06-01 08:00:00] 余井(wxid_demo): 更早的一条"
    target.write_text(line_old + "\n", encoding="utf-8")

    cand = core.CandidateDir(path=person, rel_display="喜乐", depth=1)

    def msg(time: str, content: str) -> core.ChatMessage:
        return _msg(time, content)

    assignments = [
        (cand, msg("2026-07-23 19:24:07", "喜乐今天KET考试考得不错")),
        (cand, msg("2026-06-01 08:00:00", "更早的一条")),
        (cand, msg("2026-01-01 09:00:00", "已存在的内容")),
    ]
    reports = core.apply_writes(assignments, output_filename="微信聊天记录.txt", source_label="文件传输助手.txt")
    assert len(reports) == 1
    rep = reports[0]
    assert rep.file_path == target
    assert rep.written == 2  # 已存在的一条被去重跳过
    assert rep.skipped_existing == 1
    assert rep.error == ""

    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[0] == line_old
    # 新写入的按时间排序追加
    assert lines[1] == line_early
    assert lines[2] == line_new


def test_apply_writes_new_file_header(tmp_path: Path) -> None:
    person = tmp_path / "老爸"
    person.mkdir()
    cand = core.CandidateDir(path=person, rel_display="老爸", depth=1)
    msg = _msg("2026-07-26 10:00:00", "报告出来了", sender="老爸", username="wxid_lao")
    reports = core.apply_writes([(cand, msg)], output_filename="记录.txt", source_label="文件传输助手.txt")
    assert reports[0].written == 1
    lines = (person / "记录.txt").read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("# 微信聊天记录（来源：文件传输助手.txt")
    assert lines[1] == msg.line


# ---------- 人物标注 ----------


def test_chinese_char_count() -> None:
    assert core.chinese_char_count("") == 0
    assert core.chinese_char_count("abc 123 ,.?") == 0  # 无汉字
    assert core.chinese_char_count("，。！？") == 0  # 中文标点不算汉字
    assert core.chinese_char_count("好的") == 2
    assert core.chinese_char_count("喜乐今天KET考试考得不错！") == 10  # 字母标点不计，恰好 10 个
    assert core.chinese_char_count("步骤(1): 先做喜乐的复习计划") == 11


def test_chat_message_people_default() -> None:
    msg = _msg("2026-07-22 20:16:11", "内容A")
    assert msg.people == []
    msg.people = ["喜乐", "老爸"]
    assert msg.people == ["喜乐", "老爸"]


def test_build_llm_messages_people(tmp_path: Path) -> None:
    candidates = _make_candidates(tmp_path)
    m1 = _msg("2026-07-23 19:24:07", "考得不错")
    m1.people = ["喜乐", "老爸"]
    m2 = _msg("2026-07-23 20:00:00", "随便聊聊")
    user_text = core.build_llm_messages(candidates, [m1, m2])[1]["content"]
    assert "（涉及人物：喜乐、老爸）" in user_text
    assert user_text.count("（涉及人物：") == 1  # 未标注的消息不带后缀
    assert "涉及人物" in core.LLM_SYSTEM_PROMPT  # 系统提示词有人物规则


def test_cache_data_people_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "classify_cache.json"
    msg = _msg("2026-07-22 20:16:11", "内容A")
    key = core.message_key(msg)
    entries = {key: {"dir": "喜乐", "reason": "r", "confirmed": False, "people": ["喜乐"]}}
    core.save_cache(path, entries, people=["张三"], msg_people={key: ["喜乐", "张三"]})
    data = core.load_cache_data(path)
    assert data["people"] == ["张三"]
    assert data["msg_people"] == {key: ["喜乐", "张三"]}
    assert data["entries"] == entries
    assert core.load_cache(path) == entries  # 兼容旧调用方
    # 损坏文件返回空结构
    path.write_text("not json", encoding="utf-8")
    assert core.load_cache_data(path) == {
        "people": [], "msg_people": {}, "msg_discarded": {}, "msg_exported": {}, "entries": {}
    }


def test_cache_data_discarded_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "classify_cache.json"
    msg = _msg("2026-07-22 20:16:11", "内容A")
    key = core.message_key(msg)
    core.save_cache(path, {}, msg_discarded={key: True, "k2": False})
    data = core.load_cache_data(path)
    # True=手动废弃，False=保留系统/附件行，两种标记都要原样往返
    assert data["msg_discarded"] == {key: True, "k2": False}
    # 旧缓存文件（无 msg_discarded 字段）：返回空 dict，不报错
    path.write_text(json.dumps({"version": 2, "entries": {}}), encoding="utf-8")
    assert core.load_cache_data(path)["msg_discarded"] == {}


def test_cache_data_exported_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "classify_cache.json"
    msg = _msg("2026-07-22 20:16:11", "内容A")
    key = core.message_key(msg)
    core.save_cache(path, {}, msg_exported={key: "2026-10-20", "k2": "2026-10-19"})
    data = core.load_cache_data(path)
    # 已导出日期原样往返（message_key -> 导出日期字符串）
    assert data["msg_exported"] == {key: "2026-10-20", "k2": "2026-10-19"}
    # 旧缓存文件（无 msg_exported 字段）：返回空 dict，不报错
    path.write_text(json.dumps({"version": 2, "entries": {}}), encoding="utf-8")
    assert core.load_cache_data(path)["msg_exported"] == {}


def test_cache_lookup_people_mismatch(tmp_path: Path) -> None:
    candidates = _make_candidates(tmp_path)
    cand_index = {core.display_rel(c): c for c in candidates}
    msg = _msg("2026-07-22 20:16:11", "内容A")
    key = core.message_key(msg)
    # 未确认条目：人物标注变了 -> 缓存失效，需重新分析
    cache = {key: {"dir": "喜乐", "reason": "r", "confirmed": False, "people": ["喜乐"]}}
    assert core.cache_lookup(cache, msg, cand_index) is None  # msg.people 为空
    msg.people = ["喜乐"]
    assert core.cache_lookup(cache, msg, cand_index) is not None  # 标注一致 -> 命中
    # 已确认条目：标注变化不影响命中
    cache[key]["confirmed"] = True
    msg.people = ["老爸"]
    hit = core.cache_lookup(cache, msg, cand_index)
    assert hit is not None and "上次已确认写入" in hit.reason
    # v1 旧条目（无 people 字段）与未标注消息兼容
    cache[key] = {"dir": "喜乐", "reason": "r", "confirmed": False}
    msg.people = []
    assert core.cache_lookup(cache, msg, cand_index) is not None


def test_classify_with_llm_cache_stores_people(tmp_path: Path) -> None:
    candidates = _make_candidates(tmp_path)
    msg = _msg("2026-07-22 20:16:11", "喜乐考得不错")
    msg.people = ["喜乐"]
    cache: dict = {}
    results = core.classify_with_llm(
        [msg], candidates,
        call_llm=lambda m: '{"results":[{"index":1,"dir":"喜乐","reason":"提到喜乐"}]}',
        cache=cache,
    )
    assert results[0].candidate is not None
    entry = cache[core.message_key(msg)]
    assert entry["people"] == ["喜乐"]  # 新结果记下分析时的人物标注


def test_apply_writes_people_suffix(tmp_path: Path) -> None:
    person = tmp_path / "喜乐"
    person.mkdir()
    target = person / "微信聊天记录.txt"
    # 旧文件里已有：一条带人物标注的旧行 + 一条与待写入消息相同的原始行
    annotated_old = "[2026-06-01 08:00:00] 余井(wxid_demo): 更早的一条（涉及人物：老妈）"
    plain_old = "[2026-07-23 19:24:07] 余井(wxid_demo): 喜乐今天KET考试考得不错"
    target.write_text(annotated_old + "\n" + plain_old + "\n", encoding="utf-8")
    cand = core.CandidateDir(path=person, rel_display="喜乐", depth=1)

    m1 = _msg("2026-07-23 19:24:07", "喜乐今天KET考试考得不错")  # 原始行已在文件中
    m1.people = ["喜乐", "老爸"]
    m2 = _msg("2026-06-01 08:00:00", "更早的一条")  # 旧行带标注，去标注后与本行相同
    m3 = _msg("2026-08-01 09:00:00", "新消息")
    m3.people = ["喜乐"]

    reports = core.apply_writes(
        [(cand, m) for m in (m1, m2, m3)],
        output_filename="微信聊天记录.txt", source_label="文件传输助手.txt",
    )
    rep = reports[0]
    assert rep.written == 1 and rep.skipped_existing == 2
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == "[2026-08-01 09:00:00] 余井(wxid_demo): 新消息（涉及人物：喜乐）"
    # 人物标注变化后重复写入同一消息，仍不会产生重复行
    m1b = _msg("2026-07-23 19:24:07", "喜乐今天KET考试考得不错")
    m1b.people = ["老爸"]
    rep2 = core.apply_writes([(cand, m1b)], output_filename="微信聊天记录.txt", source_label="s")
    assert rep2[0].written == 0 and rep2[0].skipped_existing == 1


def test_scan_existing_message_lines(tmp_path: Path) -> None:
    # 两个之前保存的导出 txt + 一个带人物标注的“确认写入”txt + 一个非 txt 文件
    (tmp_path / "涉及人员消息_2026-10-20.txt").write_text(
        "==== 涉及人员：喜乐 ====\n"
        "[2026-07-23 19:24:07] 余井(wxid_demo): 喜乐今天KET考试考得不错\n",
        encoding="utf-8",
    )
    (tmp_path / "涉及人员消息_2026-10-21.txt").write_text(
        "==== 涉及人员：老妈 ====\n"
        "[2026-07-26 10:00:00] 老爸(wxid_lao): 老妈的检查报告出来了\n",
        encoding="utf-8",
    )
    (tmp_path / "微信聊天记录.txt").write_text(
        "[2026-06-01 08:00:00] 余井(wxid_demo): 更早的一条（涉及人物：老妈）\n",
        encoding="utf-8",
    )
    (tmp_path / "笔记.md").write_text(
        "[2026-08-01 09:00:00] 余井(wxid_demo): 新消息\n", encoding="utf-8"
    )
    lines = core.scan_existing_message_lines(tmp_path)
    # 各 txt 中的消息行均视为已存在；带人物标注的行其原始行也算已存在
    assert "[2026-07-23 19:24:07] 余井(wxid_demo): 喜乐今天KET考试考得不错" in lines
    assert "[2026-07-26 10:00:00] 老爸(wxid_lao): 老妈的检查报告出来了" in lines
    assert "[2026-06-01 08:00:00] 余井(wxid_demo): 更早的一条" in lines
    # 非 txt 文件不参与去重
    assert "[2026-08-01 09:00:00] 余井(wxid_demo): 新消息" not in lines
    # 目录不存在时安静返回空集合
    assert core.scan_existing_message_lines(tmp_path / "不存在") == set()


def test_summarize_annotated() -> None:
    m1 = _msg("2026-07-23 19:24:07", "考得不错")
    m1.people = ["喜乐"]
    r1 = core.ClassifyResult(message=m1, reason="r")
    r2 = core.ClassifyResult(message=_msg("2026-07-23 20:00:00", "x"), reason="")
    text = core.summarize([r1, r2])
    assert "已标注人物 1 条" in text
