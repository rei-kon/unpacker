#!/usr/bin/env bash
# update.sh — обновить движок «Распаковщик» и откатиться, если новая версия не понравилась (§10).
#
# Главное правило: ставим ПОСЛЕДНИЙ ТЕГ-релиз, а НЕ HEAD ветки. Один неудачный пуш автора
# репо не должен положить ботов всех учеников — поэтому HEAD никто не тянет.
#
# Использование (от root, ровно в этой форме — она же разрешена в sudoers Распаковщика):
#   sudo /opt/unpacker/update.sh                 # обновиться до последнего релиза
#   sudo /opt/unpacker/update.sh --dry-run       # показать план, ничего не менять
#   sudo /opt/unpacker/update.sh --rollback      # вернуться на предыдущий релиз (КОД)
#   sudo /opt/unpacker/update.sh --restore-db    # вернуть базы сессий из последнего бэкапа
#   sudo /opt/unpacker/update.sh --ref v0.3.0    # встать на конкретный релиз
#
# Флаги:
#   --ref <тег>           конкретный релиз вместо последнего
#   --rollback            вернуться на релиз, с которого обновлялись в прошлый раз (только КОД)
#   --restore-db [<имя>]  восстановить базы сессий из последнего бэкапа (всем или одному агенту)
#   --initiator <имя>     агент, чей юнит рестартуется ПОСЛЕДНИМ (по умолчанию unpacker)
#   --dry-run             показать план, ничего не менять
#   -h, --help            эта справка
#
# КАТАЛОГ ДВИЖКА НЕ НАСТРАИВАЕТСЯ ФЛАГОМ (SEC-1). Он там, где лежит сам update.sh.
# Почему: скрипт выдан Распаковщику через sudoers NOPASSWD, а правило «команда без аргументов»
# в sudoers означает «с любыми аргументами». Пока существовал --engine-dir, агент мог создать
# свой каталог с файлом deploy/_common.sh и получить исполнение произвольного кода от root —
# скрипт сорсит этот файл ДО любых проверок. Флага нет — вектора нет.
#
# Порядок шагов: конфиг → обновить теги → выбрать релиз → БЭКАП баз сессий (до любых миграций,
# согласованный снимок) → запомнить текущий релиз для отката → checkout → uv sync → рестарт ботов.
# Юнит инициатора (Распаковщика) рестартуется ПОСЛЕДНИМ: он же ведёт обновление из чата и,
# рестартовав себя раньше, убил бы операцию на середине.
#
# Рычаги для тестов и нестандартных установок (в норме не нужны):
#   UNPACKER_ETC=/etc/unpacker            где лежит машинный конфиг engine.conf
#   UV_PROJECT_ENVIRONMENT=/var/lib/unpacker/venv   где живёт venv движка (ВНЕ дерева кода)
set -euo pipefail

# Каталог движка = каталог этого скрипта. Единственный источник — расположение файла.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$HERE"

REF="" ROLLBACK="false" RESTORE_DB="false" RESTORE_ONLY="" DRY_RUN="false" INITIATOR="unpacker"

usage() { awk 'NR==1{next} /^set -euo pipefail/{exit} {sub(/^# ?/,"");print}' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ref)        REF="$2"; shift 2 ;;
    --rollback)   ROLLBACK="true"; shift ;;
    --restore-db)
      RESTORE_DB="true"; shift
      # имя агента — необязательный аргумент сразу после флага
      if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then RESTORE_ONLY="$1"; shift; fi ;;
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
  echo "  update.sh обновляет движок, внутри которого лежит сам — запускай его оттуда:" >&2
  echo "    sudo /opt/unpacker/update.sh" >&2
  echo "  Если движок ещё не ставился — начни с install.sh." >&2
  exit 2
fi

# ── машинный конфиг: единая вселенная путей (Р2) ─────────────────────────────
# Личность run-user'а нельзя выводить из окружения вызывающего: root вручную, sudo от агента и
# cron дают три разных ответа, и update.sh тихо бэкапил бы «не те» базы (C6/ADV-06).
# Файл ПАРСИМ ПОСТРОЧНО, а не `source`: source конфига — тот же класс дыр, что SEC-1.
# Приоритет: окружение → engine.conf → фолбэк внутри _common.sh.
ETC_DIR="${UNPACKER_ETC:-/etc/unpacker}"
ENGINE_CONF="$ETC_DIR/engine.conf"
load_engine_conf() {
  [ -r "$ENGINE_CONF" ] || return 0
  local line key val
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key="${line%%=*}"; val="${line#*=}"
    [ "$key" != "$line" ] || continue
    case "$key" in
      TG_RUN_USER|TG_RUNTIME|TG_AGENTS_BASE|TG_BRAINS_BASE|TG_UV_BIN) : ;;
      *) continue ;;
    esac
    [ -z "${!key:-}" ] || continue   # окружение важнее конфига
    export "$key=$val"
  done < "$ENGINE_CONF"
}
load_engine_conf

