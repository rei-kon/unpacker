"""ButtonRegistry — инстансные кнопки в рантайме (§9).

Владелец правит `buttons.yaml` живого инстанса руками, и рестарт бота ради новой кнопки —
плохой UX «наращивания штук» (§9). Поэтому реестр перечитывает файл по mtime. Битый yaml
после правки НЕ должен обезоружить бота: держим последний рабочий набор и живём дальше.
"""

from __future__ import annotations

from engine.core.brain import ButtonSpec
from engine.core.buttons import ButtonRegistry

ONE = 'buttons:\n  - label: "Создать КП"\n    prompt: "собери КП"\n'
TWO = ONE + '  - label: "Отчёт"\n    prompt: "сделай отчёт"\n'


def _reg(tmp_path, text: str | None = ONE, **kw) -> ButtonRegistry:
    path = tmp_path / "buttons.yaml"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    return ButtonRegistry(path, **kw)


def test_reads_buttons_from_file(tmp_path):
    assert [b.label for b in _reg(tmp_path).get()] == ["Создать КП"]


def test_missing_file_is_empty(tmp_path):
    assert _reg(tmp_path, text=None).get() == []


def test_registry_has_no_enabled_flag(tmp_path):
    """K10: одна конвенция косметики — «выключено» = реестра нет вовсе.

    Флаг внутри объекта был третьим механизмом на однотипный флаг (у intake — None,
    у send_file — bool). Проверку «выключено убирает ряд целиком» держит test_runtime
    (`bot._buttons is None`) и test_bot_attachments (пустая клавиатура).
    """
    assert not hasattr(_reg(tmp_path), "enabled")


def test_reloads_after_edit(tmp_path):
    reg = _reg(tmp_path)
    assert len(reg.get()) == 1
    (tmp_path / "buttons.yaml").write_text(TWO, encoding="utf-8")
    assert [b.label for b in reg.get()] == ["Создать КП", "Отчёт"]


def test_broken_edit_keeps_last_good_set(tmp_path):
    reg = _reg(tmp_path)
    good = reg.get()
    (tmp_path / "buttons.yaml").write_text("buttons: [битый\n", encoding="utf-8")
    assert reg.get() == good, "опечатка владельца не должна снимать кнопки с живого бота"


def test_broken_from_the_start_is_empty_not_crash(tmp_path):
    assert _reg(tmp_path, text="buttons: [битый\n").get() == []


def test_recovers_after_fixing_file(tmp_path):
    reg = _reg(tmp_path, text="buttons: [битый\n")
    assert reg.get() == []
    (tmp_path / "buttons.yaml").write_text(ONE, encoding="utf-8")
    assert [b.label for b in reg.get()] == ["Создать КП"]


def test_deleted_file_keeps_last_good_set(tmp_path):
    reg = _reg(tmp_path)
    good = reg.get()
    (tmp_path / "buttons.yaml").unlink()
    assert reg.get() == good


def test_resolve_by_index(tmp_path):
    reg = _reg(tmp_path, text=TWO)
    assert reg.resolve(1) == ButtonSpec(label="Отчёт", prompt="сделай отчёт")


def test_resolve_out_of_range_is_none(tmp_path):
    # кнопка на старом сообщении, а файл уже переписан → не падаем, отвечаем «устарела»
    reg = _reg(tmp_path)
    assert reg.resolve(5) is None
    assert reg.resolve(-1) is None
