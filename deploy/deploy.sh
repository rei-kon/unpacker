#!/usr/bin/env bash
# deploy/deploy.sh — развернуть ОДИН Telegram-агент движка «Распаковщик» на этой машине.
# ОДИН идемпотентный скрипт: повторный запуск синхронизирует, не ломает (§6.1, §10 конституции).
#
# Движок НЕ копируется под каждого агента — все агенты бегут один код через
# `uv run --project <runtime> python -m engine`. Пер-агентный конфиг (.env + мозг + state/)
# живёт в отдельном инстанс-каталоге $AGENTS_BASE/<name>.
#
# Использование:
#   deploy.sh --surface tg --name <slug> --token <BOT_TOKEN> --users <id,id> \
#             --brain <git-url|local-path> [--project-slug <slug>] [--brand "<имя>"] \
#             [--cc-token <OAUTH>] [--default-model <m>] [--role agent|unpacker] \
#             [--audience internal|client] [--auth-mode subscription|api] [--api-key <KEY>] \
#             [--limit-requests-per-day N] [--limit-tokens-per-day N] \
#             [--dry-run] [--skip-preflight]
#
# КОНТУР AUTH — выбирается явно (§8.1). По умолчанию subscription (внутренний контур):
#   движок бежит на Pro/Max-токене (CLAUDE_CODE_OAUTH_TOKEN из `claude setup-token`).
#   --auth-mode api — контур на ANTHROPIC_API_KEY, требует лимитов; смешивать контуры нельзя.
#   --audience client (бот общается с внешними людьми) на подписке = отказ КОДОМ: обслуживать
#   третьих лиц с подписки запрещено ToS. Клиентская поверхность целиком — Фаза 4 (нет ToolPolicy).
#
# --cc-token: долгоживущий токен из `claude setup-token` (подписка). Пишется в .env инстанса как
#   CLAUDE_CODE_OAUTH_TOKEN → бот не зависит от ambient ~/.claude (чей access-token протухает → 401).
#
# --role unpacker: инстанс мета-агента «Распаковщик» (§7.5) — ему дополнительно ставится
#   systemd drop-in (sudo работает: без NoNewPrivileges) и sudoers-whitelist на конкретные скрипты.
#
# КОДЫ ВОЗВРАТА: 2 — плохие аргументы; 3 — не пройден гейт §7.3 / preflight.
#   Гейты §7.3 исполняет КОД и они НЕ снимаются --skip-preflight: «уговорить словами» нельзя.
#
# ВАЖНО: bypassPermissions под root ЗАПРЕЩЁН самим CLI. run-user не может быть root — безусловный
#   блокер (даже под --skip-preflight). Заведи непривилегированного юзера: TG_RUN_USER=<user>
#   (или запусти из-под него через sudo, тогда SUDO_USER подхватится). При `sudo` от root uv/claude
#   резолвятся В КОНТЕКСТЕ RUN_USER, а не root (иначе /root/.local/bin/uv недостижим для юнита).
#
# Шаги: гейты §7.3 (контур auth, мозг, чистота git-мозга, имя, занятость токена, getMe) → preflight →
#   общий движок (pull/uv sync под RUN_USER) → мозг (clone/copy; scrub .env/.git/симлинков) →
#   инстанс + .env (chmod 600, секреты через printf, НЕ перезатирая) → buttons.yaml (мост из мозга) →
#   сид проекта → templated systemd agent-tg@<name>.service + enable --now.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/_common.sh
. "$HERE/_common.sh"

resolve_run_identity

SURFACE="" NAME="" TOKEN="" USERS="" BRAIN_REPO="" PROJECT_SLUG="" BRAND=""
CC_TOKEN="" DEFAULT_MODEL="" DRY_RUN="false" SKIP_PREFLIGHT="false"
ROLE="agent" AUDIENCE="internal" AUTH_MODE="subscription" API_KEY=""
LIMIT_RPD="" LIMIT_TPD=""
# Куда стучимся за getMe. Переопределяется под локальный Bot API / прокси (и под тесты гейта).
TG_API_BASE="${TG_API_BASE:-https://api.telegram.org}"

usage() { awk 'NR==1{next} /^set -euo pipefail/{exit} {sub(/^# ?/,"");print}' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --surface)        SURFACE="$2"; shift 2 ;;
    --name)           NAME="$2"; shift 2 ;;
    --token)          TOKEN="$2"; shift 2 ;;
    --users)          USERS="$2"; shift 2 ;;
    --brain)          BRAIN_REPO="$2"; shift 2 ;;
    --project-slug)   PROJECT_SLUG="$2"; shift 2 ;;
    --brand)          BRAND="$2"; shift 2 ;;
    --cc-token)       CC_TOKEN="$2"; shift 2 ;;
    --default-model)  DEFAULT_MODEL="$2"; shift 2 ;;
    --role)           ROLE="$2"; shift 2 ;;
    --audience)       AUDIENCE="$2"; shift 2 ;;
    --auth-mode)      AUTH_MODE="$2"; shift 2 ;;
    --api-key)        API_KEY="$2"; shift 2 ;;
    --limit-requests-per-day) LIMIT_RPD="$2"; shift 2 ;;
    --limit-tokens-per-day)   LIMIT_TPD="$2"; shift 2 ;;
    --dry-run)        DRY_RUN="true"; shift ;;
    --skip-preflight) SKIP_PREFLIGHT="true"; shift ;;
    -h|--help)        usage 0 ;;
    *) echo "Неизвестный флаг: $1" >&2; usage 1 ;;
  esac
