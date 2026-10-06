"""Profile-owned Telegram inline buttons (DIGIVICE ``dv1:``, Tentamon ``tm1:``) run profile scripts."""

import os
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _make_adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    adapter._is_callback_user_authorized = MagicMock(return_value=True)
    return adapter


def _callback_update(data: str):
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=12345, first_name="Donghoon"),
        message=SimpleNamespace(
            chat_id=8614105155, message_id=77, message_thread_id=None, chat=SimpleNamespace(type="private")),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    return SimpleNamespace(callback_query=query), query


def _write_script(name: str, body: str) -> Path:
    scripts = Path(os.environ["HERMES_HOME"]) / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    path = scripts / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


_ECHO_ARGV = """
    import json, os, sys
    print(json.dumps({"argv": sys.argv[1:], "home": os.environ.get("HERMES_HOME")}))
    print("✅ 운동 기록")
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("data, handler", [
    ("dv1:nonce:c", "_handle_digivice_callback"),
    ("tm1:done", "_handle_tentamon_review_callback"),
])
async def test_profile_prefixes_route_to_their_handler(data, handler):
    adapter = _make_adapter()
    setattr(adapter, handler, AsyncMock())
    update, query = _callback_update(data)

    await adapter._handle_callback_query(update, SimpleNamespace())

    called = getattr(adapter, handler)
    called.assert_awaited_once()
    assert called.await_args.args[:2] == (query, data)
    assert called.await_args.args[2]["chat_id"] == 8614105155


@pytest.mark.asyncio
async def test_digivice_denies_unauthorized_user_without_running_script():
    adapter = _make_adapter()
    adapter._is_callback_user_authorized = MagicMock(return_value=False)
    marker = Path(os.environ["HERMES_HOME"]) / "ran"
    _write_script("digivice_callback.py", f"open({str(marker)!r}, 'w').close()\n")
    update, query = _callback_update("dv1:nonce:c")

    await adapter._handle_callback_query(update, SimpleNamespace())

    query.answer.assert_awaited_once_with(text="⛔ You are not authorized to record this check-in.")
    assert not marker.exists()


@pytest.mark.asyncio
async def test_digivice_runs_profile_script_reacts_big_and_closes_keyboard():
    adapter = _make_adapter()
    _write_script("digivice_callback.py", _ECHO_ARGV)
    update, query = _callback_update("dv1:nonce:c")

    await adapter._handle_callback_query(update, SimpleNamespace())

    query.answer.assert_awaited_once_with(text="✅ 운동 기록")
    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=8614105155, message_id=77, reaction="🔥", is_big=True)
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_digivice_passes_chat_message_and_user_to_script():
    adapter = _make_adapter()
    out = Path(os.environ["HERMES_HOME"]) / "argv.txt"
    _write_script("digivice_callback.py", f"""
        import os, sys
        open({str(out)!r}, "w").write("\\n".join(sys.argv[1:] + [os.environ["HERMES_HOME"]]))
    """)
    update, query = _callback_update("dv1:nonce:x")

    await adapter._handle_callback_query(update, SimpleNamespace())

    argv = out.read_text().splitlines()
    assert argv == [
        "dv1:nonce:x", "--chat-id", "8614105155", "--message-id", "77",
        "--user-id", "12345", "--user-name", "Donghoon", os.environ["HERMES_HOME"]]
    # Silent script -> default label; a skipped day gets a small (not big) thumbs-down.
    query.answer.assert_awaited_once_with(text="✅ DIGIVICE 기록")
    adapter._bot.set_message_reaction.assert_awaited_once_with(chat_id=8614105155, message_id=77, reaction="👎")


@pytest.mark.asyncio
async def test_digivice_script_failure_reports_stderr_and_keeps_keyboard():
    adapter = _make_adapter()
    _write_script("digivice_callback.py", """
        import sys
        print("already recorded", file=sys.stderr)
        sys.exit(2)
    """)
    update, query = _callback_update("dv1:nonce:c")

    await adapter._handle_callback_query(update, SimpleNamespace())

    query.answer.assert_awaited_once_with(text="❌ DIGIVICE 기록 실패: already recorded")
    adapter._bot.set_message_reaction.assert_not_awaited()
    query.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_profile_script_is_reported():
    adapter = _make_adapter()
    update, query = _callback_update("tm1:item-1")

    await adapter._handle_callback_query(update, SimpleNamespace())

    query.answer.assert_awaited_once_with(text="❌ Tentamon callback script missing.")


@pytest.mark.asyncio
@pytest.mark.parametrize("data, closes", [("tm1:item-1", False), ("tm1:done", True)])
async def test_tentamon_only_done_closes_keyboard(data, closes):
    adapter = _make_adapter()
    _write_script("tentamon_review_callback.py", _ECHO_ARGV)
    update, query = _callback_update(data)

    await adapter._handle_callback_query(update, SimpleNamespace())

    query.answer.assert_awaited_once_with(text="✅ 운동 기록")
    assert query.edit_message_reply_markup.await_count == (1 if closes else 0)


@pytest.mark.asyncio
async def test_set_reaction_big_animation_is_opt_in():
    adapter = _make_adapter()

    assert await adapter._set_reaction("123", "456", "🔥", is_big=True) is True
    assert await adapter._set_reaction("123", "456", "🔥") is True

    big, plain = adapter._bot.set_message_reaction.await_args_list
    assert big.kwargs == {"chat_id": 123, "message_id": 456, "reaction": "🔥", "is_big": True}
    assert plain.kwargs == {"chat_id": 123, "message_id": 456, "reaction": "🔥"}