if [ -n "${TG_RUNTIME:-}" ] && [ "$TG_RUNTIME" != "$ENGINE_DIR" ]; then
  say "  ! $ENGINE_CONF говорит, что движок в $TG_RUNTIME, а я запущен из $ENGINE_DIR."
  say "    Обновляю тот, из которого запущен. Если это не тот — запусти нужный update.sh."
fi
# Дальше по коду движок — это ENGINE_DIR (см. шапку), а не значение из конфига.
export TG_RUNTIME="$ENGINE_DIR"

# ── что можно сорсить от root ────────────────────────────────────────────────
# Второй слой защиты SEC-1: файл, который может переписать не только владелец (mode & 022),
# от root не исполняем. `find -perm` есть и в GNU, и в BSD — в отличие от флагов stat.
writable_by_others() {
  [ -n "$(find "$1" -maxdepth 0 \( -perm -0002 -o -perm -0020 \) -print 2>/dev/null)" ]
}
not_owned_by_root() {
  [ -n "$(find "$1" -maxdepth 0 ! -user root -print 2>/dev/null)" ]
}
guard_source() {
  local f="$1" d
  d="$(dirname "$f")"
  for p in "$f" "$d" "$ENGINE_DIR"; do
    if writable_by_others "$p"; then
      echo "✗ отказываюсь исполнять код движка: $p может править кто угодно (права на запись у группы/всех)." >&2
      echo "  Каталог движка обязан быть только для чтения всем, кроме root. Почини так:" >&2
      echo "    sudo chown -R root:root $ENGINE_DIR && sudo chmod -R go-w $ENGINE_DIR" >&2
      exit 3
    fi
    # Владельца проверяем только под root: под обычным юзером «не root» — норма (dev-хост).
    if [ "$(id -u)" -eq 0 ] && not_owned_by_root "$p"; then
      echo "✗ отказываюсь исполнять код движка от root: $p принадлежит не root'у." >&2
      echo "  Иначе владелец файла получил бы root даром. Почини так:" >&2
      echo "    chown -R root:root $ENGINE_DIR && chmod -R go-w $ENGINE_DIR" >&2
      exit 3
    fi
  done
}

# Пути инстансов и юзера движка резолвим кодом deploy/_common.sh — единый источник правды
# с deploy.sh/agentctl.sh (иначе бэкапили бы «не те» базы).
if [ -r "$ENGINE_DIR/deploy/_common.sh" ]; then
  guard_source "$ENGINE_DIR/deploy/_common.sh"
  # shellcheck source=deploy/_common.sh
  . "$ENGINE_DIR/deploy/_common.sh"
  resolve_run_identity
  resolve_uv_bin
else
  echo "✗ не нашёл $ENGINE_DIR/deploy/_common.sh — код движка неполный, переустанови install.sh" >&2
  exit 2
fi

# ── fail-closed: я обязан знать, чьи боты обновляю (C6/ADV-06) ───────────────
# Раньше запуск от root (как советовала же шпаргалка) давал RUN_USER=root → AGENTS_BASE=
# /root/agents → ни бэкапов, ни рестартов, и финальное «готово: v1 → v2». Молчаливый обман.
if [ "$RUN_USER" = "root" ]; then
  echo "✗ не знаю, под каким юзером бегут боты: не нашёл $ENGINE_CONF." >&2
  echo "  Этот файл пишет install.sh — он и есть карта путей движка." >&2
  echo "  Быстрая починка: переустанови движок (bash $ENGINE_DIR/install.sh — это безопасно," >&2
  echo "  повторный запуск = обновление), либо подскажи юзера руками:" >&2
  echo "    sudo TG_RUN_USER=<юзер движка> $ENGINE_DIR/update.sh" >&2
  exit 3
fi