done

# ── БЕЗУСЛОВНЫЙ блокер root (вне preflight — --skip-preflight его НЕ снимает) ─
# Движок бежит с bypassPermissions, CLI отказывается работать с bypass под root → бот-зомби,
# молчащий на все сообщения. Единственный гейт с обходом = дыра, поэтому проверка защищена от skip.
if [ "$RUN_USER" = "root" ]; then
  echo "✗ run-user = root — bypassPermissions под root запрещён Claude CLI (бот молчал бы)." >&2
  echo "  Заведи непривилегированного юзера и передай TG_RUN_USER=<user> (или sudo из-под него)." >&2
  exit 3
fi

# ── валидация аргументов (до preflight и любых мутаций) ──────────────────────
case "$SURFACE" in
  tg) : ;;
  "") echo "--surface обязателен (в C1 поддержан только: tg)" >&2; exit 2 ;;
  *)  echo "--surface '$SURFACE' не реализован в C1 (только tg; web=фаза3, standalone=фаза2)" >&2; exit 2 ;;
esac
[ -n "$NAME" ]       || { echo "--name обязателен" >&2; exit 2; }
[ -n "$TOKEN" ]      || { echo "--token обязателен" >&2; exit 2; }
[ -n "$USERS" ]      || { echo "--users обязателен" >&2; exit 2; }
[ -n "$BRAIN_REPO" ] || { echo "--brain обязателен" >&2; exit 2; }
valid_slug "$NAME"   || { echo "--name '$NAME': недопустимый slug (^[a-z0-9]([a-z0-9-]*[a-z0-9])?\$)" >&2; exit 2; }
if ! printf '%s' "$TOKEN" | grep -Eq '^[0-9]+:[A-Za-z0-9_-]+$'; then
  echo "--token: непохож на токен бота (формат <digits>:<token>). getMe проверит валидность позже." >&2
  exit 2
fi
if ! printf '%s' "$USERS" | grep -Eq '^[0-9]+(,[0-9]+)*$'; then
  echo "--users '$USERS': нужен список целых через запятую, напр. 111,222" >&2; exit 2
fi
[ -n "$PROJECT_SLUG" ] || PROJECT_SLUG="$NAME"
valid_slug "$PROJECT_SLUG" || { echo "--project-slug '$PROJECT_SLUG': недопустимый slug" >&2; exit 2; }
[ -n "$BRAND" ] || BRAND="$NAME"
# BRAIN_REPO/RUNTIME_URL втекают в bash -c-строки (git/tar) → метасимволы запрещены.
# (BRAND/DEFAULT_MODEL уходят в seed через argv — им шелл-валидация не нужна, апостроф ок.)
safe_arg "$BRAIN_REPO" || { echo "--brain '$BRAIN_REPO': недопустимые символы (шелл-метасимволы запрещены)" >&2; exit 2; }
[ -z "${TG_RUNTIME_URL:-}" ] || safe_arg "${TG_RUNTIME_URL}" || { echo "TG_RUNTIME_URL: недопустимые символы" >&2; exit 2; }
# TG_API_BASE влияет на сетевой запрос гейта — метасимволы тут тоже не нужны.
safe_arg "$TG_API_BASE" || { echo "TG_API_BASE: недопустимые символы" >&2; exit 2; }

case "$ROLE" in
  agent|unpacker) : ;;
  *) echo "--role '$ROLE': допустимо agent|unpacker" >&2; exit 2 ;;
esac
case "$AUDIENCE" in
  internal|client) : ;;
  *) echo "--audience '$AUDIENCE': допустимо internal|client" >&2; exit 2 ;;
esac
case "$AUTH_MODE" in
  subscription|api) : ;;
  *) echo "--auth-mode '$AUTH_MODE': допустимо subscription|api" >&2; exit 2 ;;
esac
for lim in "$LIMIT_RPD" "$LIMIT_TPD"; do
  [ -z "$lim" ] && continue
  printf '%s' "$lim" | grep -Eq '^[1-9][0-9]*$' \
    || { echo "лимит '$lim': нужно целое > 0 (--limit-requests-per-day / --limit-tokens-per-day)" >&2; exit 2; }
done

