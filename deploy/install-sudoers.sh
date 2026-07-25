#!/usr/bin/env bash
# deploy/install-sudoers.sh — поставить sudoers-whitelist мета-агента «Распаковщик» (§7.5).
#
# Отдельный шаг, а не часть deploy.sh, по двум причинам: (1) это ОДНОРАЗОВАЯ операция уровня
# машины, а деплой агентов — рутина; (2) провал прав не должен ронять уже живого бота.
# deploy.sh --role unpacker вызывает этот скрипт сам и громко печатает команду для ручного
# повтора, если что-то не сошлось.
#
# МОДЕЛЬ ВЛАДЕНИЯ (Р1, §7.5): каталог движка принадлежит root:root и не доступен на запись
# ни run-user'у, ни группе, ни прочим. Иначе whitelist бессмыслен: агент перепишет скрипт,
# на который выдан NOPASSWD, и получит root в одну строку (SEC-2). venv движка живёт СНАРУЖИ
# дерева (UV_PROJECT_ENVIRONMENT=/var/lib/unpacker/venv) и принадлежит run-user'у — поэтому
# «отдать движок root» больше ничего не ломает.
#
# Использование:
#   install-sudoers.sh [--dry-run] [--dev-owner-ok]
#
# Env:
#   TG_RUNTIME            — где лежит движок (дефолт /opt/unpacker); из его путей строится whitelist
#   TG_RUN_USER           — юзер, от которого бежит Распаковщик (кому выдаём права)
#   UNPACKER_SUDOERS_DIR  — куда ставить (дефолт /etc/sudoers.d); переопределяется в тестах
#   UNPACKER_SUDOERS_NAME — имя файла (дефолт unpacker; точек в имени быть не должно — sudo их игнорит)
#   UNPACKER_VISUDO       — путь до visudo, если он не в PATH
#   UNPACKER_DEV_OWNER_OK — то же, что --dev-owner-ok (нужен, когда скрипт зовёт deploy.sh).
#                           Через `sudo` от агента НЕ проходит: env_reset его стирает.
#
# Идемпотентность: если целевой файл уже совпадает с рендером — ничего не пишем и не трогаем mtime.
# КОДЫ ВОЗВРАТА: 2 — плохие входные данные; 3 — проверка безопасности не пройдена.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/_common.sh
. "$HERE/_common.sh"
resolve_run_identity

DRY_RUN="false"
# dev-режим (дерево движка принадлежит run-user'у) — ТОЛЬКО явным флагом. Раньше эта ветка
# была предупреждением «продолжаю (dev-режим)», и продукт из коробки жил именно в ней.
DEV_OWNER_OK="false"; [ -z "${UNPACKER_DEV_OWNER_OK:-}" ] || DEV_OWNER_OK="true"
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN="true"; shift ;;
    --dev-owner-ok) DEV_OWNER_OK="true"; shift ;;
    -h|--help) awk 'NR==1{next} /^set -euo pipefail/{exit} {sub(/^# ?/,"");print}' "$0"; exit 0 ;;
    *) echo "Неизвестный флаг: $1" >&2; exit 2 ;;
  esac
done

# RUNTIME пришёл из _common.sh (env → /etc/unpacker/engine.conf → /opt/unpacker) — Р2.
SUDOERS_DIR="${UNPACKER_SUDOERS_DIR:-/etc/sudoers.d}"
SUDOERS_NAME="${UNPACKER_SUDOERS_NAME:-unpacker}"
TARGET="$SUDOERS_DIR/$SUDOERS_NAME"
TPL="$HERE/sudoers.d/unpacker"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

fail() { echo "✗ $1" >&2; echo "==> sudoers НЕ установлен (§7.5): права выдаём только на безопасной базе." >&2; exit 3; }

echo "==> sudoers-whitelist Распаковщика (§7.5): run-user=$RUN_USER, движок=$RUNTIME"

[ -f "$TPL" ] || fail "нет шаблона $TPL — репо неполное."
[ "$RUN_USER" != "root" ] || fail "run-user = root: ему whitelist не нужен, а Распаковщик под root
      запрещён (bypassPermissions). Заведи непривилегированного юзера и передай TG_RUN_USER."
# Имя юзера втекает в файл sudoers → пускаем только безопасный набор символов.
printf '%s' "$RUN_USER" | grep -Eq '^[a-z_][a-z0-9_-]*$' \
  || { echo "TG_RUN_USER '$RUN_USER': недопустимое имя юзера" >&2; exit 2; }
