"""Unit tests for the pure helpers in provenance_log."""

from __future__ import annotations

import hashlib

import provenance_log as pl


def test_sha256_text_matches_hashlib():
    assert pl.sha256_text("abc") == hashlib.sha256(b"abc").hexdigest()


def test_sha256_text_replaces_unencodable():
    # lone surrogate cannot be utf-8 encoded; "replace" must not raise
    assert len(pl.sha256_text("\ud800")) == 64


def test_extract_content_non_dict():
    assert pl.extract_content(None) == ("", [])
    assert pl.extract_content("str") == ("", [])


def test_extract_content_plain_string():
    assert pl.extract_content({"content": "hello"}) == ("hello", [])


def test_extract_content_blocks():
    msg = {"content": [
        {"type": "thinking", "thinking": "t"},
        {"type": "text", "text": "x"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        {"type": "tool_result", "content": "r1"},
        {"type": "tool_result", "content": [{"type": "text", "text": "r2"}, "ignored"]},
        {"type": "image"},
        "not a dict",
    ]}
    text, tools = pl.extract_content(msg)
    assert tools == ["Bash"]
    assert text == 't\nx\n{"command": "ls"}\nr1\nr2'


def test_extract_content_tool_use_input_truncated():
    msg = {"content": [{"type": "tool_use", "name": "W", "input": {"big": "x" * 5000}}]}
    text, tools = pl.extract_content(msg)
    assert tools == ["W"]
    assert len(text) == 2000


def test_extract_content_missing_tool_name():
    text, tools = pl.extract_content({"content": [{"type": "tool_use"}]})
    assert tools == ["?"]
    assert text == '""'


def test_extract_content_drops_empty_blocks():
    msg = {"content": [{"type": "text", "text": ""}, {"type": "text", "text": "a"}]}
    assert pl.extract_content(msg) == ("a", [])


def test_now_iso_is_utc_seconds():
    s = pl.now_iso()
    assert s.endswith("+00:00")
    assert "." not in s