# RUNTIME/AGENTS_BASE/BRAINS_BASE/VENV_PATH уже выставил resolve_run_identity (Р2:
# env → /etc/unpacker/engine.conf → фолбэк) — здесь их больше не переопределяем.
RUNTIME_URL="${TG_RUNTIME_URL:-}"
resolve_uv_bin
# SEC-8: путь до uv втекает sed'ом в root-устанавливаемый юнит → валидируем ДО подстановки.
validate_uv_bin || { echo "--> проверь TG_UV_BIN / TG_UV_BIN в /etc/unpacker/engine.conf" >&2; exit 2; }
INST="$AGENTS_BASE/$NAME"
BRAIN="$BRAINS_BASE/$NAME"
UNIT="$(unit_name "$NAME")"            # M-17: имя юнита — из _common.sh, не литералом
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

# Куда ставим юнит/drop-in и чем зовём systemctl. Переопределяемо (H6): иначе тесты на
# Linux с passwordless sudo ставили бы юниты в /etc/systemd/system живой машины и делали
# `enable --now`. На боевом VPS значения по умолчанию — те самые системные.
UNIT_BASE="${TG_UNIT_DIR:-/etc/systemd/system}"
DROPIN_BASE="${TG_DROPIN_DIR:-/etc/systemd/system}"
SYSTEMCTL="${TG_SYSTEMCTL:-systemctl}"

# systemctl может быть и заглушкой по абсолютному пути (TG_SYSTEMCTL) — тогда `command -v`
# по имени его не найдёт; проверяем именно то, чем будем звать.
have_systemctl() { command -v "$SYSTEMCTL" >/dev/null 2>&1; }
# systemctl подменён (TG_SYSTEMCTL) — это dev/тестовый стенд, sudo там не нужен и в
# неинтерактивной сессии он бы просто попросил пароль и повесил прогон.
SYSTEMCTL_SUDO="$SUDO"; [ -z "${TG_SYSTEMCTL:-}" ] || SYSTEMCTL_SUDO=""

# ensure_dir/install_root: каталог юнита и drop-in на VPS принадлежит root, а на стенде
# (TG_UNIT_DIR в tmp) — нам. sudo зовём только когда без него нельзя.
ensure_dir()   { [ -d "$1" ] || mkdir -p "$1" 2>/dev/null || $SUDO mkdir -p "$1"; }
install_root() {  # install_root <mode> <src> <dst>
  local dir; dir="$(dirname "$3")"
  ensure_dir "$dir"
  if [ -w "$dir" ]; then install -m "$1" "$2" "$3"
  else $SUDO install -m "$1" -o root -g root "$2" "$3"; fi
}

run()        { if [ "$DRY_RUN" = "true" ]; then echo "[dry-run] $*"; else "$@"; fi; }
# asuser: команда-СТРОКА как RUN_USER (для фикс-команд с валидированными путями).
asuser()     {
  if [ "$DRY_RUN" = "true" ]; then echo "[dry-run] (as $RUN_USER) $*";
  elif [ "$RUN_USER" = "$(id -un)" ]; then bash -c "$*";
  else sudo -u "$RUN_USER" -H bash -c "$*"; fi
}
# asuser_argv: команда как ARGV (без bash -c) — свободный текст (BRAND) не парсится шеллом.
asuser_argv() {
  if [ "$DRY_RUN" = "true" ]; then echo "[dry-run] (as $RUN_USER) $*";
  elif [ "$RUN_USER" = "$(id -un)" ]; then "$@";
  else sudo -u "$RUN_USER" -H "$@"; fi
}

# asuser_real: как asuser, но ИСПОЛНЯЕТ даже в --dry-run. Только для ЧТЕНИЯ (гейты):
# «грязный мозг» надо проверить и в dry-run, иначе план обещает деплой, который не поедет.
asuser_real() {
  if [ "$RUN_USER" = "$(id -un)" ]; then bash -c "$*";
  else sudo -u "$RUN_USER" -H bash -c "$*"; fi
}

# ── ГЕЙТЫ §7.3 — исполняет КОД, не LLM ──────────────────────────────────────
# Философия §7.5: Распаковщик-мозг собирает данные и показывает план, но пройти проверку
# «уговорив» не может — она здесь, в bash. Гейты гоняются ВСЕГДА: и в --dry-run (чтобы
# план не обещал невозможного), и под --skip-preflight (иначе флаг = обход гейта).

gate_fail() {
  echo "  ✗ $1" >&2
  echo "==> ГЕЙТ §7.3 НЕ ПРОЙДЕН — деплой остановлен. Это код, а не мнение: почини причину." >&2
  exit 3
}

# redact: секреты инстанса → [REDACTED] перед любой печатью (§8.2 secret-scrubber).
redact() {
  local s="$1"
  s="${s//$TOKEN/[REDACTED]}"
  if [ -n "$CC_TOKEN" ]; then s="${s//$CC_TOKEN/[REDACTED]}"; fi
  if [ -n "$API_KEY" ]; then s="${s//$API_KEY/[REDACTED]}"; fi
  printf '%s' "$s"
}

