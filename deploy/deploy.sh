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
#             [--cc-token <OAUTH>] [--default-model <m>] [--dry-run] [--skip-preflight]
#
# AUTH = ТОЛЬКО ПОДПИСКА (§8.1 внутренний контур). Движок бежит на Pro/Max-токене
#   (CLAUDE_CODE_OAUTH_TOKEN из `claude setup-token`). API-ключ (ANTHROPIC_API_KEY) здесь
#   не используется, не предлагается и не пишется — код-гейт клиентского контура (фаза 4).
#
# --cc-token: долгоживущий токен из `claude setup-token` (подписка). Пишется в .env инстанса как
#   CLAUDE_CODE_OAUTH_TOKEN → бот не зависит от ambient ~/.claude (чей access-token протухает → 401).
#
# ВАЖНО: bypassPermissions под root ЗАПРЕЩЁН самим CLI. run-user не может быть root — безусловный
#   блокер (даже под --skip-preflight). Заведи непривилегированного юзера: TG_RUN_USER=<user>
#   (или запусти из-под него через sudo, тогда SUDO_USER подхватится). При `sudo` от root uv/claude
#   резолвятся В КОНТЕКСТЕ RUN_USER, а не root (иначе /root/.local/bin/uv недостижим для юнита).
#
# Шаги: preflight → общий движок (pull/uv sync под RUN_USER) → мозг (clone/copy; ГРЯЗНЫЙ git-мозг =
#   СТОП; scrub .env/.git/симлинков; требуется CLAUDE.md) → инстанс + .env (chmod 600, секреты через
#   printf, НЕ перезатирая) → сид проекта → templated systemd agent-tg@<name>.service + enable --now.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/_common.sh
. "$HERE/_common.sh"

resolve_run_identity

SURFACE="" NAME="" TOKEN="" USERS="" BRAIN_REPO="" PROJECT_SLUG="" BRAND=""
CC_TOKEN="" DEFAULT_MODEL="" DRY_RUN="false" SKIP_PREFLIGHT="false"

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

RUNTIME="${TG_RUNTIME:-/opt/unpacker}"
RUNTIME_URL="${TG_RUNTIME_URL:-}"
resolve_uv_bin
INST="$AGENTS_BASE/$NAME"
BRAIN="$BRAINS_BASE/$NAME"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

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

  if command -v systemctl >/dev/null 2>&1 && [ "$(id -u)" -ne 0 ] && ! sudo -n true 2>/dev/null; then
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

# ── 2. мозг (ГРЯЗНЫЙ git-мозг = СТОП §6.1; scrub; требуется CLAUDE.md §7.3) ──
if [ "$DRY_RUN" != "true" ]; then
  # dirty-check ИСТОЧНИКА как ВЛАДЕЛЬЦА: root против canary-owned репо даёт 'dubious ownership',
  # git падает — и 2>/dev/null проглотил бы это как «чисто». Гоняем как RUN_USER (владелец на VPS).
  if [ -d "$BRAIN_REPO/.git" ]; then
    if st="$(asuser "git -C '$BRAIN_REPO' status --porcelain" 2>/dev/null)"; then
      [ -z "$st" ] || { echo "  ✗ мозг-источник '$BRAIN_REPO' — git с ГРЯЗНЫМ деревом (dirty). Закоммить/спрячь и повтори." >&2; exit 3; }
    else
      echo "  ✗ не смог проверить чистоту мозга '$BRAIN_REPO' (права/dubious ownership?). Останавливаюсь." >&2; exit 3
    fi
  fi
fi
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
      echo "    cc-token ротирован — рестартни: $SUDO systemctl restart agent-tg@$NAME"
    fi
  fi
else
  if [ "$DRY_RUN" = "true" ]; then
    echo "[dry-run] записал бы $INST/.env (chmod 600, TELEGRAM_BOT_TOKEN$( [ -n "$CC_TOKEN" ] && echo ' + CLAUDE_CODE_OAUTH_TOKEN') через printf)"
  else
    TMP_ENV="$(mktemp)"
    # Не-секретные плейсхолдеры — через sed. Секреты (bot-token, cc-token) — printf-дописью,
    # чтобы НЕ светиться в argv sed (ps/proc/set -x) — §8.2.
    sed -e "s|{{USERS}}|$USERS|g" -e "s|{{PROJECT_SLUG}}|$PROJECT_SLUG|g" \
        "$HERE/templates/.env.template" > "$TMP_ENV"
    printf 'TELEGRAM_BOT_TOKEN=%s\n' "$TOKEN" >> "$TMP_ENV"
    [ -n "$CC_TOKEN" ] && printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$CC_TOKEN" >> "$TMP_ENV"
    install -m 600 "$TMP_ENV" "$INST/.env"; rm -f "$TMP_ENV"
    [ "$RUN_USER" = "$(id -un)" ] || $SUDO chown "$RUN_USER" "$INST/.env"
    echo "    .env записан (chmod 600, секреты через printf)"
  fi
fi

# ── 4. сид дефолтного проекта→мозг (иначе router → NoProjectError) ───────────
# argv (asuser_argv), НЕ bash -c-строка: BRAND='Никита's бот' безопасен, инъекции нет.
if [ "$DRY_RUN" = "true" ]; then
  echo "[dry-run] сид проекта: slug=$PROJECT_SLUG brand='$BRAND' brain=$BRAIN db=$INST/state/state.db"
else
  seed_args=("$UV_BIN" run --project "$RUNTIME" python -m engine.seed
             --db "$INST/state/state.db" --slug "$PROJECT_SLUG" --name "$BRAND" --brain "$BRAIN")
  [ -n "$DEFAULT_MODEL" ] && seed_args+=(--model "$DEFAULT_MODEL")
  asuser_argv "${seed_args[@]}"
fi

# ── 5. systemd templated unit + autostart ───────────────────────────────────
if ! command -v systemctl >/dev/null 2>&1; then
  echo "warn: systemctl не найден — пропускаю установку юнита (dev-хост, напр. macOS)."
  echo "      На VPS этот шаг поставит agent-tg@$NAME.service и enable --now."
else
  UNIT_SRC="$HERE/templates/agent-tg@.service"
  UNIT_DST="/etc/systemd/system/agent-tg@.service"
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
      echo "[dry-run] поставил бы $UNIT_DST и: $SUDO systemctl enable --now agent-tg@$NAME"
      rm -f "$TMP_UNIT"
    else
      $SUDO install -m 644 "$TMP_UNIT" "$UNIT_DST"; rm -f "$TMP_UNIT"
      $SUDO systemctl daemon-reload
      $SUDO systemctl enable --now "agent-tg@$NAME"
      echo "    systemd: agent-tg@$NAME enabled + started"
    fi
  else
    echo "warn: шаблон юнита не найден: $UNIT_SRC"
  fi
fi

echo "==> готово. engine=$RUNTIME (общий)  brain=$BRAIN  instance=$INST"
echo "    диагностика:  $HERE/agentctl.sh doctor $NAME"
