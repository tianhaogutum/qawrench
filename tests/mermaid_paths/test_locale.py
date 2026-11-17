"""English and Simplified Chinese interface text, source-language tagging and RTL isolation."""
from qawrench.mermaid_paths import apply_review, create_review, parse_flowchart, render_appendix
from qawrench.mermaid_paths.i18n import MESSAGES, interface_language

from .support import html_problems, raises


def test_interface_dictionaries_cover_the_same_messages_and_reject_false_language_declarations():
    assert sorted(MESSAGES["en"]) == sorted(MESSAGES["zh-CN"])
    for tag in ("en", "en-GB", "zh", "zh-CN", "zh-Hans"):
        assert interface_language(tag)
    for tag in ("de", "zh-TW", "en-Cyrl", 'en"', "not a tag"):
        raises("LANGUAGE", interface_language, tag)


def test_chinese_review_templates_and_drafts_localize_neutral_text_without_translating_source_labels():
    graph = parse_flowchart("flowchart TD\n 甲[开始] --> 乙[结束]")
    review = create_review(graph, lang="zh-CN")
    assert "检查每条连接" in review["instructions"]
    assert review["edges"][0]["text"] == "未标注文字的连接"
    assert review["edges"][0]["confirmed"] is False
    draft = render_appendix(apply_review(graph, None, draft=True), lang="zh-CN")
    assert 'lang="zh-CN"' in draft
    for text in ("语义节点列表", "分支邻接表", "草稿：还有 1 条", "未标注文字的连接", "开始", "结束"):
        assert text in draft, text
    assert "Unlabeled connection" not in draft


def test_source_labels_remain_literal_even_when_they_equal_an_interface_phrase_or_contain_template_braces():
    graph = parse_flowchart('flowchart TD\n A["Return to node list"] -->|"{count} No directed loops."| B')
    html = render_appendix(apply_review(graph, None, draft=True), lang="zh-CN", label_lang="en-us")
    assert '<bdi lang="en-US">Return to node list</bdi>' in html
    assert '<bdi lang="en-US">{count} No directed loops.</bdi>' in html
    assert "返回节点列表" in html


def test_rtl_source_labels_are_isolated_and_tagged_independently_of_the_interface():
    graph = parse_flowchart("flowchart TD\n أ[بدء] --> ب[نهاية]\n ب --> أ")
    html = render_appendix(apply_review(graph, None, draft=True), lang="en", label_lang="ar")
    assert '<html lang="en">' in html
    assert '<bdi lang="ar">بدء</bdi>' in html
    assert html_problems(html) == []
    raises("LANGUAGE", render_appendix, apply_review(graph, None, draft=True), label_lang='ar" onclick="x')


def test_chinese_fragments_have_localized_warnings_and_conforming_heading_and_link_semantics():
    graph = parse_flowchart("flowchart TD\n A-->B\n style A fill:#fff")
    fragment = render_appendix(apply_review(graph, None, draft=True), lang="zh-CN", fragment=True)
    assert "请用文字表达有意义的区别" in fragment
    html = f'<!DOCTYPE html><html lang="zh-CN"><head><title>文档</title></head><body><h1>文档</h1>{fragment}</body></html>'
    assert html_problems(html) == []
