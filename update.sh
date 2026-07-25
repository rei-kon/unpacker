#!/usr/bin/env bash
# update.sh — обновить движок «Распаковщик» и откатиться, если новая версия не понравилась (§10).
#
# Главное правило: ставим ПОСЛЕДНИЙ ТЕГ-релиз, а НЕ HEAD ветки. Один неудачный пуш автора
# репо не должен положить ботов всех учеников — поэтому HEAD никто не тянет.
#
# Использование:
#   bash update.sh                  # обновиться до последнего релиза
#   bash update.sh --dry-run        # показать план, ничего не менять
#   bash update.sh --rollback       # вернуться на предыдущий релиз
#   bash update.sh --ref v0.3.0     # встать на конкретный релиз
#
# Флаги:
#   --engine-dir <путь>   где лежит движок (по умолчанию /opt/unpacker)
#   --ref <тег>           конкретный релиз вместо последнего
#   --rollback            вернуться на релиз, с которого обновлялись в прошлый раз
#   --initiator <имя>     агент, чей юнит рестартуется ПОСЛЕДНИМ (по умолчанию unpacker)
#   --dry-run             показать план, ничего не менять
#   -h, --help            эта справка
#
# Порядок шагов: обновить теги → выбрать релиз → БЭКАП баз сессий (до любых миграций) →
# запомнить текущий релиз для отката → checkout → uv sync → рестарт ботов.
# Юнит инициатора (Распаковщика) рестартуется ПОСЛЕДНИМ: он же ведёт обновление из чата и,
# рестартовав себя раньше, убил бы операцию на середине.
set -euo pipefail

ENGINE_DIR="/opt/unpacker" REF="" ROLLBACK="false" DRY_RUN="false" INITIATOR="unpacker"

usage() { awk 'NR==1{next} /^set -euo pipefail/{exit} {sub(/^# ?/,"");print}' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --engine-dir) ENGINE_DIR="$2"; shift 2 ;;
    --ref)        REF="$2"; shift 2 ;;
    --rollback)   ROLLBACK="true"; shift ;;
    --initiator)  INITIATOR="$2"; shift 2 ;;
    --dry-run)    DRY_RUN="true"; shift ;;
    -h|--help)    usage 0 ;;
    *) echo "Неизвестный флаг: $1 (см. --help)" >&2; exit 2 ;;
  esac
done

SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
run() { if [ "$DRY_RUN" = "true" ]; then echo "[dry-run] $*"; else "$@"; fi; }
say() { printf '%s\n' "$1"; }
have() { "$1" --version >/dev/null 2>&1; }

if [ ! -d "$ENGINE_DIR/.git" ]; then
  echo "✗ в $ENGINE_DIR нет кода движка (это не git-репо)." >&2
  echo "  Если движок ещё не ставился — начни с install.sh." >&2
  echo "  Если он лежит в другом месте — укажи: update.sh --engine-dir <путь>" >&2
  exit 2
fi

# Пути инстансов и юзера движка резолвим кодом deploy/_common.sh — единый источник правды
# с deploy.sh/agentctl.sh (иначе бэкапили бы «не те» базы).
if [ -r "$ENGINE_DIR/deploy/_common.sh" ]; then
  # shellcheck source=deploy/_common.sh
  . "$ENGINE_DIR/deploy/_common.sh"
  resolve_run_identity
  resolve_uv_bin
else
  echo "✗ не нашёл $ENGINE_DIR/deploy/_common.sh — код движка неполный, переустанови install.sh" >&2
  exit 2
fi

PREV_FILE="$ENGINE_DIR/.update-prev"

# Текущий релиз: тег, если HEAD ровно на теге, иначе короткий хеш (dev-состояние).
current_release() {
  git -C "$ENGINE_DIR" describe --tags --exact-match HEAD 2>/dev/null \
    || git -C "$ENGINE_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown"
}
# `|| true` обязателен: под pipefail пустой список тегов уронил бы скрипт молча.
latest_tag() { git -C "$ENGINE_DIR" tag --sort=-v:refname 2>/dev/null | head -1 || true; }

CURRENT="$(current_release)"
say "==> движок: $ENGINE_DIR (сейчас: $CURRENT)"

if ! run git -C "$ENGINE_DIR" fetch --tags --force; then
  say "  ! не смог получить свежие теги (сеть/доступ к репо) — работаю с тем, что уже скачано"
fi

# ── что ставим ───────────────────────────────────────────────────────────────
if [ "$ROLLBACK" = "true" ]; then
  TARGET=""
  [ -r "$PREV_FILE" ] && TARGET="$(tr -d '[:space:]' < "$PREV_FILE")"
  if [ -z "$TARGET" ]; then
    echo "✗ откат невозможен: я не знаю, с какого релиза ты обновлялся (нет $PREV_FILE)." >&2
    echo "  Посмотри доступные релизы:  git -C $ENGINE_DIR tag" >&2
    echo "  И встань на нужный руками:  update.sh --ref <тег>" >&2
    exit 2
  fi
  say "==> откат на предыдущий релиз: $TARGET"
elif [ -n "$REF" ]; then
  TARGET="$REF"