brain_is_url() { case "$1" in *://*|git@*:*) return 0 ;; *) return 1 ;; esac; }

# getMe: 0 — токен живой (GETME_USER=имя бота), 1 — Telegram отверг, 2 — не дозвонились.
# URL уходит curl'у через --config (stdin), а НЕ через argv: иначе токен видно в `ps` (§8.2).
GETME_USER="" GETME_ERR=""
tg_getme() {
  local raw code body
  if ! raw="$(printf 'url = "%s/bot%s/getMe"\n' "$TG_API_BASE" "$TOKEN" \
              | curl -sS --max-time 15 --config - -w '\n%{http_code}' 2>&1)"; then
    GETME_ERR="$(redact "$raw")"; return 2
  fi
  code="${raw##*$'\n'}"; body="${raw%$'\n'*}"
  # нормализуем пробелы: у Bot API «"ok":true», у локальных серверов бывает «"ok": true»
  body="$(printf '%s' "$body" | tr -d ' \t\n')"
  if [ "$code" != "200" ]; then GETME_ERR="HTTP $code — $(redact "$body")"; return 1; fi
  case "$body" in *'"ok":true'*) ;; *) GETME_ERR="ответ без ok:true — $(redact "$body")"; return 1 ;; esac
  GETME_USER="$(printf '%s' "$body" | sed -n 's/.*"username":"\([A-Za-z0-9_]*\)".*/\1/p')"
  return 0
}

