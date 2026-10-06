"""A one-image Telegram cron report is delivered as ONE photo+caption bubble (profile HTML reports)."""

from concurrent.futures import Future
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cron.scheduler import _deliver_result
from gateway.config import Platform
from gateway.platforms.base import SendResult


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    root = tmp_path / "media-cache"
    root.mkdir()
    monkeypatch.setattr("gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS", (root,))
    return root


def _media(root, name):
    path = root / name
    path.write_bytes(b"media")
    return path.resolve()


def _fake_run_coro(coro, _loop):
    import asyncio
    future = Future()
    try:
        future.set_result(asyncio.run(coro))
    except BaseException as exc:  # noqa: BLE001
        future.set_exception(exc)
    return future


def _deliver(job, content, adapter, **extra_patches):
    cfg = MagicMock()
    cfg.platforms = {Platform.TELEGRAM: MagicMock(enabled=True)}
    loop = MagicMock()
    loop.is_running.return_value = True
    with patch("gateway.config.load_gateway_config", return_value=cfg), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=_fake_run_coro):
        return _deliver_result(job, content, adapters={Platform.TELEGRAM: adapter}, loop=loop)


def _adapter():
    adapter = AsyncMock()
    adapter.send.return_value = SendResult(success=True, message_id="1")
    adapter.send_image_file.return_value = SendResult(success=True, message_id="2")
    return adapter


_ORIGIN_JOB = {"deliver": "origin", "origin": {"platform": "telegram", "chat_id": "123"}}


def test_single_image_becomes_html_captioned_photo_in_topic(media_root):
    image = _media(media_root, "report.png")
    adapter = _adapter()
    # 800 tags -> 2400 raw chars but only 800 visible: fits the post-entity caption cap.
    content = "<b>x</b>" * 800

    result = _deliver({"id": "captioned-report", "deliver": "telegram:-100123:77"},
                      f"{content}\nMEDIA:{image}", adapter)

    assert result is None
    adapter.send.assert_not_called()
    adapter.send_image_file.assert_called_once()
    call = adapter.send_image_file.call_args.kwargs
    assert call["image_path"] == str(image)
    assert call["caption"] == content
    assert str(call["metadata"]["thread_id"]) == "77"
    assert call["metadata"]["job_id"] == "captioned-report"
    assert call["metadata"]["notify"] is True  # the photo IS the final notification


def test_text_over_caption_cap_keeps_separate_text_and_image(media_root):
    image = _media(media_root, "report.png")
    adapter = _adapter()

    _deliver({"id": "long-report", **_ORIGIN_JOB}, f"{'x' * 1025}\nMEDIA:{image}", adapter)

    adapter.send.assert_called_once()
    assert adapter.send.call_args.args[1] == "x" * 1025
    adapter.send_image_file.assert_called_once()
    assert "caption" not in adapter.send_image_file.call_args.kwargs


def test_multiple_images_are_never_collapsed_into_one_caption(media_root):
    first, second = _media(media_root, "first.png"), _media(media_root, "second.jpg")
    adapter = _adapter()

    _deliver({"id": "multi-report", **_ORIGIN_JOB}, f"Summary\nMEDIA:{first}\nMEDIA:{second}", adapter)

    adapter.send.assert_called_once()
    assert adapter.send_image_file.call_count == 2
    assert all("caption" not in c.kwargs for c in adapter.send_image_file.call_args_list)


def test_non_image_attachment_keeps_text_then_document(media_root):
    doc = _media(media_root, "report.pdf")
    adapter = _adapter()

    _deliver({"id": "pdf-report", **_ORIGIN_JOB}, f"<b>판단</b>\nMEDIA:{doc}", adapter)

    adapter.send.assert_called_once()
    adapter.send_image_file.assert_not_called()
    adapter.send_document.assert_called_once()


def test_image_refused_by_media_policy_is_not_captioned(media_root, tmp_path, monkeypatch):
    denied = tmp_path / "denied"
    denied.mkdir()
    secret = denied / "report.png"
    secret.write_bytes(b"media")
    monkeypatch.setattr("gateway.platforms.base._media_delivery_denied_paths", lambda: [denied])
    adapter = _adapter()

    _deliver({"id": "policy-report", **_ORIGIN_JOB}, f"<b>판단</b>\nMEDIA:{secret.resolve()}", adapter)

    adapter.send.assert_called_once()
    adapter.send_image_file.assert_not_called()


def test_refused_captioned_send_falls_back_to_standalone_with_media(media_root):
    image = _media(media_root, "report.png")
    adapter = _adapter()
    adapter.send_image_file.return_value = SendResult(success=False, error="photo rejected")

    with patch("tools.send_message_tool._send_to_platform",
               new=AsyncMock(return_value={"success": True})) as standalone_send:
        result = _deliver({"id": "caption-fallback", **_ORIGIN_JOB}, f"<b>판단</b>\nMEDIA:{image}", adapter)

    assert result is None
    adapter.send_image_file.assert_called_once()
    standalone_send.assert_awaited_once()
    assert standalone_send.await_args.args[3] == "<b>판단</b>"
    assert standalone_send.await_args.kwargs["media_files"] == [(str(image), False)]