else
  TARGET="$(latest_tag)"
  if [ -z "$TARGET" ]; then
    echo "✗ в репо нет тегов-релизов — обновлять не на что." >&2
    echo "  Релизы движка помечаются тегами; HEAD ветки я не тяну намеренно" >&2
    echo "  (один плохой пуш не должен положить твоего бота)." >&2
    exit 2
  fi
fi

if [ "$TARGET" = "$CURRENT" ]; then
  say "    уже на релизе $CURRENT — обновлять нечего, ничего не трогаю."
  exit 0
fi
git -C "$ENGINE_DIR" rev-parse --verify --quiet "$TARGET^{commit}" >/dev/null || {
  echo "✗ релиза '$TARGET' в репо нет. Список: git -C $ENGINE_DIR tag" >&2; exit 2; }

say "==> план: $CURRENT → $TARGET"

# ── бэкап баз сессий ДО переключения кода ───────────────────────────────────
# Миграции идут при старте нового кода; если что-то пойдёт не так, сессии ботов (переписка
# и resume) должны восстанавливаться из копии, а не «ну, бывает».
backup_dbs() {
  local ts; ts="$(date +%Y%m%d-%H%M%S)"
  if [ ! -d "$AGENTS_BASE" ]; then
    say "  ! инстансов в $AGENTS_BASE нет — бэкапить нечего"
    return 0
  fi
  local inst db dst name
  for inst in "$AGENTS_BASE"/*/; do
    [ -d "$inst/state" ] || continue
    name="$(basename "$inst")"
    for db in "$inst"state/*.db; do
      [ -f "$db" ] || continue
      dst="$inst""state/backups/$(basename "$db").$CURRENT-$ts"
      if [ "$DRY_RUN" = "true" ]; then
        echo "[dry-run] сделал бы бэкап $db → $dst"
        continue
      fi
      mkdir -p "$inst""state/backups"
      # sqlite3 .backup даёт согласованный снимок при живом боте (WAL); cp — фолбэк.
      if have sqlite3 && sqlite3 "$db" ".backup '$dst'" 2>/dev/null; then
        say "    бэкап (sqlite): $name → $(basename "$dst")"
      else
        cp "$db" "$dst"
        say "    бэкап (копия): $name → $(basename "$dst")"
      fi
    done
  done
}

say "==> бэкап баз сессий"
backup_dbs

# Запоминаем, откуда ушли — это и есть точка отката. Пишем ДО checkout: если переключение
# сорвётся на середине, откат всё равно будет знать адрес возврата.
if [ "$ROLLBACK" != "true" ]; then
  if [ "$DRY_RUN" = "true" ]; then
    echo "[dry-run] запомнил бы точку отката ($CURRENT) в $PREV_FILE"
  else
    printf '%s\n' "$CURRENT" > "$PREV_FILE"
  fi
fi

# ── переключение кода + зависимости ─────────────────────────────────────────
say "==> переключаю код на $TARGET"
run git -C "$ENGINE_DIR" checkout --quiet "$TARGET"
# sync под юзером движка: .venv обязан принадлежать тому, кто бежит юнит (иначе EACCES).
if [ "$DRY_RUN" = "true" ]; then
  echo "[dry-run] (as $RUN_USER) $UV_BIN sync --project $ENGINE_DIR"
elif [ "$RUN_USER" = "$(id -un)" ]; then
  "$UV_BIN" sync --project "$ENGINE_DIR"
else
  $SUDO -u "$RUN_USER" -H "$UV_BIN" sync --project "$ENGINE_DIR"
fi

# ── рестарт ботов: инициатор ПОСЛЕДНИМ ──────────────────────────────────────
restart_agents() {
  if ! command -v systemctl >/dev/null 2>&1; then
    say "  ! systemctl не найден (dev-хост) — рестарт ботов пропущен"
    return 0
  fi
  if [ ! -d "$AGENTS_BASE" ]; then
    say "  ! инстансов нет — рестартовать нечего"
    return 0
  fi
  local inst name others=() last=""
  for inst in "$AGENTS_BASE"/*/; do
    [ -d "$inst" ] || continue
    name="$(basename "$inst")"
    if [ "$name" = "$INITIATOR" ]; then last="$name"; else others+=("$name"); fi
  done
  for name in ${others+"${others[@]}"}; do
    run $SUDO systemctl restart "agent-tg@$name"
    say "    рестарт: $name"
  done
  if [ -n "$last" ]; then
    say "    рестарт инициатора последним: $last"
    run $SUDO systemctl restart "agent-tg@$last"
  fi
}

say "==> рестарт ботов"
restart_agents

say ""
# В dry-run нельзя писать «готово»: код не переключён, бэкапов нет, боты не рестартованы.
if [ "$DRY_RUN" = "true" ]; then
  say "==> это был только план ($CURRENT → $TARGET) — ничего не изменено."
  say "    Устраивает? Запусти без --dry-run."
  exit 0
fi
say "==> готово: $CURRENT → $TARGET"
say "    не понравилось?      bash $ENGINE_DIR/update.sh --rollback   (вернёт $CURRENT)"
say "    бэкапы баз сессий    $AGENTS_BASE/<агент>/state/backups/"
say "    проверить бота       $ENGINE_DIR/deploy/agentctl.sh doctor $INITIATOR"
