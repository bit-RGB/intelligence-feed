from intelligence_feed.models import SourceItem
from intelligence_feed.repository import upsert_item
from intelligence_feed.translation import (
    LocalTranslator,
    _prepare_title,
    _restore_terms,
    should_translate,
    translate_pending,
)


class FakeTranslator:
    def __init__(self):
        self.calls = []

    def translate_many(self, titles):
        self.calls.append(list(titles))
        return [f"译文：{title}" for title in titles]


def add(conn, url, title):
    return upsert_item(conn, SourceItem(
        "source", "ai", "Source", url, title, "", None,
    ))[0]


def test_should_translate_only_non_cjk_latin_titles():
    assert should_translate("AI agents improve coding") is True
    assert should_translate("GPT-6.1 release") is True
    assert should_translate("AI 智能体提升编程") is False
    assert should_translate("人工智能新闻") is False
    assert should_translate("12345") is False


def test_translate_pending_batches_english_titles_and_persists_results(conn):
    first = add(conn, "https://example.com/one", "First English title")
    add(conn, "https://example.com/chinese", "中文标题")
    second = add(conn, "https://example.com/two", "Second English title")
    translator = FakeTranslator()

    assert translate_pending(conn, translator=translator, limit=1) == 1
    assert translator.calls == [["First English title"]]
    assert conn.execute(
        "SELECT translated_title FROM articles WHERE id=(SELECT primary_article_id FROM events WHERE id=?)",
        (first,),
    ).fetchone()[0] == "译文：First English title"

    assert translate_pending(conn, translator=translator, limit=10) == 1
    assert translator.calls[-1] == ["Second English title"]
    assert conn.execute(
        "SELECT translated_title FROM articles WHERE id=(SELECT primary_article_id FROM events WHERE id=?)",
        (second,),
    ).fetchone()[0] == "译文：Second English title"


def test_translate_pending_does_not_overwrite_existing_translation(conn):
    event_id = add(conn, "https://example.com/existing", "Existing title")
    conn.execute(
        "UPDATE articles SET translated_title='人工译文' WHERE id=(SELECT primary_article_id FROM events WHERE id=?)",
        (event_id,),
    )
    conn.commit()
    translator = FakeTranslator()

    assert translate_pending(conn, translator=translator, limit=10) == 0
    assert translator.calls == []


def test_translate_pending_rolls_back_when_translator_fails(conn):
    add(conn, "https://example.com/fail", "Translation should fail")

    class BrokenTranslator:
        def translate_many(self, titles):
            raise RuntimeError("offline model failure")

    try:
        translate_pending(conn, translator=BrokenTranslator(), limit=10)
    except RuntimeError:
        pass
    else:
        raise AssertionError("translation error was not propagated")

    assert conn.execute(
        "SELECT translated_title FROM articles WHERE canonical_url='https://example.com/fail'"
    ).fetchone()[0] is None


def test_local_translator_appends_eos_and_removes_special_tokens():
    class SourceTokenizer:
        def encode(self, title, out_type):
            assert title == "English title"
            assert out_type is str
            return ["token"]

    class TargetTokenizer:
        def decode(self, tokens):
            assert tokens == ["中文", "标题"]
            return "中文标题"

    class Engine:
        def translate_batch(self, batches, **kwargs):
            assert batches == [["token", "</s>"]]
            assert kwargs["beam_size"] == 4
            result = type("Result", (), {"hypotheses": [["<pad>", "中文", "标题", "</s>"]]})
            return [result()]

    translator = LocalTranslator.__new__(LocalTranslator)
    translator._source = SourceTokenizer()
    translator._target = TargetTokenizer()
    translator._translator = Engine()
    assert translator.translate_many(["English title"]) == ["中文标题"]


def test_title_preparation_protects_product_names_and_expands_ai():
    prepared, terms = _prepare_title(
        "OpenAI partners with NVIDIA on AI agents, Says TrendForce"
    )
    assert "OpenAI" not in prepared
    assert "NVIDIA" not in prepared
    assert "TrendForce" not in prepared
    assert "artificial intelligence agents" in prepared
    assert "Says" not in prepared
    assert _restore_terms("ZXQ0ZX 与 ZXQ1ZX 合作开发人工智能智能体", terms) == (
        "OpenAI 与 NVIDIA 合作开发人工智能智能体"
    )