gates() {
  echo "==> гейты §7.3 (проверяет КОД: словами их не обойти)"

  # 1. Контур auth (§8.1). Клиентская поверхность на подписке — нарушение ToS и оно
  #    энфорсится Anthropic (прецедент 04.04.2026), поэтому отказ здесь, а не в README.
  if [ "$AUDIENCE" = "client" ] && [ "$AUTH_MODE" != "api" ]; then
    gate_fail "клиентская поверхность (--audience client) на контуре подписки запрещена ToS:
      обслуживать третьих лиц с Pro/Max нельзя. Нужен свой API-ключ: --auth-mode api --api-key <KEY>
      плюс лимиты (--limit-requests-per-day, --limit-tokens-per-day)."
  fi
  if [ "$AUDIENCE" = "client" ]; then
    gate_fail "клиентская поверхность целиком — Фаза 4 конституции: в движке ещё нет ToolPolicy
      (урезание Bash/Write) и LimitsPolicy fail-closed. Поднимать бота для внешних людей без них
      я не буду — это fail-closed, а не забывчивость. Сейчас поддержан только --audience internal."
  fi
  if [ "$AUTH_MODE" = "api" ]; then
    [ -n "$API_KEY" ] || gate_fail "контур api выбран, но нет --api-key <ANTHROPIC_API_KEY>."
    { [ -n "$LIMIT_RPD" ] && [ -n "$LIMIT_TPD" ]; } || gate_fail \
      "для API-контура обязательны лимиты: --limit-requests-per-day N --limit-tokens-per-day N
      (pay-per-token без потолка = счёт на любую сумму)."
    [ -z "$CC_TOKEN" ] || gate_fail "смешаны контуры: --auth-mode api и --cc-token (подписка).
      Один инстанс — один контур (§8.1). Оставь что-то одно."
    echo "  ✓ контур auth: api (лимиты: $LIMIT_RPD запр./сут, $LIMIT_TPD ток./сут)"
    echo "  ! энфорс лимитов в движке — Фаза 4: сейчас они пишутся в .env как декларация,"
    echo "    ядро их ещё не читает. Держи потолок на стороне биллинга Anthropic."
  else
    [ -z "$API_KEY" ] || gate_fail "смешаны контуры: --api-key при --auth-mode subscription.
      Один инстанс — один контур (§8.1)."
    echo "  ✓ контур auth: subscription (внутренний, аудитория $AUDIENCE)"
  fi

  # 2. Мозг: существует и несёт личность. Проверяем ИСТОЧНИК до любых мутаций —
  #    иначе опечатка в пути уходила в `git clone` и падала уже после провизии.
  if brain_is_url "$BRAIN_REPO"; then
    echo "  ✓ мозг: git-URL (наличие CLAUDE.md проверю после клона)"
  elif [ -d "$BRAIN_REPO" ]; then
    [ -f "$BRAIN_REPO/CLAUDE.md" ] \
      || gate_fail "в мозге '$BRAIN_REPO' нет CLAUDE.md — это не папка-мозг (§4: минимум один CLAUDE.md)."
    echo "  ✓ мозг: $BRAIN_REPO (CLAUDE.md на месте)"
  else
    gate_fail "мозг '$BRAIN_REPO' не найден: это ни существующая папка, ни git-URL. Проверь путь."
  fi

  # 3. Рабочее дерево git-мозга чистое (§6.1): иначе следующий `git pull` упрётся в конфликт
  #    и «идемпотентный повторный деплой» умрёт. Читаем как ВЛАДЕЛЕЦ репо: root против
  #    чужого репо получает 'dubious ownership' и молча выглядел бы как «чисто».
  if [ -d "$BRAIN_REPO/.git" ]; then
    local st
    if st="$(asuser_real "git -C '$BRAIN_REPO' status --porcelain" 2>/dev/null)"; then
      [ -z "$st" ] || gate_fail "мозг-источник '$BRAIN_REPO' — git с ГРЯЗНЫМ деревом (dirty).
      Закоммить или спрячь правки (git stash) и повтори."
      echo "  ✓ мозг-git: рабочее дерево чистое"
    else
      gate_fail "не смог проверить чистоту git-мозга '$BRAIN_REPO' (права / dubious ownership?).
      Молча считать его чистым нельзя — останавливаюсь."
    fi
  fi

  # 4. Имя инстанса уникально: имя ↔ бот держим 1:1. Повторный деплой ТОГО ЖЕ бота — норма
  #    (идемпотентность), а вот занять чужое имя другим токеном = потерять привязку и логи.
  if [ -f "$INST/.env" ]; then
    local cur
    cur="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$INST/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)"
    if [ -n "$cur" ] && [ "$cur" != "$TOKEN" ]; then
      gate_fail "имя '$NAME' уже занято ДРУГИМ ботом (в $INST/.env другой токен).
      Возьми другое --name либо снеси инстанс осознанно: systemctl disable --now $UNIT && rm -rf $INST"
    fi
    echo "  ✓ имя '$NAME': это повторный деплой того же бота (идемпотентно)"
  else
    echo "  ✓ имя '$NAME': свободно"
  fi

  # 5. Токен не занят другим живым инстансом: два polling'а на один токен = 409 у обоих,
  #    боты «дерутся» за апдейты и оба выглядят сломанными.
  if [ -d "$AGENTS_BASE" ]; then
    local d other
    for d in "$AGENTS_BASE"/*/; do
      [ -d "$d" ] || continue
      other="$(basename "$d")"
      [ "$other" = "$NAME" ] && continue
      [ -f "${d}.env" ] || continue
      if printf 'TELEGRAM_BOT_TOKEN=%s\n' "$TOKEN" | grep -qxFf - "${d}.env" 2>/dev/null; then
        gate_fail "этот токен уже ЗАНЯТ инстансом '$other' ($d) — двойной polling даст 409
      и оба бота начнут терять сообщения. Возьми у @BotFather отдельного бота либо сноси '$other'."
      fi
    done
  fi
  echo "  ✓ токен не занят другим инстансом"

  # 6. Токен валиден по getMe. Сеть недоступна → ОТКАЗ, а не тихий проход: молча пропущенный
  #    гейт хуже отсутствующего (ученик увидит «деплой ок» и мёртвого бота).
  command -v curl >/dev/null 2>&1 || gate_fail "нужен curl для проверки токена (apt install curl)."
  local g=0
  set +e; tg_getme; g=$?; set -e
  case "$g" in
    0) echo "  ✓ токен валиден (getMe): бот @${GETME_USER:-неизвестен}, сам токен — [REDACTED]" ;;
    1) gate_fail "Telegram отверг токен (getMe): $GETME_ERR
      Токен в выводе — [REDACTED]. Проверь его у @BotFather (не отозван ли) и передай заново." ;;
    2) gate_fail "не смог проверить токен: сеть до $TG_API_BASE недоступна ($GETME_ERR).
      Тихого прохода мимо гейта нет — почини сеть/прокси (или укажи TG_API_BASE) и повтори." ;;
  esac

  echo "    все гейты §7.3 пройдены"
}

gates

# ── preflight: падать РАНО и ВНЯТНО ─────────────────────────────────────────
preflight() {
  local errs=0 warns=0
  echo "==> preflight (run-user=$RUN_USER, uv=$UV_BIN, runtime=$RUNTIME)"

  id "$RUN_USER" >/dev/null 2>&1 \
    || { echo "  ✗ run-user '$RUN_USER' не существует — создай его или TG_RUN_USER=<кто-есть>"; errs=$((errs+1)); }
  command -v git >/dev/null 2>&1 || { echo "  ✗ git не найден (apt install git)"; errs=$((errs+1)); }
  "$UV_BIN" --version >/dev/null 2>&1 || {
    echo "  ✗ uv не запускается по пути '$UV_BIN' (резолвлен для $RUN_USER)"
    echo "      install под $RUN_USER: curl -LsSf https://astral.sh/uv/install.sh | sh   (или TG_UV_BIN=/path/uv)"
    errs=$((errs+1)); }

  if command -v python3 >/dev/null 2>&1; then
    local pv; pv="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo 0.0)"
    awk "BEGIN{exit !($pv < 3.11)}" && { echo "  ! system python3 $pv (<3.11) — uv поднимет свой, ок"; warns=$((warns+1)); }
  fi

  # claude CLI проверяем В КОНТЕКСТЕ RUN_USER (юнит бежит от него; root-PATH — ложная зелёнка).
  local has_claude
  if [ "$RUN_USER" = "$(id -un)" ]; then has_claude="$(command -v claude 2>/dev/null || true)"
  else has_claude="$(sudo -u "$RUN_USER" -H bash -lc 'command -v claude' 2>/dev/null || true)"; fi
  [ -n "$has_claude" ] || {
    echo "  ! claude CLI не найден в PATH у $RUN_USER — движок не сможет спавнить агента."
    echo "      поставь Claude Code CLI (юнит подхватит его через Environment=PATH)."; warns=$((warns+1)); }

  if [ -z "$CC_TOKEN" ] && [ ! -f "$INST/.env" ]; then
    echo "  ! нет --cc-token: бот поедет на ambient-auth и упадёт в 401 при протухании."
    echo "      durable: 'claude setup-token' (Max/Pro) под $RUN_USER, потом --cc-token <TOKEN>."; warns=$((warns+1))
  fi

  if have_systemctl && [ "$(id -u)" -ne 0 ] && ! sudo -n true 2>/dev/null; then
    echo "  ! не root и нет passwordless sudo — шаг systemd попросит пароль (похоже на зависание)."; warns=$((warns+1))
  fi

  if [ "$errs" -gt 0 ]; then
    echo "==> preflight ПРОВАЛ: $errs блокер(ов), $warns предупреждение(й). Почини ✗ или --skip-preflight."
    exit 3
  fi
  echo "    preflight ок — $warns предупреждение(й)"
}

if [ "$SKIP_PREFLIGHT" = "true" ]; then echo "==> preflight пропущен (--skip-preflight; блокер root всё равно применён)"; else preflight; fi

echo "==> деплой '$NAME' (surface=$SURFACE, project=$PROJECT_SLUG)"
echo "    runtime=$RUNTIME  instance=$INST  brain=$BRAIN"

# ── 1. общий движок (обновление ГРОМКОЕ, sync под RUN_USER) ──────────────────
if [ -d "$RUNTIME/.git" ]; then
  asuser "git -C '$RUNTIME' pull --ff-only" \
    || { echo "  ✗ обновление движка ($RUNTIME) не удалось — не раскатываю на устаревшем/битом коде." >&2; exit 3; }
elif [ -n "$RUNTIME_URL" ]; then
  asuser "git clone '$RUNTIME_URL' '$RUNTIME'"
else
  echo "warn: движок не git и TG_RUNTIME_URL пуст — считаю, что он уже лежит в $RUNTIME (rsync-bootstrap)"
fi
# sync под RUN_USER: .venv/managed-python обязаны принадлежать тому, кто бежит юнит (иначе EACCES).
asuser_argv "$UV_BIN" sync --project "$RUNTIME"

# ── 2. мозг (чистота источника проверена гейтом; scrub; CLAUDE.md после клона) ─
# materialize (условия — локальный stat; root читает /home/*; git-операции идут asuser)
if [ -d "$BRAIN/.git" ]; then
  asuser "git -C '$BRAIN' pull --ff-only" || echo "warn: brain pull пропущен (расхождение/сеть) — проверь мозг вручную"
elif [ -d "$BRAIN" ] && [ -n "$(ls -A "$BRAIN" 2>/dev/null)" ]; then
  echo "    мозг уже лежит (non-git) — не трогаю (idempotent)"
elif [ -d "$BRAIN_REPO" ]; then
  echo "    мозг — локальная папка, копирую в $BRAIN (без .git/.env, unanchored)"
  asuser "mkdir -p '$BRAIN' && tar -C '$BRAIN_REPO' --exclude='.git' --exclude='.env' --exclude='.env.*' -cf - . | tar -C '$BRAIN' -xf -"
else
  asuser "git clone '$BRAIN_REPO' '$BRAIN'"
fi
if [ "$DRY_RUN" != "true" ]; then
  # scrub: вложенные .env/.env.* (§8.2 утечка секрета в cwd бота) + симлинки (path-traversal наружу)
  # + вложенные .git (submodule). Топ-уровневый .git оставляем для git-мозга (нужен pull).
  asuser "find '$BRAIN' -depth \\( -name .env -o -name '.env.*' \\) -type f -delete 2>/dev/null || true"
  asuser "find '$BRAIN' -type l -delete 2>/dev/null || true"
  asuser "find '$BRAIN' -mindepth 2 -name .git -exec rm -rf {} + 2>/dev/null || true"
  # §7.3: мозг существует и в нём CLAUDE.md — ловит half-clone/пустую/битую папку.
  [ -f "$BRAIN/CLAUDE.md" ] || { echo "  ✗ в мозге $BRAIN нет CLAUDE.md — неполный/битый мозг. Останавливаюсь." >&2; exit 3; }
fi

# ── 3. инстанс-каталог + .env (idempotent, chmod 600, секреты через printf) ──
asuser "mkdir -p '$INST/state/uploads'"
if [ -f "$INST/.env" ]; then
  echo "    .env уже есть — не трогаю (idempotent)"
  if [ -n "$CC_TOKEN" ]; then
    if [ "$DRY_RUN" = "true" ]; then echo "[dry-run] ротировал бы CLAUDE_CODE_OAUTH_TOKEN в $INST/.env"; else
      TMP_ENV="$(mktemp)"
      grep -v '^CLAUDE_CODE_OAUTH_TOKEN=' "$INST/.env" > "$TMP_ENV" || true
      printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$CC_TOKEN" >> "$TMP_ENV"
      install -m 600 "$TMP_ENV" "$INST/.env"; rm -f "$TMP_ENV"
      [ "$RUN_USER" = "$(id -un)" ] || $SUDO chown "$RUN_USER" "$INST/.env"
      echo "    cc-token ротирован — рестартни: $SUDO $SYSTEMCTL restart $UNIT"
    fi
  fi
else
  if [ "$DRY_RUN" = "true" ]; then
    echo "[dry-run] записал бы $INST/.env (chmod 600, AUTH_MODE=$AUTH_MODE, TELEGRAM_BOT_TOKEN и"
    echo "          прочие секреты — через printf, без argv; значения в чат не печатаются)"
  else
    TMP_ENV="$(mktemp)"
    # Не-секретные плейсхолдеры — через sed. Секреты (bot-token, cc-token, api-key) — printf-дописью,
    # чтобы НЕ светиться в argv sed (ps/proc/set -x) — §8.2.
    sed -e "s|{{USERS}}|$USERS|g" -e "s|{{PROJECT_SLUG}}|$PROJECT_SLUG|g" \
        "$HERE/templates/.env.template" > "$TMP_ENV"
    # Контур (§8.1) фиксируем в .env: инстанс живёт в ОДНОМ контуре, и это видно глазами.
    printf 'AUTH_MODE=%s\n' "$AUTH_MODE" >> "$TMP_ENV"
    printf 'TELEGRAM_BOT_TOKEN=%s\n' "$TOKEN" >> "$TMP_ENV"
    if [ "$AUTH_MODE" = "api" ]; then
      printf 'ANTHROPIC_API_KEY=%s\n' "$API_KEY" >> "$TMP_ENV"
      # Декларация лимитов: энфорс — Фаза 4 (LimitsPolicy), гейт про это предупредил.
      printf 'LIMIT_REQUESTS_PER_DAY=%s\nLIMIT_TOKENS_PER_DAY=%s\n' "$LIMIT_RPD" "$LIMIT_TPD" >> "$TMP_ENV"
    elif [ -n "$CC_TOKEN" ]; then
      printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$CC_TOKEN" >> "$TMP_ENV"
    fi
    install -m 600 "$TMP_ENV" "$INST/.env"; rm -f "$TMP_ENV"
    [ "$RUN_USER" = "$(id -un)" ] || $SUDO chown "$RUN_USER" "$INST/.env"
    echo "    .env записан (chmod 600, контур $AUTH_MODE, секреты через printf)"
  fi
fi

# ── 3b. мост «мозг → инстанс»: buttons.yaml (контракт со срезом С2) ──────────
# Кнопки-триггеры живут в паспорте мозга (.brain.yaml), а рабочая копия — в инстансе:
# владелец правит её у себя, и синк мозга её НЕ перезатирает (та же дисциплина, что у .env, §4).
# Владелец модуля engine/brainkit.py — срез С2. Если его ещё нет, деплой НЕ падает:
# бот без кнопок работоспособен, а молча пропущенный шаг был бы хуже громкого предупреждения.
BUTTONS="$INST/buttons.yaml"
if [ -f "$BUTTONS" ]; then
  echo "    buttons.yaml уже есть — не трогаю (idempotent)"
elif [ "$DRY_RUN" = "true" ]; then
  echo "[dry-run] экспортировал бы кнопки: python -m engine.brainkit export-buttons --brain $BRAIN --out $BUTTONS"
else
  # --directory, а НЕ --project: `uv run --project` НЕ меняет cwd, и `python -m` подхватил бы
  # пакет `engine` из каталога вызова (у deploy.sh это обычно чей-то чекаут репо) вместо
  # общего движка. Ловится тестом моста: со --project мост «не видел» модуль в рантайме.
  if bk_out="$(asuser_argv "$UV_BIN" run --directory "$RUNTIME" python -m engine.brainkit \
                export-buttons --brain "$BRAIN" --out "$BUTTONS" 2>&1)"; then
    echo "    buttons.yaml экспортирован из мозга (engine.brainkit)"
  else
    echo "warn: не смог экспортировать buttons.yaml через engine.brainkit — кнопки-триггеры"
    echo "      останутся пустыми (бот работает). Причина (первая строка):"
    printf '%s\n' "$bk_out" | head -1 | sed 's/^/      /'
  fi
fi

# ── 4. сид дефолтного проекта→мозг (иначе router → NoProjectError) ───────────
# argv (asuser_argv), НЕ bash -c-строка: BRAND='Никита's бот' безопасен, инъекции нет.
if [ "$DRY_RUN" = "true" ]; then
  echo "[dry-run] сид проекта: slug=$PROJECT_SLUG brand='$BRAND' brain=$BRAIN db=$INST/state/state.db"
else
  # --directory (см. про мост выше): гарантирует, что `engine` берётся из общего движка.
  seed_args=("$UV_BIN" run --directory "$RUNTIME" python -m engine.seed
             --db "$INST/state/state.db" --slug "$PROJECT_SLUG" --name "$BRAND" --brain "$BRAIN")
  [ -n "$DEFAULT_MODEL" ] && seed_args+=(--model "$DEFAULT_MODEL")
  asuser_argv "${seed_args[@]}"
fi

# ── 4b. модель прав мета-агента (§7.5) — только --role unpacker ──────────────
# Обычному агенту sudo не нужен и он его НЕ получает: послабления живут в drop-in'е
# конкретного инстанса, базовый agent-tg@.service остаётся под NoNewPrivileges=true.
if [ "$ROLE" = "unpacker" ]; then
  echo "==> Распаковщик: модель прав §7.5 (drop-in юнита + sudoers-whitelist)"
  if have_systemctl; then
    DROPIN_DIR="$DROPIN_BASE/$UNIT.service.d"
    if [ "$DRY_RUN" = "true" ]; then
      echo "[dry-run] поставил бы drop-in $DROPIN_DIR/10-unpacker.conf (NoNewPrivileges=no, остальной hardening)"
    else
      # daemon-reload не зову: шаг 5 ниже всё равно его делает перед enable --now
      install_root 644 "$HERE/templates/unpacker-dropin.conf" "$DROPIN_DIR/10-unpacker.conf"
      echo "    drop-in установлен: $DROPIN_DIR/10-unpacker.conf"
    fi
  else
    echo "warn: нет systemctl — drop-in Распаковщика не поставлен (dev-хост). На VPS шаг сработает."
  fi
  # Права ставит отдельный идемпотентный скрипт. Его провал НЕ роняет деплой: бот уже живой,
  # а права доставляются одной командой — её и печатаем, чтобы человек не гадал.
  if [ "$DRY_RUN" = "true" ]; then
    "$HERE/install-sudoers.sh" --dry-run || echo "warn: sudoers-whitelist в этих условиях не встанет (см. ✗ выше)"
  else
    "$HERE/install-sudoers.sh" || {
      echo "warn: sudoers-whitelist не установлен — Распаковщик не сможет разворачивать ботов."
      echo "      Поставь вручную (нужен root):  sudo TG_RUN_USER=$RUN_USER TG_RUNTIME=$RUNTIME $HERE/install-sudoers.sh"
    }
  fi
fi

# ── 5. systemd templated unit + autostart ───────────────────────────────────
if ! have_systemctl; then
  echo "warn: systemctl не найден — пропускаю установку юнита (dev-хост, напр. macOS)."
  echo "      На VPS этот шаг поставит $UNIT.service и enable --now."
else
  UNIT_SRC="$HERE/templates/$(unit_template)"
  UNIT_DST="$UNIT_BASE/$(unit_template)"
  if [ -f "$UNIT_SRC" ]; then
    TMP_UNIT="$(mktemp)"
    # Истинный шаблон: пер-агентные пути через %i. Подставляем только ХОСТ-глобальные значения.
    # PATH включает $RUN_HOME/.local/bin (per-user claude/uv) + системные — иначе SDK не найдёт claude.
    sed -e "s|REPLACE_WITH_LINUX_USER|$RUN_USER|g" \
        -e "s|REPLACE_WITH_AGENTS_BASE|$AGENTS_BASE|g" \
        -e "s|REPLACE_WITH_UV_PATH|$UV_BIN|g" \
        -e "s|REPLACE_WITH_RUNTIME_PATH|$RUNTIME|g" \
        -e "s|REPLACE_WITH_RUN_HOME|$RUN_HOME|g" \
        "$UNIT_SRC" > "$TMP_UNIT"
    if [ "$DRY_RUN" = "true" ]; then
      echo "[dry-run] поставил бы $UNIT_DST и: $SUDO $SYSTEMCTL enable --now $UNIT"
      rm -f "$TMP_UNIT"
    else
      install_root 644 "$TMP_UNIT" "$UNIT_DST"; rm -f "$TMP_UNIT"
      $SYSTEMCTL_SUDO "$SYSTEMCTL" daemon-reload
      $SYSTEMCTL_SUDO "$SYSTEMCTL" enable --now "$UNIT"
      echo "    systemd: $UNIT enabled + started"
    fi
  else
    echo "warn: шаблон юнита не найден: $UNIT_SRC"
  fi
fi

echo "==> готово. engine=$RUNTIME (общий)  brain=$BRAIN  instance=$INST"
echo "    диагностика:  $HERE/agentctl.sh doctor $NAME"
