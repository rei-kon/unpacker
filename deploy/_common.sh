#!/usr/bin/env bash
# deploy/_common.sh — общий резолвинг для deploy.sh и agentctl.sh.
# Единый источник правды: deploy кладёт инстанс в $AGENTS_BASE/<name>, agentctl обязан
# смотреть в ТОТ ЖЕ каталог. Раньше блок дублировался в обоих скриптах — любая правка
# приоритета молча разводила их (agentctl рапортовал бы NOT DEPLOYED про живого бота).
# Источится обоими: `. "$HERE/_common.sh"`.

# RUN_USER: явный TG_RUN_USER → SUDO_USER (человек за `sudo`, не root) → текущий юзер.
# root как run-user отсекается вызывающим скриптом отдельно (bypass+root запрещён CLI).
resolve_run_identity() {
  if [ -n "${TG_RUN_USER:-}" ]; then
    RUN_USER="$TG_RUN_USER"
  elif [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    RUN_USER="$SUDO_USER"
  else
    RUN_USER="$(id -un)"
  fi
  RUN_HOME=""
  if command -v getent >/dev/null 2>&1; then
    RUN_HOME="$(getent passwd "$RUN_USER" 2>/dev/null | cut -d: -f6)"
  fi
  if [ -z "$RUN_HOME" ]; then
    if [ "$RUN_USER" = "$(id -un)" ]; then RUN_HOME="$HOME"; else RUN_HOME="/home/$RUN_USER"; fi
  fi
  # используются в sourcing-скриптах (deploy.sh/agentctl.sh) — SC2034 здесь ложный
  # shellcheck disable=SC2034
  AGENTS_BASE="${TG_AGENTS_BASE:-$RUN_HOME/agents}"
  # shellcheck disable=SC2034
  BRAINS_BASE="${TG_BRAINS_BASE:-$RUN_HOME/brains}"
}

# uv резолвим В КОНТЕКСТЕ RUN_USER, не вызывающего. КРИТИЧНО: при `sudo deploy.sh` от root
# `command -v uv` дал бы /root/.local/bin/uv — а юнит бежит User=canary и не может читать /root
# (mode 700) → ExecStart падает 203/EXEC, бот-зомби. Берём uv из окружения RUN_USER.
resolve_uv_bin() {
  if [ -n "${TG_UV_BIN:-}" ]; then
    UV_BIN="$TG_UV_BIN"
  elif [ "$RUN_USER" = "$(id -un)" ]; then
    UV_BIN="$(command -v uv 2>/dev/null || echo "$RUN_HOME/.local/bin/uv")"
  else
    UV_BIN="$(sudo -u "$RUN_USER" -H bash -lc 'command -v uv' 2>/dev/null || true)"
    [ -n "$UV_BIN" ] || UV_BIN="$RUN_HOME/.local/bin/uv"
  fi
}

# slug: ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ — алнум по краям, не пусто, не только дефисы
# (agent-tg@-.service — валидный, но бессмысленный юнит; отсекаем).
valid_slug() { printf '%s' "$1" | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$'; }

# safe_arg: 0/true, если в строке НЕТ шелл-метасимволов и переводов строк. Для значений,
# которые втекают в bash -c-строки (пути/URL мозга) — легит-путь/URL их не содержит.
# BRAND/модель НЕ обязаны быть safe_arg: они уходят в seed через argv (asuser_argv), где
# шелл их не парсит — «Никита's бот» валиден.
safe_arg() {
  case "$1" in
    *\'*|*\"*|*\`*|*\$*|*\;*|*\|*|*\&*|*'<'*|*'>'*|*'('*|*')'*|*'{'*|*'}'*|*'\'*|"") return 1 ;;
    *$'\n'*) return 1 ;;
  esac
  return 0
}
