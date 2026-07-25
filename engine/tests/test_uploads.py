"""Приём файлов: безопасное имя, песочница каталога, untrusted-рамка (§5.5 + §8.2).

`file_name` приходит из Telegram — это ВВОД, а не факт. Клиент вправе прислать
`../../.env`, `/etc/cron.d/pwn` или `C:\\evil.dll`, и движок не имеет права записать
файл туда, куда просят. Плюс §8.2: содержимое файла — данные, не инструкции, и это
должно быть сказано агенту в самом промпте, а не подразумеваться.
"""

from __future__ import annotations

import pytest

from engine.core.uploads import (
    MAX_UPLOAD_BYTES,
    UploadStore,
    frame_attachment_prompt,
    safe_filename,
    too_large_message,
)

SID = "0b3f5c9a-1111-2222-3333-444455556666"


# ── safe_filename: имя из Telegram — недоверенный ввод ───────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("отчёт.pdf", "отчёт.pdf"),
        ("../../.env", "env"),
        ("../../../etc/passwd", "passwd"),
        ("/etc/shadow", "shadow"),
        ("C:\\Windows\\system32\\evil.dll", "evil.dll"),
        ("....//....//x.txt", "x.txt"),
        ("файл с пробелами.docx", "файл с пробелами.docx"),
    ],
)
def test_safe_filename_strips_paths(raw, expected):
    assert safe_filename(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "..", ".", "/", "///", "   ", "\x00", "\n\t"])
def test_safe_filename_falls_back_on_junk(raw):
    out = safe_filename(raw)
    assert out and "/" not in out and out not in (".", "..")


def test_safe_filename_removes_control_chars():
    assert safe_filename("отчёт\n\x00.pdf") == "отчёт.pdf"


def test_safe_filename_neutralizes_shell_and_fs_metachars():
    out = safe_filename('от"чёт|*?<>:.pdf')
    for ch in '"|*?<>:':
        assert ch not in out


def test_safe_filename_truncates_but_keeps_extension():
    out = safe_filename("а" * 500 + ".pdf")
    assert len(out) <= 128
    assert out.endswith(".pdf")


def test_safe_filename_never_returns_dotfile():
    # «.env» без расширения-обманки: скрытых файлов в uploads не заводим
    assert not safe_filename(".env").startswith(".")
    assert not safe_filename(".bashrc").startswith(".")


# ── UploadStore: каталог сессии ──────────────────────────────────────────────


def test_dir_for_session_matches_contract(tmp_path):
    # контракт срезов: <instance_dir>/state/uploads/<session_id>/
    store = UploadStore(tmp_path / "state" / "uploads")
    assert store.dir_for(SID) == tmp_path / "state" / "uploads" / SID
    assert store.dir_for(SID).is_dir()


def test_reserve_path_is_inside_session_dir(tmp_path):
    store = UploadStore(tmp_path / "uploads")
    path = store.reserve(SID, "../../escape.txt")
    assert path.parent == store.dir_for(SID)
    assert path.name == "escape.txt"


def test_reserve_dedupes_same_name(tmp_path):
    store = UploadStore(tmp_path / "uploads")
    first = store.reserve(SID, "отчёт.pdf")
    first.write_text("1", encoding="utf-8")
    second = store.reserve(SID, "отчёт.pdf")
    assert second != first
    assert second.suffix == ".pdf"  # расширение сохранено — агент поймёт формат


# ── ADV-14: reserve атомарен ──────────────────────────────────────────────────


def test_reserve_is_atomic_two_calls_never_collide(tmp_path):
    """Ровно та тихая потеря данных, которую docstring обещал предотвратить.

    Раньше reserve только ПРОВЕРЯЛ exists(), а файл создавался позже, в download. Два
    скриншота с одним `photo.jpg` получали один и тот же путь, и второй затирал первый
    вместе со ссылкой, уже отданной агенту.
    """
    store = UploadStore(tmp_path / "uploads")
    first = store.reserve(SID, "photo.jpg")
    second = store.reserve(SID, "photo.jpg")
    assert first != second


def test_reserve_creates_the_file_as_a_marker(tmp_path):
    """Резерв — это факт на диске (O_CREAT|O_EXCL), а не намерение."""
    store = UploadStore(tmp_path / "uploads")
    path = store.reserve(SID, "photo.jpg")
    assert path.is_file() and path.stat().st_size == 0


def test_reserve_survives_a_thousand_same_names(tmp_path):
    """Потолок дедупликации: 1000-й файл с тем же именем — понятная ошибка, не тишина."""
    store = UploadStore(tmp_path / "uploads")
    for _ in range(1000):
        store.reserve(SID, "photo.jpg")
    with pytest.raises(ValueError, match="слишком много"):
        store.reserve(SID, "photo.jpg")


def test_reserve_rejects_hostile_session_id(tmp_path):
    store = UploadStore(tmp_path / "uploads")
    for bad in ["../../../etc", "a/b", "..", "", "s" * 100, "sid;rm -rf"]:
        with pytest.raises(ValueError):
            store.reserve(bad, "f.txt")


def test_sessions_are_isolated(tmp_path):
    store = UploadStore(tmp_path / "uploads")
    a = store.reserve(SID, "f.txt")
    b = store.reserve("11111111-2222-3333-4444-555566667777", "f.txt")
    assert a.parent != b.parent


# ── лимит 20 МБ: честное сообщение, а не молчание ────────────────────────────


def test_limit_is_bot_api_20mb():
    assert MAX_UPLOAD_BYTES == 20 * 1024 * 1024


def test_too_large_message_names_the_limit():
    msg = too_large_message(size=50 * 1024 * 1024, limit=MAX_UPLOAD_BYTES)
    assert "20" in msg  # человек должен увидеть предел, а не «что-то пошло не так»
    assert "50" in msg  # и размер своего файла


# ── untrusted-рамка §8.2 ─────────────────────────────────────────────────────


def test_frame_marks_content_as_data_not_instructions():
    prompt = frame_attachment_prompt(
        path="/home/a/agents/x/state/uploads/s1/отчёт.pdf", kind="документ", user_text=""
    )
    assert "данные" in prompt.lower() and "не инструкции" in prompt.lower()
    assert "/home/a/agents/x/state/uploads/s1/отчёт.pdf" in prompt


def test_frame_includes_user_text_separately():
    prompt = frame_attachment_prompt(path="/x/f.pdf", kind="документ", user_text="сделай саммари")
    assert "сделай саммари" in prompt


def test_frame_warns_about_injection_attempts_inside_file():
    prompt = frame_attachment_prompt(path="/x/f.pdf", kind="документ", user_text="")
    low = prompt.lower()
    # рамка прямо предупреждает: «сделай/отправь» внутри файла — не команда движку
    assert "игнорируй" in low or "команд" in low


def test_frame_without_user_text_asks_nothing_extra():
    prompt = frame_attachment_prompt(path="/x/f.pdf", kind="фото", user_text="")
    assert "фото" in prompt