# venv движка живёт ВНЕ дерева кода (Р1): само дерево root:root и go-w, писать в .venv туда
# было бы нельзя. Значение — контракт с deploy.sh и systemd-юнитом.
UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/var/lib/unpacker/venv}"
export UV_PROJECT_ENVIRONMENT

PREV_FILE="$ENGINE_DIR/.update-prev"

say "==> движок: $ENGINE_DIR (юзер ботов: $RUN_USER, инстансы: $AGENTS_BASE)"

# ── инстансы: сколько их и есть ли вообще юниты ──────────────────────────────
instance_names() {
  [ -d "$AGENTS_BASE" ] || return 0
  local inst
  for inst in "$AGENTS_BASE"/*/; do
    [ -d "$inst" ] || continue
    basename "$inst"
  done
}
units_exist() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl list-units --all --no-legend --plain 'agent-tg@*' 2>/dev/null | grep -q 'agent-tg@'
}

INSTANCES=()
while IFS= read -r n; do [ -n "$n" ] && INSTANCES+=("$n"); done < <(instance_names)

# Ноль инстансов при живых юнитах = я смотрю не туда. Это ошибка, а не «нечего делать»:
# иначе обновление «проходит» и печатает «готово», не тронув ни одного бота (C6).
if [ "${#INSTANCES[@]}" -eq 0 ] && units_exist; then
  echo "✗ в системе есть юниты agent-tg@*, но инстансов в $AGENTS_BASE нет — я смотрю не туда." >&2
  echo "  Скорее всего $ENGINE_CONF описывает другого юзера/каталог, чем тот, на котором стоят боты." >&2
  echo "  Проверь: systemctl list-units 'agent-tg@*' и grep . $ENGINE_CONF" >&2
  echo "  Обновляться в таком состоянии нельзя: боты остались бы на старом коде без бэкапов." >&2
  exit 3
fi

# ── бэкап баз сессий: согласованный снимок, иначе не обновляемся (ADV-12) ────
# `cp` живой WAL-базы даёт битую копию или копию без последних сессий, поэтому только
# честный снимок: sqlite3 .backup либо python stdlib. Не смогли — обновление не идёт.
backup_one() {  # <src.db> <dst.db>
  local src="$1" dst="$2"
  if have sqlite3 && sqlite3 "$src" ".backup '$dst'" 2>/dev/null; then return 0; fi
  if command -v python3 >/dev/null 2>&1 && python3 - "$src" "$dst" <<'PY' 2>/dev/null
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
d = sqlite3.connect(dst)
with d:
    s.backup(d)
d.close(); s.close()
PY
  then return 0; fi
  return 1
}

BACKUP_TAG=""
backup_dbs() {
  local ts inst db dst_dir name found="false"
  ts="$(date +%Y%m%d-%H%M%S)"
  BACKUP_TAG="$CURRENT-$ts"
  for name in ${INSTANCES+"${INSTANCES[@]}"}; do
    inst="$AGENTS_BASE/$name"
    for db in "$inst"/state/*.db; do
      [ -f "$db" ] || continue
      found="true"
      dst_dir="$inst/state/backups/$BACKUP_TAG"
      if [ "$DRY_RUN" = "true" ]; then
        echo "[dry-run] сделал бы согласованный бэкап $db → $dst_dir/$(basename "$db")"
        continue
      fi
      mkdir -p "$dst_dir"
      if ! backup_one "$db" "$dst_dir/$(basename "$db")"; then
        echo "✗ не смог сделать согласованный бэкап базы $db." >&2
        echo "  Копировать живую базу как файл нельзя: у SQLite свежие сессии лежат в -wal," >&2
        echo "  и копия вышла бы битой. Обновляться без бэкапа я не буду." >&2
        echo "  Починка: sudo apt-get install -y sqlite3   (или починить python3) и повторить." >&2
        exit 3
      fi
      chown "$RUN_USER" "$dst_dir/$(basename "$db")" 2>/dev/null || true
      say "    бэкап: $name/$(basename "$db") → state/backups/$BACKUP_TAG/"
    done
  done
  [ "$found" = "true" ] || say "  · баз сессий пока нет — бэкапить нечего (боты ещё не говорили)"
}

# ── восстановление баз из бэкапа (--restore-db) ─────────────────────────────
# --rollback возвращает КОД. Данные он не трогает — для них отдельная операция, иначе
# «откатился» превращалось в «откатил код, а база осталась после миграции новой версии».
restore_dbs() {
  local name inst latest src db restored=0
  for name in ${INSTANCES+"${INSTANCES[@]}"}; do
    [ -z "$RESTORE_ONLY" ] || [ "$RESTORE_ONLY" = "$name" ] || continue
    inst="$AGENTS_BASE/$name"
    # Самый свежий бэкап выбираем по метке времени в имени каталога (<релиз>-ГГГГММДД-ЧЧММСС),
    # а не по алфавиту: имя релиза само может содержать дефисы (v1.0.0-rc1).
    latest=""; local d b stamp best_stamp=""
    for d in "$inst"/state/backups/*/; do
      [ -d "$d" ] || continue
      b="$(basename "$d")"; stamp="${b: -15}"
      if [ -z "$latest" ] || [ "$stamp" \> "$best_stamp" ]; then latest="$b"; best_stamp="$stamp"; fi
    done
    if [ -z "$latest" ]; then
      say "  ! $name: бэкапов нет ($inst/state/backups) — восстанавливать нечего"
      continue
    fi
    say "  · $name: восстанавливаю из $latest"
    run $SUDO systemctl stop "agent-tg@$name" || say "    ! юнит agent-tg@$name не остановился — проверь сам"
    for src in "$inst/state/backups/$latest"/*.db; do
      [ -f "$src" ] || continue
      db="$inst/state/$(basename "$src")"
      if [ "$DRY_RUN" = "true" ]; then
        echo "[dry-run] вернул бы $src → $db (и удалил бы $db-wal/$db-shm)"
        continue
      fi
      cp "$src" "$db"
      rm -f "$db-wal" "$db-shm"
      chown "$RUN_USER" "$db" 2>/dev/null || true
      say "    вернул: state/$(basename "$src")"
      restored=$((restored+1))
    done
    run $SUDO systemctl start "agent-tg@$name" || say "    ! юнит agent-tg@$name не поднялся — проверь: systemctl status agent-tg@$name"
  done
  if [ "$DRY_RUN" = "true" ]; then
    say ""
    say "==> это был только план — ни одна база не тронута."
    exit 0
  fi
  if [ "$restored" -eq 0 ]; then
    echo "✗ ни одна база не восстановлена: бэкапов не нашлось." >&2
    echo "  Бэкапы делает сам update.sh перед обновлением: $AGENTS_BASE/<агент>/state/backups/" >&2
    exit 3
  fi
  say ""
  say "==> готово: восстановлено баз — $restored"
  exit 0
}

if [ "$RESTORE_DB" = "true" ]; then
  say "==> восстановление баз сессий из бэкапа"
  restore_dbs
fi

# ── теги и выбор релиза ─────────────────────────────────────────────────────
# Текущий релиз: тег, если HEAD ровно на теге, иначе короткий хеш (dev-состояние).
current_release() {
  git -C "$ENGINE_DIR" describe --tags --exact-match HEAD 2>/dev/null \
    || git -C "$ENGINE_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown"
}

# Отказ git — НЕ «в репо нет тегов» (C16). Раньше `|| true` превращал любую жалобу git
# (чаще всего dubious ownership на каталоге с чужим владельцем) в бодрое «обновлять не на что».
# Читаем теги ОДИН раз и на верхнем уровне: из `$(...)` никакой `exit` скрипт не остановит —
# он завершил бы только подоболочку, и отказ git снова стал бы «тегов нет».
TAGS=""
read_tags() {
  local st=0
  TAGS="$(git -C "$ENGINE_DIR" tag --sort=-v:refname 2>&1)" || st=$?
  if [ "$st" -ne 0 ]; then
    echo "✗ git не смог прочитать репо движка в $ENGINE_DIR. Он сказал так:" >&2
    printf '  %s\n' "$TAGS" >&2
    echo "  Обычная причина — каталог принадлежит не root'у (git: dubious ownership)." >&2
    echo "  Починка: sudo chown -R root:root $ENGINE_DIR && sudo chmod -R go-w $ENGINE_DIR" >&2
    exit 3
  fi
}

CURRENT="$(current_release)"
say "    сейчас: $CURRENT"

if ! run git -C "$ENGINE_DIR" fetch --tags --force; then
  say "  ! не смог получить свежие теги (сеть/доступ к репо) — работаю с тем, что уже скачано"
fi
read_tags

if [ "$ROLLBACK" = "true" ]; then
  TARGET=""
  [ -r "$PREV_FILE" ] && TARGET="$(tr -d '[:space:]' < "$PREV_FILE")"
  if [ -z "$TARGET" ]; then
    echo "✗ откат невозможен: я не знаю, с какого релиза ты обновлялся (нет $PREV_FILE)." >&2
    echo "  Посмотри доступные релизы:  git -C $ENGINE_DIR tag" >&2
    echo "  И встань на нужный руками:  sudo $ENGINE_DIR/update.sh --ref <тег>" >&2
    exit 2
  fi
  say "==> откат на предыдущий релиз: $TARGET"
elif [ -n "$REF" ]; then
  TARGET="$REF"
else
  TARGET="${TAGS%%$'\n'*}"
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

# Бэкап — ДО переключения кода: миграции идут при старте нового кода, и если что-то пойдёт не
# так, сессии ботов (переписка и resume) должны восстанавливаться из копии, а не «ну, бывает».
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

# uv sync — от root (Р1: дерево движка root:root, go-w), venv вынесен наружу через
# UV_PROJECT_ENVIRONMENT и после сборки отдаётся run-user'у: юнит бежит под ним.
# --frozen: не переписывать uv.lock, иначе рабочее дерево движка стало бы грязным и
# следующий checkout/деплой упёрся бы в гейт чистоты.
say "==> зависимости (venv: $UV_PROJECT_ENVIRONMENT)"
run mkdir -p "$(dirname "$UV_PROJECT_ENVIRONMENT")"
if ! run "$UV_BIN" sync --frozen --project "$ENGINE_DIR"; then
  echo "✗ зависимости не встали: uv sync провалился." >&2
  echo "  Код уже переключён на $TARGET, но библиотеки старые — боты на нём не поднимутся." >&2
  echo "  Боты я НЕ рестартовал: они пока живут на прежнем коде в памяти." >&2
  echo "  Вернись на прошлый релиз одной командой:" >&2
  echo "    sudo $ENGINE_DIR/update.sh --rollback     (вернёт $CURRENT)" >&2
  echo "  Частая причина — нет сети до pypi. Проверь: curl -I https://pypi.org" >&2
  exit 3
fi
if [ -d "$UV_PROJECT_ENVIRONMENT" ]; then
  run chown -R "$RUN_USER" "$UV_PROJECT_ENVIRONMENT" || \
    say "  ! не смог отдать venv юзеру $RUN_USER — проверь: chown -R $RUN_USER $UV_PROJECT_ENVIRONMENT"
fi

# ── рестарт ботов: инициатор ПОСЛЕДНИМ ──────────────────────────────────────
RESTART_FAILED=0
restart_one() {
  local name="$1"
  if run $SUDO systemctl restart "agent-tg@$name"; then
    say "    рестарт: $name"
  else
    RESTART_FAILED=$((RESTART_FAILED+1))
    say "  ! юнит agent-tg@$name не рестартовался. Посмотри причину:"
    say "      systemctl status agent-tg@$name   и   journalctl -u agent-tg@$name -n 50 --no-pager"
  fi
}
restart_agents() {
  if ! command -v systemctl >/dev/null 2>&1; then
    say "  ! systemctl не найден (dev-хост) — рестарт ботов пропущен"
    return 0
  fi
  local name last=""
  for name in ${INSTANCES+"${INSTANCES[@]}"}; do
    if [ "$name" = "$INITIATOR" ]; then last="$name"; else restart_one "$name"; fi
  done
  if [ -n "$last" ]; then
    say "    (инициатор — последним: он же ведёт обновление из чата)"
    restart_one "$last"
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
if [ "$RESTART_FAILED" -gt 0 ]; then
  echo "✗ код обновлён ($CURRENT → $TARGET), но ботов не поднялось: $RESTART_FAILED" >&2
  echo "  Пока это не починено, обновление НЕ завершено — «готово» я не напишу." >&2
  echo "  Не разбираешься — вернись на прошлый релиз: sudo $ENGINE_DIR/update.sh --rollback" >&2
  exit 3
fi
say "==> готово: $CURRENT → $TARGET"
say "    не понравилось?      sudo $ENGINE_DIR/update.sh --rollback      (вернёт код $CURRENT)"
say "    базы сессий назад    sudo $ENGINE_DIR/update.sh --restore-db    (из бэкапа $BACKUP_TAG)"
say "    бэкапы баз сессий    $AGENTS_BASE/<агент>/state/backups/"
say "    проверить бота       sudo $ENGINE_DIR/deploy/agentctl.sh doctor $INITIATOR"
