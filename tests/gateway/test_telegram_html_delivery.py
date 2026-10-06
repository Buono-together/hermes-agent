"""Explicit Telegram HTML (profile report formatters) is delivered as HTML, not MarkdownV2-escaped."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from gateway.config import PlatformConfig  # noqa: E402
from gateway.platforms.base import SendResult  # noqa: E402
from plugins.platforms.telegram.adapter import (  # noqa: E402
    ParseMode, TelegramAdapter, _contains_supported_telegram_html, _strip_telegram_html, _telegram_caption_fits)

HAEO_REPORT = (
    "🪿 장중 · 수급 점검\n\n<b>09:30 → 지금</b>\n"
    "<pre>코스피 −1.34% → −1.55%\n외인+기관 −5,436억</pre>\n\n<b>판단</b>\n새 매매 신호 없음.")


@pytest.fixture
def adapter():
    a = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    a._bot = MagicMock()
    a._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    a._rich_messages_enabled = False
    return a


@pytest.mark.parametrize("text, expected", [
    ("<b>판단</b>", True),
    ('<a href="https://x.y">link</a>', True),
    ("x < y and y > z", False),
    ("`<b>` inside code", False),
    ("```\n<pre>fenced</pre>\n```", False),
    ("<div>not telegram</div>", False),
])
def test_only_telegram_supported_tags_outside_code_count_as_html(text, expected):
    assert _contains_supported_telegram_html(text) is expected


def test_strip_keeps_decoded_visible_text():
    assert _strip_telegram_html("<b>A &amp; B</b>\n<pre>1 &lt; 2</pre>") == "A & B\n1 < 2"


def test_caption_fit_is_measured_after_entities():
    assert _telegram_caption_fits("<b>x</b>" * 1024)
    assert not _telegram_caption_fits("<b>x</b>" * 1025)
    assert not _telegram_caption_fits("😀" * 513)  # supplementary plane = 2 UTF-16 units


@pytest.mark.asyncio
async def test_html_report_is_sent_verbatim_as_html(adapter):
    result = await adapter.send("12345", HAEO_REPORT, metadata={"notify": True})

    assert result.success is True
    call = adapter._bot.send_message.await_args.kwargs
    assert call["text"] == HAEO_REPORT
    assert call["parse_mode"] == ParseMode.HTML


@pytest.mark.asyncio
async def test_html_parse_failure_falls_back_to_readable_plain_text(adapter):
    adapter._bot.send_message = AsyncMock(side_effect=[
        Exception("Can't parse entities: unclosed start tag"), SimpleNamespace(message_id=2)])

    result = await adapter.send("12345", "<b>판단</b>\n<pre>기다린다</pre>", metadata={"notify": True})

    assert result.success is True
    first, second = adapter._bot.send_message.await_args_list
    assert first.kwargs["parse_mode"] == ParseMode.HTML
    assert second.kwargs["parse_mode"] is None
    assert second.kwargs["text"] == "판단\n기다린다"


@pytest.mark.asyncio
async def test_oversized_html_is_split_as_plain_text_never_mid_tag(adapter):
    adapter._bot.send_message = AsyncMock(side_effect=[SimpleNamespace(message_id=i) for i in range(1, 5)])
    content = "<b>" + "가" * 5000 + "</b>"

    result = await adapter.send("12345", content, metadata={"notify": True})

    assert result.success is True
    calls = adapter._bot.send_message.await_args_list
    assert len(calls) >= 2
    for call in calls:
        assert call.kwargs["parse_mode"] is None
        assert "<b>" not in call.kwargs["text"] and "</b>" not in call.kwargs["text"]


@pytest.mark.asyncio
async def test_ordinary_markdown_still_uses_markdownv2(adapter):
    result = await adapter.send("12345", "**판단**: wait!", metadata={"notify": True})

    assert result.success is True
    call = adapter._bot.send_message.await_args.kwargs
    assert call["parse_mode"] == ParseMode.MARKDOWN_V2
    assert call["text"] == "*판단*: wait\\!"


@pytest.mark.asyncio
async def test_resumed_plain_tail_keeps_its_format(adapter):
    partial = SendResult(success=False, error="flood", raw_response={
        "undelivered_chunks": ("<plain> tail (2/2)",), "delivered_message_ids": ("1",),
        "undelivered_chunk_format": "plain"})
    adapter._send_flood_cooldown_remaining = MagicMock(return_value=None)

    result = await adapter._resume_partial_send("12345", partial, reply_to=None, metadata=None)

    assert result.success is True
    call = adapter._bot.send_message.await_args.kwargs
    assert call["parse_mode"] is None
    assert call["text"] == "<plain> tail (2/2)"


# --- photo captions ---------------------------------------------------------------------------

@pytest.fixture
def image(tmp_path):
    img = tmp_path / "report.png"
    img.write_bytes(b"\x89PNG" + b"\x00" * 50)
    return str(img)


@pytest.fixture
def photo_adapter(adapter):
    adapter._compress_image_to_jpeg = MagicMock(return_value=None)
    adapter._bot.send_photo = AsyncMock(return_value=MagicMock(message_id=44))
    return adapter


@pytest.mark.asyncio
async def test_html_caption_preserved_and_counted_after_entities(photo_adapter, image):
    caption = "<b>x</b>" * 800

    result = await photo_adapter.send_image_file(chat_id="12345", image_path=image, caption=caption)

    assert result.success
    call = photo_adapter._bot.send_photo.call_args.kwargs
    assert call["caption"] == caption
    assert call["parse_mode"] == ParseMode.HTML


@pytest.mark.asyncio
async def test_markdown_caption_rendered_as_markdownv2(photo_adapter, image):
    await photo_adapter.send_image_file(chat_id="12345", image_path=image, caption="**판단**: wait!")

    call = photo_adapter._bot.send_photo.call_args.kwargs
    assert call["parse_mode"] == ParseMode.MARKDOWN_V2
    assert call["caption"] == "*판단*: wait\\!"


@pytest.mark.asyncio
async def test_caption_parse_failure_retries_same_photo_as_plain_text(photo_adapter, image):
    photo_adapter._bot.send_photo = AsyncMock(side_effect=[
        Exception("Can't parse entities: unclosed start tag"), MagicMock(message_id=45)])
    photo_adapter.send_document = AsyncMock()

    result = await photo_adapter.send_image_file(
        chat_id="12345", image_path=image, caption="<b>판단</b>\n<pre>대기</pre>")

    assert result.success
    first, second = photo_adapter._bot.send_photo.await_args_list
    assert first.kwargs["parse_mode"] == ParseMode.HTML
    assert second.kwargs["caption"] == "판단\n대기"
    assert "parse_mode" not in second.kwargs
    photo_adapter.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversized_caption_truncates_on_utf16_boundary_without_parse_mode(photo_adapter, image):
    await photo_adapter.send_image_file(chat_id="12345", image_path=image, caption="😀" * 600)

    call = photo_adapter._bot.send_photo.call_args.kwargs
    assert call["caption"] == "😀" * 512
    assert "parse_mode" not in call


@pytest.mark.asyncio
async def test_document_fallback_strips_html_caption(photo_adapter, image):
    photo_adapter._bot.send_photo = AsyncMock(side_effect=Exception("PHOTO_INVALID_DIMENSIONS"))
    photo_adapter.send_document = AsyncMock(return_value=SendResult(success=True, message_id="47"))

    result = await photo_adapter.send_image_file(
        chat_id="12345", image_path=image, caption="<b>판단</b>\n<pre>대기</pre>")

    assert result.success
    assert photo_adapter.send_document.await_args.kwargs["caption"] == "판단\n대기"