case "$RUNTIME" in /*) ;; *) echo "TG_RUNTIME '$RUNTIME' должен быть абсолютным путём" >&2; exit 2 ;; esac
safe_arg "$RUNTIME" || { echo "TG_RUNTIME '$RUNTIME': недопустимые символы" >&2; exit 2; }
case "$RUNTIME" in *'*'*|*..*) echo "TG_RUNTIME '$RUNTIME': ни wildcard, ни .. в пути движка" >&2; exit 2 ;; esac

file_mode()  { local m; m="$(stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1" 2>/dev/null)"; printf '%s' "${m: -3}"; }
file_owner() { stat -c '%U' "$1" 2>/dev/null || stat -f '%Su' "$1" 2>/dev/null; }

# Ключевая проверка §7.5: whitelist имеет смысл только если run-user НЕ может подменить скрипт,
# который мы разрешаем запускать от root. Иначе это не whitelist, а полный root в одну строку.
#
# Владелец == run-user — это ОТКАЗ, а не предупреждение: право записи владелец возвращает себе
# одним `chmod u+w`, так что mode 0555 его не держит. Единственный обход — явный --dev-owner-ok
# (стенд разработчика), и он громко называет себя dev-режимом.
FIX_HINT="Отдай движок root:
        sudo chown -R root:root '$RUNTIME' && sudo chmod -R go-w '$RUNTIME'
      venv движка при этом живёт снаружи дерева: UV_PROJECT_ENVIRONMENT=/var/lib/unpacker/venv"

owner_is_run_user() {  # owner_is_run_user <что это> <путь>
  if [ "$DEV_OWNER_OK" = "true" ]; then
    echo "  ! $1 $2 принадлежит $RUN_USER — на боевом VPS так нельзя."
    echo "    Продолжаю: явно передан --dev-owner-ok (dev-режим, не для учеников)."
    return 0
  fi
  fail "$1 $2 принадлежит run-user'у $RUN_USER: право записи он вернёт себе chmod'ом,
      значит NOPASSWD на наши скрипты = полный root в одну строку (SEC-2). $FIX_HINT"
}

check_mode_go_w() {  # check_mode_go_w <что это> <путь> <mode>
  case "$3" in
    ??[2367]) fail "$1 $2 доступен на запись «прочим» (mode $3) → chmod o-w '$2'" ;;
  esac
  case "$3" in
    ?[2367]?) fail "$1 $2 доступен на запись группе (mode $3) → chmod g-w '$2'" ;;
  esac
}

# Каталог тоже важен: право записи в каталог = подмена скрипта переименованием, даже
# если сам файл 0555 и принадлежит root.
check_dir() {
  local d="$1" mode owner
  [ -d "$d" ] || fail "нет каталога $d — проверь TG_RUNTIME."
  mode="$(file_mode "$d")"; owner="$(file_owner "$d")"
  check_mode_go_w "каталог" "$d" "$mode"
  if [ "$owner" = "$RUN_USER" ]; then owner_is_run_user "каталог" "$d"
  else echo "  ✓ каталог $d (mode $mode, владелец $owner)"; fi
}

check_script() {
  local f="$1" mode owner
  # SEC-3: NOPASSWD на несуществующий путь — это разрешение на файл, который кто-то создаст
  # позже. Все три скрипта в релизе есть всегда, значит отсутствие = битое дерево.
  [ -f "$f" ] || fail "нет $f — не на что выдавать права (NOPASSWD на несуществующий путь
      я не выдаю). Проверь TG_RUNTIME или переустанови движок."
  mode="$(file_mode "$f")"; owner="$(file_owner "$f")"
  check_mode_go_w "скрипт" "$f" "$mode"
  if [ "$owner" = "$RUN_USER" ]; then owner_is_run_user "скрипт" "$f"
  else echo "  ✓ $f (mode $mode, владелец $owner)"; fi
}

check_dir "$RUNTIME"
check_dir "$RUNTIME/deploy"
check_script "$RUNTIME/deploy/deploy.sh"
check_script "$RUNTIME/deploy/agentctl.sh"
check_script "$RUNTIME/update.sh"

# Рендер во временный файл: в целевой пишем только то, что прошло visudo.
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
sed -e "s|REPLACE_WITH_LINUX_USER|$RUN_USER|g" \
    -e "s|REPLACE_WITH_RUNTIME_PATH|$RUNTIME|g" \
    "$TPL" > "$TMP"

VISUDO="${UNPACKER_VISUDO:-}"
if [ -z "$VISUDO" ]; then
  VISUDO="$(command -v visudo 2>/dev/null || true)"
  [ -n "$VISUDO" ] || { [ -x /usr/sbin/visudo ] && VISUDO="/usr/sbin/visudo"; }
fi
[ -n "$VISUDO" ] && [ -x "$VISUDO" ] || fail "не нашёл visudo — непроверенный sudoers я не поставлю:
      файл с синтаксической ошибкой ломает sudo целиком (обратно уже не починить без консоли).
      Поставь sudo-пакет или укажи UNPACKER_VISUDO=<путь до visudo>."
"$VISUDO" -cf "$TMP" >/dev/null 2>&1 \
  || fail "visudo забраковал рендер шаблона — файл НЕ установлен. Проверь: $VISUDO -cf <рендер>"
echo "  ✓ visudo -cf: синтаксис в порядке"

if [ -f "$TARGET" ] && cmp -s "$TMP" "$TARGET"; then
  echo "    $TARGET уже актуален — не трогаю (idempotent)"
  exit 0
fi

if [ "$DRY_RUN" = "true" ]; then
  echo "[dry-run] поставил бы $TARGET (mode 0440, владелец root) со whitelist на:"
  echo "[dry-run]   $RUNTIME/deploy/deploy.sh · $RUNTIME/update.sh · $RUNTIME/deploy/agentctl.sh"
  echo "[dry-run]   systemctl start|stop|restart|status $(unit_template | sed 's/\.service$/*/')"
  exit 0
fi

if [ -d "$SUDOERS_DIR" ]; then :
elif mkdir -p "$SUDOERS_DIR" 2>/dev/null; then :
else $SUDO mkdir -p "$SUDOERS_DIR"; fi

if [ "$(id -u)" -eq 0 ]; then
  install -m 0440 -o root -g root "$TMP" "$TARGET"
elif [ -w "$SUDOERS_DIR" ]; then
  install -m 0440 "$TMP" "$TARGET"
  echo "  ! файл принадлежит $(id -un), не root — на боевом VPS ставь этот шаг под sudo/root"
else
  $SUDO install -m 0440 -o root -g root "$TMP" "$TARGET"
fi
echo "    $TARGET установлен (mode 0440). Полный sudo НЕ выдан — только перечисленные команды."
