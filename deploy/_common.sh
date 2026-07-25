#!/usr/bin/env bash
# deploy/_common.sh — общий резолвинг для deploy.sh, agentctl.sh, install-sudoers.sh, update.sh.
# Единый источник правды: deploy кладёт инстанс в $AGENTS_BASE/<name>, agentctl обязан
# смотреть в ТОТ ЖЕ каталог. Раньше блок дублировался в обоих скриптах — любая правка
# приоритета молча разводила их (agentctl рапортовал бы NOT DEPLOYED про живого бота).
# Источится всеми: `. "$HERE/_common.sh"`.

# ── машинный конфиг /etc/unpacker/engine.conf (Р2) ───────────────────────────
# Зачем: раньше личность и базы выводились из окружения ВЫЗЫВАЮЩЕГО (TG_* → SUDO_USER →
# id -un). Любая команда, запущенная «не так, как это делал install.sh» (cron, root руками,
# sudo от агента с env_reset), читала ДРУГУЮ вселенную путей и тихо рапортовала успех:
# первый мозг лёг в /srv/brains, следующие — в /home/unpacker/brains (ADV-09), а
# `agentctl doctor` отвечал NOT DEPLOYED про живого бота (C14/ADV-05).
#
# Файл пишет install.sh (root:root, 0644). Мы его ПАРСИМ ПОСТРОЧНО, а НЕ сорсим: привычка
# `source` любого конфига — ровно тот класс дыр, что SEC-1/SEC-2 (подложенный файл = код от
# root). Берём только известные ключи, значения без подстановок и без шелл-метасимволов.
# Путь к карте берём в порядке: уже заданный вызывающим (install.sh сорсит этот файл, чтобы
# резолвить пути ТЕМ ЖЕ кодом, и свой ENGINE_CONF у него уже вычислен) → явный
# UNPACKER_ENGINE_CONF → каталог из UNPACKER_ETC → системный дефолт. Без первых двух звеньев
# мы затирали значение вызывающего своим дефолтом: install.sh писал карту в один каталог, а
# читал бы из другого — тот самый класс «две вселенных путей», ради которого карта и введена.
ENGINE_CONF="${ENGINE_CONF:-${UNPACKER_ENGINE_CONF:-${UNPACKER_ETC:-/etc/unpacker}/engine.conf}}"
CONF_RUN_USER="" CONF_RUNTIME="" CONF_AGENTS_BASE="" CONF_BRAINS_BASE="" CONF_UV_BIN=""
CONF_VENV=""
_ENGINE_CONF_LOADED="false"

load_engine_conf() {
  [ "$_ENGINE_CONF_LOADED" = "false" ] || return 0
  _ENGINE_CONF_LOADED="true"
  [ -f "$ENGINE_CONF" ] && [ -r "$ENGINE_CONF" ] || return 0
  local line key val
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; *=*) ;; *) continue ;; esac
    key="${line%%=*}"; val="${line#*=}"
    key="${key//[[:space:]]/}"
    # trim пробелов по краям значения (кавычек не разбираем — их и не пишем)
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"
    # значение втекает в пути, sed-подстановки и bash -c-строки → метасимволы отвергаем
    if [ -n "$val" ] && ! safe_arg "$val"; then
      echo "warn: $ENGINE_CONF: значение ключа $key отброшено (недопустимые символы)" >&2
      continue
    fi
    case "$key" in
      TG_RUN_USER)    CONF_RUN_USER="$val" ;;
      TG_RUNTIME)     CONF_RUNTIME="$val" ;;
      TG_AGENTS_BASE) CONF_AGENTS_BASE="$val" ;;
      TG_BRAINS_BASE) CONF_BRAINS_BASE="$val" ;;
      TG_UV_BIN)      CONF_UV_BIN="$val" ;;
      # venv движка вынесен ИЗ дерева кода (Р1): код root-owned, venv принадлежит run-user.
      # Ключ необязательный — если его нет, uv берёт свой дефолт ($RUNTIME/.venv).
      TG_VENV)        CONF_VENV="$val" ;;
      *) : ;;  # чужие ключи игнорируем молча: файл общий, а не наш private-namespace
    esac
  done < "$ENGINE_CONF"
}

# RUN_USER: явный TG_RUN_USER → engine.conf → SUDO_USER (человек за `sudo`, не root) →
# текущий юзер. root как run-user отсекается вызывающим скриптом (bypass+root запрещён CLI).
resolve_run_identity() {
  load_engine_conf
  if [ -n "${TG_RUN_USER:-}" ]; then
    RUN_USER="$TG_RUN_USER"
  elif [ -n "$CONF_RUN_USER" ]; then
    RUN_USER="$CONF_RUN_USER"
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
  AGENTS_BASE="${TG_AGENTS_BASE:-${CONF_AGENTS_BASE:-$RUN_HOME/agents}}"
  # shellcheck disable=SC2034
  BRAINS_BASE="${TG_BRAINS_BASE:-${CONF_BRAINS_BASE:-$RUN_HOME/brains}}"
  # shellcheck disable=SC2034
  RUNTIME="${TG_RUNTIME:-${CONF_RUNTIME:-/opt/unpacker}}"
  # venv движка: env (родной для uv) → engine.conf → пусто (значит дефолт uv).
  # shellcheck disable=SC2034
  VENV_PATH="${UV_PROJECT_ENVIRONMENT:-$CONF_VENV}"
}

