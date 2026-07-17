"""Allow-list гейт — бот отвечает ТОЛЬКО разрешённым Telegram user ID и только в личке."""

from engine.core.security import AllowList, is_private_chat


def test_allowed_user_passes():
    al = AllowList([111, 222])
    assert al.is_allowed(111) is True
    assert al.is_allowed(222) is True


def test_unknown_user_blocked():
    al = AllowList([111])
    assert al.is_allowed(999) is False


def test_none_user_blocked():
    al = AllowList([111])
    assert al.is_allowed(None) is False


def test_empty_allowlist_blocks_everyone():
    # пустой список = запрет всем (fail-closed), а не «пускать всех»
    al = AllowList([])
    assert al.is_allowed(111) is False
    assert al.is_allowed(None) is False


def test_string_ids_not_confused_with_int():
    # user_id из Telegram — int; строка "111" не должна проходить как 111
    al = AllowList([111])
    assert al.is_allowed("111") is False


def test_bool_not_confused_with_int():
    # True == 1 в Python; bool не должен пролезть как id
    al = AllowList([1])
    assert al.is_allowed(True) is False


def test_is_private_chat():
    assert is_private_chat("private") is True
    for t in ("group", "supergroup", "channel", None):
        assert is_private_chat(t) is False


def test_should_handle_requires_private_and_allowed():
    al = AllowList([111])
    assert al.should_handle(111, "private") is True
    # разрешённый юзер, но НЕ личка → запрет (вывод офиса не должен утечь в группу)
    assert al.should_handle(111, "group") is False
    assert al.should_handle(111, "supergroup") is False
    # личка, но чужой юзер → запрет
    assert al.should_handle(999, "private") is False
