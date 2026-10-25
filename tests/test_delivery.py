"""Delivery: a short code is how a watch-sized screen hands over a real file."""

import zipfile

import pytest

from app.artifacts import ArtifactStore, ArtifactError
from app.db import Database


@pytest.fixture
def store(tmp_path):
    db = Database(str(tmp_path / "del.db"))
    db.init()
    return ArtifactStore(db, tmp_path / "artifacts")


def test_code_is_short_and_avoids_confusable_characters(store):
    item = store.create_html("会议一页", "<html>hi</html>")
    code = item["code"]
    assert len(code) == 4
    assert all(character in store.ALPHABET if hasattr(store, "ALPHABET") else True
               for character in code)
    assert not set(code) & set("0O1I"), "ambiguous characters must not appear"
    assert code == code.upper()


def test_two_artifacts_get_different_codes(store):
    first = store.create_html("a", "<html>a</html>")
    second = store.create_html("b", "<html>b</html>")
    assert first["code"] != second["code"]


def test_a_code_resolves_to_exactly_that_artifact(store):
    first = store.create_html("第一个", "<html>1</html>")
    second = store.create_html("第二个", "<html>2</html>")

    found = store.get_by_code(first["code"])
    assert found is not None
    assert found["id"] == first["id"]
    assert found["title"] == "第一个"

    assert store.get_by_code(second["code"])["id"] == second["id"]


def test_an_unknown_code_resolves_to_nothing(store):
    store.create_html("x", "<html>x</html>")
    assert store.get_by_code("ZZZZ") is None


def test_codes_survive_a_store_restart(tmp_path):
    db = Database(str(tmp_path / "del2.db"))
    db.init()
    root = tmp_path / "artifacts"
    created = ArtifactStore(db, root).create_html("持久", "<html>p</html>")
    again = ArtifactStore(db, root).get_by_code(created["code"])
    assert again is not None and again["id"] == created["id"]


def test_every_listed_artifact_carries_its_code(store):
    store.create_pptx("周三会议", [{"title": "背景", "bullets": ["a"]}])
    store.create_html("网页", "<html>w</html>")
    listed = store.list(10)
    assert len(listed) == 2
    for item in listed:
        assert item["code"] and len(item["code"]) == 4


def test_pptx_really_contains_a_presentation(store):
    item = store.create_pptx("周三会议", [{"title": "要点", "bullets": ["进度"]}])
    stored = store.root / item["path"].rsplit("/", 1)[-1]
    with zipfile.ZipFile(stored) as archive:
        assert "ppt/presentation.xml" in archive.namelist()


def test_empty_input_is_refused_with_a_reason(store):
    with pytest.raises(ArtifactError) as caught:
        store.create_pptx("标题", [])
    assert "内容" in caught.value.detail or "页" in caught.value.detail