# uv резолвим ФИКСИРОВАННЫМИ путями, а НЕ через login-шелл run-user'а (SEC-8, Р3).
# Раньше было `sudo -u $RUN_USER bash -lc 'command -v uv'`: значение из окружения агента
# подставлялось sed'ом в root-устанавливаемый юнит — неверная граница доверия.
# Теперь uv ставится системно (UV_INSTALL_DIR=/usr/local/bin от root), путь приходит
# из engine.conf, а кандидаты перебираются по абсолютным путям.
resolve_uv_bin() {
  load_engine_conf
  UV_BIN="${TG_UV_BIN:-$CONF_UV_BIN}"
  if [ -z "$UV_BIN" ]; then
    local c
    for c in /usr/local/bin/uv /usr/bin/uv "$RUN_HOME/.local/bin/uv"; do
      [ -x "$c" ] && { UV_BIN="$c"; break; }
    done
  fi
  # dev-машина: run-user — это я сам, uv стоит в моём PATH (brew и т.п.). Чужой login-шелл
  # здесь не поднимаем — только собственный, уже доверенный.
  if [ -z "$UV_BIN" ] && [ "$RUN_USER" = "$(id -un)" ]; then
    UV_BIN="$(command -v uv 2>/dev/null || true)"
  fi
  # Пустой путь дал бы невнятное «: command not found» — подставляем каноничный (Р3),
  # чтобы preflight сказал «uv не запускается по пути /usr/local/bin/uv».
  [ -n "$UV_BIN" ] || UV_BIN="/usr/local/bin/uv"
}

# Валидация пути до uv ДО того, как он попадёт в sed-подстановку юнита (SEC-8).
# Печатает причину в stderr, возвращает 1 — вызывающий решает, это exit 2 или предупреждение.
validate_uv_bin() {
  case "${UV_BIN:-}" in
    /*) ;;
    *) echo "путь до uv '$UV_BIN' должен быть абсолютным (TG_UV_BIN / engine.conf)" >&2; return 1 ;;
  esac
  safe_arg "$UV_BIN" || {
    echo "путь до uv '$UV_BIN': недопустимые символы — он подставляется в юнит" >&2; return 1; }
  case "$UV_BIN" in
    *'*'*|*..*) echo "путь до uv '$UV_BIN': ни wildcard, ни .. в пути" >&2; return 1 ;;
  esac
  return 0
}

# ── PATH юнита и claude CLI: ОДИН источник (Р4, ADV-16) ─────────────────────
# PATH, с которым бежит юнит, — он же место, где ДОЛЖЕН лежать claude. Проверять CLI в
# логин-шелле run-user'а бессмысленно: у юнита другое окружение (systemd-дефолт PATH не
# включает ~/.local/bin и npm-prefix). Именно так рождался ADV-16: ученик ставил claude от
# root, проверка в оболочке root была зелёной, а бот молчал на каждое сообщение.
unit_path() {
  printf '%s/.local/bin:%s/.npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
    "$RUN_HOME" "$RUN_HOME"
}

# CLAUDE_BIN: явный TG_CLAUDE_BIN → первый исполняемый claude в PATH юнита → пусто (=не найден).
resolve_claude_bin() {
  CLAUDE_BIN="${TG_CLAUDE_BIN:-}"
  [ -z "$CLAUDE_BIN" ] || { [ -x "$CLAUDE_BIN" ] || CLAUDE_BIN=""; return 0; }
  local saved_ifs="$IFS" d
  IFS=:
  for d in $(unit_path); do
    if [ -x "$d/claude" ]; then CLAUDE_BIN="$d/claude"; break; fi
  done
  IFS="$saved_ifs"
}

# systemctl: чем зовём службы. Переопределяемо (H6) — тесты не должны трогать живой systemd.
SYSTEMCTL="${TG_SYSTEMCTL:-systemctl}"
have_systemctl() { command -v "$SYSTEMCTL" >/dev/null 2>&1; }

# ── имя systemd-юнита: ОДИН источник (M-17) ─────────────────────────────────
# Раньше литерал 'agent-tg@' был зашит в шести файлах, и _common.sh его не отдавал:
# переименование поверхности молча разводило deploy.sh, agentctl.sh, update.sh и sudoers.
UNIT_PREFIX="agent-tg@"
UNIT_SUFFIX=".service"
# unit_name <slug> → agent-tg@<slug>; unit_template → agent-tg@.service (файл шаблона).
unit_name()     { printf '%s%s' "$UNIT_PREFIX" "$1"; }
unit_template() { printf '%s%s' "$UNIT_PREFIX" "$UNIT_SUFFIX"; }

# slug: ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ — алнум по краям, не пусто, не только дефисы
# (agent-tg@-.service — валидный, но бессмысленный юнит; отсекаем).
valid_slug() { printf '%s' "$1" | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$'; }

# safe_arg: 0/true, если в строке НЕТ шелл-метасимволов и переводов строк. Для значений,
# которые втекают в bash -c-строки (пути/URL мозга) — легит-путь/URL их не содержит.
# BRAND/модель НЕ обязаны быть safe_arg: они уходят в seed через argv (asuser_argv), где
# шелл их не парсит — «Никита's бот» валиден.
safe_arg() {
  # SC1003: *'\'* — это литеральный обратный слэш в шаблоне case, а не попытка экранировать
  # кавычку. Глушим осознанно, чтобы shellcheck на всей зоне был чистым.
  # shellcheck disable=SC1003
  case "$1" in
    *\'*|*\"*|*\`*|*\$*|*\;*|*\|*|*\&*|*'<'*|*'>'*|*'('*|*')'*|*'{'*|*'}'*|*'\'*|"") return 1 ;;
    *$'\n'*) return 1 ;;
  esac
  return 0
}
