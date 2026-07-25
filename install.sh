#!/usr/bin/env bash
# install.sh — поставить движок «Распаковщик» на свежий VPS ОДНОЙ командой (§10 конституции).
#
# Для кого: у тебя есть VPS (Ubuntu 22+/24), подписка Claude Pro/Max и бот из @BotFather.
# Опыт в Linux не нужен: каждая непройденная проверка печатает не «ошибку», а инструкцию
# «сделай вот это».
#
# Запуск (от root на свежем сервере — это штатный путь, он проверен тестами):
#   bash install.sh
#
# Идемпотентно: повторный запуск — обновление, а не поломка. Ответы (кроме секретов)
# запоминаются в /etc/unpacker/install.conf, второй раз их не спрашивают.
#
# Флаги:
#   --dry-run              показать план, ничего не менять
#   --non-interactive      не задавать вопросов, ответы взять из переменных окружения:
#                          UNPACKER_BOT_TOKEN (или UNPACKER_BOT_TOKEN_FILE),
#                          UNPACKER_ALLOWED_USERS, UNPACKER_BRAINS_DIR,
#                          UNPACKER_CC_TOKEN (или UNPACKER_CC_TOKEN_FILE)
#   --engine-dir <путь>    куда положить код движка (по умолчанию /opt/unpacker)
#   --run-user <имя>       непривилегированный юзер движка (по умолчанию unpacker)
#   --name <slug>          имя первого агента-Распаковщика (по умолчанию unpacker)
#   --repo <git-url>       откуда брать код (приватный репо курса)
#   --ref <тег|ветка>      что выкатить (по умолчанию — последний git-тег, НЕ HEAD)
#   --ram-mb <N>           переопределить измеренную RAM (если детект врёт)
#   --min-disk-mb <N>      минимум свободного места, МБ (по умолчанию 3000)
#   --no-ufw               не настраивать файрвол
#   --no-ssh-hardening     не выключать вход по паролю в ssh
#   --ssh-keys <файл>      где искать твой ssh-ключ (по умолчанию ~/.ssh/authorized_keys)
#   --no-unattended        не включать автообновления безопасности
#   --no-hardening         отказаться от всего шага 0 сразу
#   -h, --help             эта справка
#
# СЕКРЕТЫ. Токен бота вводится БЕЗ эха и не попадает ни в вывод, ни в history, ни в argv:
# до deploy.sh он доезжает ФАЙЛОМ с правами 600, который удаляется сразу после (аргументы
# любой команды под sudo видны всей машине и остаются в /var/log/auth.log навсегда).
#
# ПРАВА. Каталог движка принадлежит root:root и не писуем никем больше: на скрипты внутри
# выдан NOPASSWD-sudo мета-агенту, и право переписать их = root даром. Писать движку можно
# только в venv (вынесен наружу, UV_PROJECT_ENVIRONMENT) и в свои инстансы.
#
# Рычаги для тестов и нестандартных установок (в норме не нужны):
#   UNPACKER_ETC=/etc/unpacker                     где лежат машинный конфиг и ответы
#   UV_PROJECT_ENVIRONMENT=/var/lib/unpacker/venv  где живёт venv движка (ВНЕ дерева кода)
#   APT_LOCK_WAIT_SEC=180                          сколько ждать занятый пакетный менеджер
set -euo pipefail

# ── значения по умолчанию ────────────────────────────────────────────────────
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF_PATH="$SELF_DIR/$(basename "${BASH_SOURCE[0]}")"
DRY_RUN="false" NON_INTERACTIVE="false"
ENGINE_DIR="/opt/unpacker" RUN_USER="unpacker" AGENT_NAME="unpacker"
REPO_URL="${UNPACKER_REPO:-https://github.com/rusanovproject-dotcom/unpacker.git}"
REF="" RAM_MB="" MIN_DISK_MB="3000" SSH_KEYS=""
DO_UFW="true" DO_SSH="true" DO_UNATTENDED="true"
# uv ставим системно: он нужен И root'у (uv sync), И юзеру движка (юнит зовёт его по
# абсолютному пути). Домашний каталог root'а — mode 700, оттуда юнит его не видит (C4/ADV-01).
UV_DIR="/usr/local/bin" UV_BIN=""
ETC_DIR="${UNPACKER_ETC:-/etc/unpacker}"
ENGINE_CONF="$ETC_DIR/engine.conf"
INSTALL_CONF="$ETC_DIR/install.conf"
APT_LOCK_WAIT_SEC="${APT_LOCK_WAIT_SEC:-180}"

usage() { awk 'NR==1{next} /^set -euo pipefail/{exit} {sub(/^# ?/,"");print}' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)          DRY_RUN="true"; shift ;;
    --non-interactive)  NON_INTERACTIVE="true"; shift ;;
    --engine-dir)       ENGINE_DIR="$2"; shift 2 ;;
    --run-user)         RUN_USER="$2"; shift 2 ;;
    --name)             AGENT_NAME="$2"; shift 2 ;;
    --repo)             REPO_URL="$2"; shift 2 ;;
    --ref)              REF="$2"; shift 2 ;;
    --ram-mb)           RAM_MB="$2"; shift 2 ;;
    --min-disk-mb)      MIN_DISK_MB="$2"; shift 2 ;;
    --no-ufw)           DO_UFW="false"; shift ;;
    --no-ssh-hardening) DO_SSH="false"; shift ;;
    --ssh-keys)         SSH_KEYS="$2"; shift 2 ;;
    --no-unattended)    DO_UNATTENDED="false"; shift ;;
    --no-hardening)     DO_UFW="false"; DO_SSH="false"; DO_UNATTENDED="false"; shift ;;
    -h|--help)          usage 0 ;;
    *) echo "Неизвестный флаг: $1 (см. --help)" >&2; exit 2 ;;
  esac
done

# venv движка — ВНЕ дерева кода: дерево обязано быть неизменяемым (см. шапку), а uv пишет
# в окружение. Значение — контракт с deploy.sh, update.sh и systemd-юнитом.
UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/var/lib/unpacker/venv}"
export UV_PROJECT_ENVIRONMENT

# «sudo» в ПЕЧАТАЕМЫХ командах — всегда буквальное слово: ученик копирует строку целиком.
SUDO="sudo"
say()   { printf '%s\n' "$1"; }

# Выполнить от root. От root — напрямую; иначе через sudo. Никаких «$SUDO -u»: при запуске
# от root переменная пуста, и `-u` становится КОМАНДОЙ («-u: command not found»), а `set -e`
# убивает установку — ровно на документированном пути «ssh root@IP → bash install.sh» (C1).
run_root() {
  if [ "$DRY_RUN" = "true" ]; then echo "[dry-run] $*"
  elif [ "$(id -u)" -eq 0 ]; then "$@"
  else sudo "$@"; fi
}

# Команда от имени юзера движка. Через `bash -lc`: per-user claude лежит в ~/.local/bin,
# а он есть только в login-PATH. `sudo` — литеральный (та же грабля C1).
asuser() {
  if [ "$DRY_RUN" = "true" ]; then echo "[dry-run] (as $RUN_USER) $*"
  elif [ "$RUN_USER" = "$(id -un)" ]; then bash -lc "$*"
  else sudo -u "$RUN_USER" -H bash -lc "$*"; fi
}

# asuser_real: как asuser, но ИСПОЛНЯЕТ даже в --dry-run. Только для ЧТЕНИЯ (проверки):
# «есть ли claude у юзера движка» надо знать и в плане, иначе план обещает живого бота.
asuser_real() {
  if [ "$RUN_USER" = "$(id -un)" ]; then bash -lc "$*"
  else sudo -u "$RUN_USER" -H bash -lc "$*"; fi
}

# Файл под root'ом (конфиги в /etc). Отдельно от run_root(), чтобы содержимое не текло в argv.
# Создаём сразу с закрытыми правами (umask 077) и только потом расширяем до нужного режима:
# иначе секретный файл на мгновение существовал бы с правами по умолчанию.
write_root_file() {  # <путь> <содержимое> [режим, по умолчанию 0644]
  local path="$1" body="$2" mode="${3:-0644}"
  if [ "$DRY_RUN" = "true" ]; then echo "[dry-run] записал бы $path"; return 0; fi
  if [ -w "$(dirname "$path")" ] || [ "$(id -u)" -eq 0 ]; then
    (umask 077; printf '%s' "$body" > "$path")
  else
    printf '%s' "$body" | sudo tee "$path" >/dev/null
  fi
  # Права применяем best-effort: на пути «sudo tee» файл мог не создаться (нет прав/каталога),
  # и падать здесь под set -e значило бы обрывать установку из-за косметики.
  run_root chmod "$mode" "$path" 2>/dev/null || true
}

# ── run-user: root запрещён БЕЗУСЛОВНО ──────────────────────────────────────
# Claude CLI отказывается работать с bypassPermissions под root, а движок бежит именно с ним:
# под root получился бы бот-зомби, молчащий на все сообщения. Та же грабля описана в Makefile
# (канарейки) и в deploy.sh — здесь она встречает ученика ДО установки.
if [ "$RUN_USER" = "root" ]; then
  echo "✗ движок нельзя запускать от root: Claude CLI запрещает bypassPermissions под root," >&2
  echo "  и бот молчал бы на все сообщения. Нужен непривилегированный юзер." >&2
  echo "  Ничего делать не надо — install.sh создаст его сам. Просто убери --run-user root" >&2
  echo "  (по умолчанию будет юзер 'unpacker')." >&2
  exit 3
fi

# Блокеры собираем и печатаем ПАЧКОЙ: новичку нужен весь список дел сразу, а не
# «починил одно — узнал про второе» пять раз подряд.
BLOCKERS=()
block() { BLOCKERS+=("$1"); }

# Печать накопленных блокеров списком дел и выход. Формулировка — «сделай вот это»,
# а не «ошибка»: ученик не должен догадываться, что от него хотят.
flush_blockers() {
  [ "${#BLOCKERS[@]}" -gt 0 ] || return 0
  say ""
  say "==> установка остановлена: сначала сделай вот это (${#BLOCKERS[@]} пункт(а)):"
  local i=1
  for b in "${BLOCKERS[@]}"; do say "  $i) $b"; i=$((i+1)); done
  say ""
  say "    Починил — запусти install.sh снова, он продолжит с этого места (это безопасно)."
  exit 3
}

# ── секреты: только файлом, никогда в argv (SEC-4) ──────────────────────────
SECRET_DIR=""
cleanup_secrets() { [ -z "$SECRET_DIR" ] || rm -rf "$SECRET_DIR"; }
# Обрыв ssh посреди установки не должен оставлять токен в /tmp.
trap cleanup_secrets EXIT INT TERM

# Каталог создаём ОТДЕЛЬНОЙ функцией: secret_file зовут из `$(...)`, то есть в подоболочке,
# и присваивание SECRET_DIR там не дожило бы до родителя — файл с токеном остался бы в /tmp.
ensure_secret_dir() {
  [ -z "$SECRET_DIR" ] || return 0
  SECRET_DIR="$(mktemp -d "${TMPDIR:-/tmp}/unpacker-secret.XXXXXX")"
  chmod 700 "$SECRET_DIR"
}

secret_file() {  # <имя> <значение> → путь к файлу 600
  local f="$SECRET_DIR/$1"
  (umask 077; printf '%s' "$2" > "$f")
  printf '%s' "$f"
}

read_secret_env() {  # <VAR> <VAR_FILE> → печатает значение
  local direct="$1" from_file="$2"
  if [ -n "$direct" ]; then printf '%s' "$direct"; return 0; fi
  [ -n "$from_file" ] || return 0
  if [ ! -r "$from_file" ]; then
    echo "✗ не могу прочитать файл с секретом: $from_file" >&2
    echo "  Проверь путь и права (файл должен быть доступен тому, кто запускает install.sh)." >&2
    exit 2
  fi
  tr -d '[:space:]' < "$from_file"
}

# ── apt: обновить индексы, дождаться лока, объяснить отказ (C15/ADV-04) ──────
APT_OK="false"
apt-get --version >/dev/null 2>&1 && APT_OK="true"
APT_UPDATED="false"

# Инструмент есть и запускается? (`command -v` недостаточно: битый бинарь тоже находится.)
have() { "$1" --version >/dev/null 2>&1; }

dpkg_locked() {
  command -v fuser >/dev/null 2>&1 || return 1
  fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || fuser /var/lib/dpkg/lock >/dev/null 2>&1
}

# На свежем VPS первые минуты пакетами занят cloud-init: без ожидания apt падает сырой
# ошибкой «Could not get lock» ровно там, где ученик ничего не сделал не так.
wait_dpkg_lock() {
  [ "$DRY_RUN" != "true" ] || return 0
  dpkg_locked || return 0
  say "  · пакетный менеджер занят (обычно это cloud-init на свежем сервере) — жду до ${APT_LOCK_WAIT_SEC}с"
  local waited=0
  while dpkg_locked; do
    if [ "$waited" -ge "$APT_LOCK_WAIT_SEC" ]; then
      block "пакетный менеджер всё ещё занят (${APT_LOCK_WAIT_SEC}с ожидания): dpkg-lock держит другой процесс — на свежем сервере это cloud-init или unattended-upgrades. Подожди 2–3 минуты и запусти install.sh снова. Посмотреть, кто держит: $SUDO fuser -v /var/lib/dpkg/lock-frontend"
      return 1
    fi
    sleep 2; waited=$((waited+2))
  done
  say "    пакетный менеджер освободился"
}

apt_update_once() {
  [ "$APT_UPDATED" = "false" ] || return 0
  wait_dpkg_lock || return 1
  APT_UPDATED="true"
  if ! run_root apt-get update; then
    block "не смог обновить списки пакетов ($SUDO apt-get update). Обычно это сеть или зеркала. Проверь: ping -c1 archive.ubuntu.com — и запусти install.sh снова."
    return 1
  fi
}

apt_install() {  # <пакет…>
  apt_update_once || return 1
  wait_dpkg_lock || return 1
  run_root apt-get install -y "$@"
}

# ── шаг 1: проверки среды (§10.1) ───────────────────────────────────────────
detect_ram_mb() {
  if [ -r /proc/meminfo ]; then
    awk '/^MemTotal:/{printf "%d", $2/1024}' /proc/meminfo
  elif command -v sysctl >/dev/null 2>&1 && sysctl -n hw.memsize >/dev/null 2>&1; then
    echo $(( $(sysctl -n hw.memsize) / 1024 / 1024 ))
  else
    echo 0
  fi
}

# Потолок тёплого пула считает САМ ДВИЖОК (engine/core/pool.py: compute_pool_ceiling).
# Второй копии формулы в bash нет сознательно: копии уже разошлись по семантике, и ученику
# обещали одно, а движок поднимал другое (M-11). Печатаем "<потолок> <минимум RAM, МБ>".
pool_numbers() {  # <ram_mb>
  local src=""
  if   [ -f "$SELF_DIR/engine/core/pool.py" ];   then src="$SELF_DIR"
  elif [ -f "$ENGINE_DIR/engine/core/pool.py" ]; then src="$ENGINE_DIR"
  else return 1; fi
  command -v python3 >/dev/null 2>&1 || return 1
  python3 - "$src" "$1" <<'PY' 2>/dev/null
import sys

sys.path.insert(0, sys.argv[1])
from engine.core.pool import _RAM_PER_AGENT_GB as PER
from engine.core.pool import _RAM_RESERVE_GB as RES
from engine.core.pool import compute_pool_ceiling

ram_mb = int(sys.argv[2])
print(compute_pool_ceiling(ram_mb * 1024**2), int((RES + PER) * 1024))
PY
}

# Нужный инструмент: доставить apt'ом либо выдать блокер с готовой командой.
need_tool() {
  local cmd="$1" pkg="$2"
  have "$cmd" && { say "  ✓ $cmd"; return 0; }
  if [ "$APT_OK" = "true" ]; then
    say "  · $cmd отсутствует — ставлю пакетом $pkg"
    if ! apt_install "$pkg"; then
      block "не смог поставить $cmd (пакет $pkg). Сделай сам и запусти install.sh снова: $SUDO apt-get update && $SUDO apt-get install -y $pkg. Если apt пишет «Unable to locate package» — проверь, что в /etc/apt/sources.list подключён репозиторий universe."
      return 1
    fi
    return 0
  fi
  block "$cmd не найден. Поставь: $SUDO apt-get update && $SUDO apt-get install -y $pkg"
}

check_env() {
  say "==> шаг 1: проверки среды"

  [ "$(uname -s)" = "Linux" ] || say "  ! это не Linux — install.sh рассчитан на Ubuntu 22+/24 (здесь только --dry-run)"

  # RAM → потолок тёплого пула (числа берём у движка, см. pool_numbers)
  [ -n "$RAM_MB" ] || RAM_MB="$(detect_ram_mb)"
  if ! printf '%s' "$RAM_MB" | grep -Eq '^[0-9]+$' || [ "$RAM_MB" -le 0 ]; then
    block "не смог измерить RAM. Задай вручную: --ram-mb <сколько ГБ×1024>"
  else
    local nums ceiling min_ram
    if nums="$(pool_numbers "$RAM_MB")" && [ -n "$nums" ]; then
      ceiling="${nums%% *}"; min_ram="${nums##* }"
      if [ "$RAM_MB" -lt "$min_ram" ]; then
        block "RAM ${RAM_MB} МБ мало: движку нужно ~1 ГБ на активного агента плюс резерв серверу, минимум ${min_ram} МБ. Возьми VPS с 4 ГБ RAM и повтори."
      else
        say "  ✓ RAM ${RAM_MB} МБ → потолок тёплого пула: ${ceiling} активных агент(ов) одновременно"
        [ "$RAM_MB" -ge 4096 ] || say "  ! меньше 4 ГБ — рекомендованный минимум 4 ГБ (один агент будет впритык)"
      fi
    else
      say "  ! не смог посчитать потолок тёплого пула (нет рабочего python3 или кода движка рядом)"
      say "      — посчитаю его на боевом прогоне, движку это не мешает"
    fi
  fi

  # Диск: ставим python-окружение и SDK — на забитом диске установка умрёт на середине
  local probe free
  probe="$ENGINE_DIR"; while [ ! -d "$probe" ] && [ "$probe" != "/" ]; do probe="$(dirname "$probe")"; done
  free="$(df -Pm "$probe" 2>/dev/null | awk 'NR==2{print $4}')"
  if [ -z "$free" ]; then
    say "  ! не смог измерить свободное место на диске ($probe) — проверь сам: df -h"
  elif [ "$free" -lt "$MIN_DISK_MB" ]; then
    block "мало места на диске: свободно ${free} МБ, нужно ${MIN_DISK_MB} МБ. Почисти диск (du -sh /var/* | sort -h) или возьми диск побольше."
  else
    say "  ✓ диск: свободно ${free} МБ"
  fi

  # python: гейт «3.11+» закрывает uv (несёт свой интерпретатор), поэтому старый
  # системный python — предупреждение, а не блокер. Ubuntu 22.04 = python 3.10.
  # Сравниваем major/minor ЦЕЛЫМИ: как дробь «3.9 < 3.11» ложно (3.9 > 3.11) — классическая
  # ловушка версий, из-за которой проверка молча пропускала бы старый интерпретатор.
  local pv maj min
  pv="$(python3 --version 2>/dev/null | awk '{print $2}')"
  maj="$(printf '%s' "$pv" | cut -d. -f1)"; min="$(printf '%s' "$pv" | cut -d. -f2)"
  if [ -z "$pv" ]; then
    say "  ! системный python3 не найден — не страшно: движок побежит на python 3.11+ от uv"
  elif [ "${maj:-0}" -lt 3 ] || { [ "${maj:-0}" -eq 3 ] && [ "${min:-0}" -lt 11 ]; }; then
    say "  ! системный python $maj.$min старее 3.11 — не страшно: движок побежит на python 3.11+ от uv"
  else
    say "  ✓ python $maj.$min"
  fi

  need_tool git git || true
  need_tool curl curl || true
  need_tool tmux tmux || true

  flush_blockers
  say "    среда готова"
}

# ── шаг 0: мини-hardening (§10.0) ───────────────────────────────────────────
# Дёшево снимает главные риски целевой аудитории (VPS с паролем наружу). Каждый пункт
# можно отклонить флагом: сервер чужой, навязывать нельзя.

# Реальные порты ssh — из эффективного конфига sshd. `ufw allow OpenSSH` открывает РОВНО
# 22/tcp: если sshd слушает другой порт, текущая сессия выживает (она уже ESTABLISHED), а
# следующий вход невозможен — сервер потерян (ADV-15).
ssh_ports() {
  local sshd_bin=""
  sshd_bin="$(command -v sshd 2>/dev/null || true)"
  [ -n "$sshd_bin" ] || { [ -x /usr/sbin/sshd ] && sshd_bin="/usr/sbin/sshd"; }
  [ -n "$sshd_bin" ] || return 0
  "$sshd_bin" -T 2>/dev/null | awk '$1=="port"{print $2}' | grep -E '^[0-9]+$' || true
}

harden() {
  say "==> шаг 0: базовая защита сервера"

  if [ "$DO_UFW" = "true" ]; then
    if have ufw; then
      local ports p
      ports="$(ssh_ports)"
      if [ -z "$ports" ]; then
        say "  ! не смог определить порт(ы) ssh (спрашивал у sshd -T) — файрвол НЕ включаю."
        say "      Иначе политика «запрещено всё» отрезала бы твой следующий вход, а это"
        say "      потеря сервера: текущая сессия живёт, а войти заново уже нельзя."
        say "      Сделай сам, подставив свой порт (посмотреть: grep -i '^port' /etc/ssh/sshd_config):"
        say "        $SUDO ufw allow <порт>/tcp && $SUDO ufw default deny incoming && $SUDO ufw --force enable"
      else
        # Порядок важен: сначала разрешаем ssh, только потом включаем deny-политику —
        # иначе enable отрезает текущую сессию вместе с доступом к серверу.
        for p in $ports; do run_root ufw allow "$p/tcp"; done
        run_root ufw default deny incoming
        run_root ufw default allow outgoing
        run_root ufw --force enable
        say "    файрвол: снаружи открыт только ssh, порт(ы) $(printf '%s' "$ports" | tr '\n' ' ')"
        say "    (веб-порты откроются в Фазе 3, когда будет веб)"
      fi
    else
      say "  ! ufw не установлен — файрвол не настроен. Поставь: $SUDO apt-get install -y ufw"
    fi
  else
    say "  · файрвол — пропущен по твоему решению"
  fi

  if [ "$DO_SSH" = "true" ]; then
    # Выключать вход по паролю МОЖНО только если ssh-ключ уже работает. Иначе ученик
    # запрёт себя снаружи и потеряет сервер — это необратимо и это его первый опыт.
    local keys="$SSH_KEYS"
    if [ -z "$keys" ]; then
      keys="$HOME/.ssh/authorized_keys"
      [ -s "$keys" ] || keys="/root/.ssh/authorized_keys"
    fi
    if [ -s "$keys" ]; then
      run_root mkdir -p /etc/ssh/sshd_config.d
      write_root_file /etc/ssh/sshd_config.d/99-unpacker.conf \
        "PasswordAuthentication no
PermitRootLogin prohibit-password
"
      # Юнит зовётся то ssh, то sshd (зависит от дистрибутива), и необработанный отказ
      # под `set -e` убил бы установку посреди шага 0. Пробуем оба имени и не сдаёмся.
      if ! run_root systemctl reload ssh 2>/dev/null; then
        if ! run_root systemctl reload sshd 2>/dev/null; then
          say "  ! не смог перечитать конфиг ssh — примени сам: $SUDO systemctl reload ssh"
        fi
      fi
      say "    вход по паролю выключен (ключ найден: $keys)"
    else
      say "  ! ssh-ключ не найден ($keys) — вход по паролю НЕ выключаю."
      say "      Иначе ты потерял бы доступ к серверу. Сначала настрой вход по ключу:"
      say "      на своём компьютере: ssh-copy-id $(id -un)@<IP-сервера>  → потом запусти install.sh снова."
    fi
  else
    say "  · вход по паролю оставлен как есть (по твоему решению)"
  fi

  if [ "$DO_UNATTENDED" = "true" ]; then
    if [ "$APT_OK" = "true" ]; then
      if apt_install unattended-upgrades; then
        write_root_file /etc/apt/apt.conf.d/20auto-upgrades \
          'APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
'
        say "    автообновления безопасности включены"
      else
        # Это не повод останавливать установку: автообновления — приятный бонус, а не движок.
        say "  ! не смог поставить unattended-upgrades — автообновления не настроены."
        say "      Поставь потом сам: $SUDO apt-get install -y unattended-upgrades"
      fi
    else
      say "  ! apt-get недоступен — автообновления не настроены (не Ubuntu/Debian?)"
    fi
  else
    say "  · автообновления безопасности — пропущены по твоему решению"
  fi
}

# ── юзер движка ─────────────────────────────────────────────────────────────
ensure_run_user() {
  if id -u "$RUN_USER" >/dev/null 2>&1; then
    say "  ✓ юзер движка '$RUN_USER' уже есть"
  else
    say "  · создаю непривилегированного юзера движка '$RUN_USER'"
    run_root useradd --create-home --shell /bin/bash "$RUN_USER"
  fi
}

# ── uv: системно, чтобы его видел и root, и юзер движка (Р3/C4/ADV-01) ──────
uv_step() {
  say "==> шаг 1б: uv (менеджер python-окружения)"
  if [ -x "$UV_DIR/uv" ]; then
    UV_BIN="$UV_DIR/uv"; say "  ✓ uv ($UV_BIN)"; return 0
  fi
  local found=""
  found="$(command -v uv 2>/dev/null || true)"
  if [ -n "$found" ] && asuser_real "test -x '$found'" >/dev/null 2>&1; then
    UV_BIN="$found"; say "  ✓ uv ($UV_BIN) — юзер движка до него достаёт"; return 0
  fi
  if [ -n "$found" ]; then
    say "  ! uv найден ($found), но юзер движка '$RUN_USER' его не запустит."
    say "      Так бывает с uv, поставленным от root: /root открыт только root'у (mode 700),"
    say "      а бот бежит под '$RUN_USER' — юнит упал бы с 203/EXEC. Ставлю системно."
  else
    say "  · uv нет — ставлю системно в $UV_DIR"
  fi
  # Официальный установщик uv, но с УКАЗАННЫМ каталогом: по умолчанию он кладёт в
  # ~/.local/bin вызывающего, то есть в /root — недостижимо для юзера движка.
  if ! run_root env UV_INSTALL_DIR="$UV_DIR" sh -c "curl -LsSf https://astral.sh/uv/install.sh | sh"; then
    block "не смог поставить uv. Сделай сам и запусти install.sh снова: curl -LsSf https://astral.sh/uv/install.sh | $SUDO env UV_INSTALL_DIR=$UV_DIR sh"
    flush_blockers
  fi
  UV_BIN="$UV_DIR/uv"
  if [ "$DRY_RUN" != "true" ] && [ ! -x "$UV_BIN" ]; then
    block "установщик uv отработал, но $UV_BIN не появился. Поставь uv вручную и запусти install.sh снова: curl -LsSf https://astral.sh/uv/install.sh | $SUDO env UV_INSTALL_DIR=$UV_DIR sh"
    flush_blockers
  fi
  say "    uv: $UV_BIN"
}

# ── шаг 2: Claude Code CLI — ИМЕННО у юзера движка (Р4/ADV-16) ──────────────
claude_cli_step() {
  say "==> шаг 2: Claude Code CLI"
  if asuser_real "command -v claude >/dev/null 2>&1"; then
    say "  ✓ claude есть у юзера движка '$RUN_USER'"
    return 0
  fi
  say "  · ставлю Claude Code CLI под юзером '$RUN_USER'"
  asuser "curl -fsSL https://claude.ai/install.sh | bash" || true
  [ "$DRY_RUN" != "true" ] || return 0
  if asuser_real "command -v claude >/dev/null 2>&1"; then
    say "  ✓ claude поставлен"
    return 0
  fi
  # Проверять claude в оболочке root бессмысленно: движок зовёт его под юзером движка.
  # Молчащий бот при HEALTHY-диагностике — худший исход из возможных, поэтому блокер.
  block "у юзера движка '$RUN_USER' нет Claude Code CLI — движок зовёт его именно под этим юзером, и бот поднялся бы, но молчал на каждое сообщение. Поставь так: $SUDO -u $RUN_USER -H bash -lc 'curl -fsSL https://claude.ai/install.sh | bash' — и запусти install.sh снова."
  flush_blockers
  # Версию SDK не выбираем здесь: она приколочена пином в pyproject.toml движка
  # (claude-agent-sdk==0.2.121) и приезжает вместе с кодом — одно место правды.
}

# ── шаг 3: код движка ───────────────────────────────────────────────────────
# `|| true` обязателен: под `set -o pipefail` падение git (нет репо/нет тегов) уронило бы
# весь install.sh молча, без единой строки объяснения.
latest_tag() { git -C "$1" tag --sort=-v:refname 2>/dev/null | head -1 || true; }

# Приватный репо анонимно не клонируется. Дружелюбный путь — gh device-flow: короткий код
# вводится в браузере на ноутбуке (тот же жест, что claude setup-token).
ensure_repo_auth() {
  # GIT_TERMINAL_PROMPT=0: без него git по HTTPS спрашивает логин/пароль прямо в терминале
  # и ждёт вечно — для новичка это «установка зависла». Пусть лучше честно откажет.
  GIT_TERMINAL_PROMPT=0 git ls-remote --exit-code "$REPO_URL" >/dev/null 2>&1 && return 0
  say "  ! репо курса приватный — GitHub просит авторизацию"
  # Вход по коду требует живого человека у терминала. В --non-interactive его запускать
  # нельзя: процесс повис бы навсегда, ожидая ввод (типовая порча автоматических прогонов).
  if [ "$NON_INTERACTIVE" = "true" ]; then
    block "нет доступа к репо $REPO_URL, а в режиме --non-interactive войти в GitHub нельзя (вход требует человека). Запусти install.sh без --non-interactive, либо передай --repo с токеном: https://<логин>:<PAT>@github.com/<owner>/<repo>.git (fine-grained PAT, право Contents:Read). Дружелюбный путь: gh auth login."
    flush_blockers
  fi
  if have gh; then
    say "    сейчас откроется код для входа: скопируй его и введи на github.com/login/device"
    run_root gh auth login --hostname github.com --git-protocol https --web
    run_root gh auth setup-git
    GIT_TERMINAL_PROMPT=0 git ls-remote --exit-code "$REPO_URL" >/dev/null 2>&1 && return 0
  fi
  block "нет доступа к репо $REPO_URL. Дружелюбный путь: поставь gh ($SUDO apt-get install -y gh) и запусти install.sh снова — он проведёт через вход по коду. Путь для продвинутых: сделай fine-grained PAT с правом Contents:Read и запусти с --repo https://<логин>:<PAT>@github.com/<owner>/<repo>.git"
  flush_blockers
}

code_step() {
  say "==> шаг 3: код движка в $ENGINE_DIR"
  local target=""
  # Все git-операции над движком — от root: дерево принадлежит root:root, и git отказывается
  # работать в репо с чужим владельцем (dubious ownership) — раньше это выглядело как
  # «в репо нет тегов» на репо, где тегов полно (C16).
  if [ -d "$ENGINE_DIR/.git" ]; then
    say "  · репо уже на месте — обновляю"
    if ! run_root git -C "$ENGINE_DIR" fetch --tags --force; then
      say "  ! не смог обновить теги (сеть/доступ) — работаю с тем, что уже скачано"
    fi
  else
    ensure_repo_auth
    run_root mkdir -p "$(dirname "$ENGINE_DIR")"
    run_root git clone "$REPO_URL" "$ENGINE_DIR"
  fi
  # Релиз = ТЕГ, не HEAD: один неудачный пуш владельца не должен приезжать ученикам (§10).
  target="$REF"; [ -n "$target" ] || target="$(latest_tag "$ENGINE_DIR")"
  if [ -n "$target" ]; then
    say "  · выкатываю релиз $target"
    run_root git -C "$ENGINE_DIR" checkout --quiet "$target"
  else
    say "  ! тегов-релизов в репо нет — остаюсь на текущей ветке (это dev-режим, для учеников не норма)"
  fi
  # Дерево кода — root:root и НЕ писуемо больше никем. Мета-агенту выдан NOPASSWD-sudo на
  # скрипты внутри: право переписать их (и всё, что они сорсят) = root даром (SEC-2).
  run_root chown -R root:root "$ENGINE_DIR"
  run_root chmod -R go-w "$ENGINE_DIR"
  say "    код движка: root:root, только для чтения (кроме root) — так требует модель прав"
  # Единственное место, куда движку можно писать, — venv вне дерева кода.
  run_root mkdir -p "$UV_PROJECT_ENVIRONMENT"
  run_root chown -R "$RUN_USER" "$UV_PROJECT_ENVIRONMENT"
  say "    venv движка: $UV_PROJECT_ENVIRONMENT (владелец — $RUN_USER)"
}

# Пути инстанса/мозгов резолвим ТЕМ ЖЕ кодом, что deploy.sh (deploy/_common.sh) — иначе
# install.sh искал бы .env не там, где его создал деплой.
resolve_paths() {
  export TG_RUN_USER="$RUN_USER"
  local common="$ENGINE_DIR/deploy/_common.sh"
  if [ -r "$common" ]; then
    # Не сорсим файл, который может править кто угодно: под root это исполнение чужого кода.
    if [ -n "$(find "$common" -maxdepth 0 \( -perm -0002 -o -perm -0020 \) -print 2>/dev/null)" ]; then
      echo "✗ $common писуем группой/всеми — отказываюсь его исполнять." >&2
      echo "  Почини права каталога движка: $SUDO chown -R root:root $ENGINE_DIR && $SUDO chmod -R go-w $ENGINE_DIR" >&2
      exit 3
    fi
    # shellcheck source=deploy/_common.sh
    . "$common"
    resolve_run_identity
  else
    # репо ещё не склонирован (бывает только в --dry-run) — повторяем правило _common.sh
    RUN_HOME="$(getent passwd "$RUN_USER" 2>/dev/null | cut -d: -f6 || true)"
    [ -n "$RUN_HOME" ] || RUN_HOME="/home/$RUN_USER"
    AGENTS_BASE="${TG_AGENTS_BASE:-$RUN_HOME/agents}"
    BRAINS_BASE="${TG_BRAINS_BASE:-$RUN_HOME/brains}"
  fi
}

# ── шаг 4: ответы (§10.4) ───────────────────────────────────────────────────
TOKEN="" USERS="" BRAINS_DIR="" AUTH_MODE="" CC_TOKEN=""
SAVED_USERS="" SAVED_BRAINS="" SAVED_AUTH=""

# Прошлые ответы ПАРСИМ, а не сорсим: файл конфига, исполняемый шеллом, — тот самый класс
# дыр, из-за которого правку одной строки превращают в запуск чего угодно от root.
load_install_conf() {
  local f line key val
  for f in "$INSTALL_CONF" "$ENGINE_DIR/.install.conf"; do
    [ -r "$f" ] || continue
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in ''|'#'*) continue ;; esac
      key="${line%%=*}"; val="${line#*=}"
      [ "$key" != "$line" ] || continue
      case "$key" in
        UNPACKER_ALLOWED_USERS) SAVED_USERS="$val" ;;
        UNPACKER_BRAINS_DIR)    SAVED_BRAINS="$val" ;;
        UNPACKER_AUTH_MODE)     SAVED_AUTH="$val" ;;
        *) : ;;
      esac
    done < "$f"
  done
}

ask() {  # ask <подсказка> <дефолт> → эхо ответа
  local prompt="$1" def="${2:-}" ans=""
  if [ -n "$def" ]; then printf '%s [%s]: ' "$prompt" "$def" >&2; else printf '%s: ' "$prompt" >&2; fi
  read -r ans || ans=""
  [ -n "$ans" ] || ans="$def"
  printf '%s' "$ans"
}

# Токен уже развёрнутого инстанса: на повторном запуске его не спрашивают заново.
token_from_instance() {
  local envf="$AGENTS_BASE/$AGENT_NAME/.env"
  [ -r "$envf" ] || return 0
  grep -E '^TELEGRAM_BOT_TOKEN=' "$envf" 2>/dev/null | head -1 | cut -d= -f2- || true
}

dialog_step() {
  say "==> шаг 4: ответы"
  load_install_conf

  # КОНТУР ПРОВЕРЯЕМ ПЕРВЫМ. Раньше выбор «API-ключ» был четвёртым вопросом и гарантированно
  # обрывал установку — уже ПОСЛЕ того, как ученик ввёл токен и всё остальное (M-12).
  AUTH_MODE="${UNPACKER_AUTH_MODE:-${SAVED_AUTH:-subscription}}"
  if [ "$AUTH_MODE" = "api" ]; then
    echo "✗ контур API-ключа (обслуживание внешних клиентов) пока не поддержан: он приходит в Фазе 4." >&2
    echo "  Сейчас движок работает на подписке Claude Pro/Max — это внутренний контур:" >&2
    echo "  ты и твои сотрудники. Запусти с UNPACKER_AUTH_MODE=subscription (или без этой переменной)." >&2
    echo "  Обслуживать внешних клиентов с подписки нельзя — это нарушение правил Anthropic." >&2
    exit 2
  fi
  [ "$AUTH_MODE" = "subscription" ] || { echo "✗ неизвестный контур '$AUTH_MODE' (ожидалось subscription)" >&2; exit 2; }

  TOKEN="$(read_secret_env "${UNPACKER_BOT_TOKEN:-}" "${UNPACKER_BOT_TOKEN_FILE:-}")"
  CC_TOKEN="$(read_secret_env "${UNPACKER_CC_TOKEN:-}" "${UNPACKER_CC_TOKEN_FILE:-}")"
  USERS="${UNPACKER_ALLOWED_USERS:-$SAVED_USERS}"
  BRAINS_DIR="${UNPACKER_BRAINS_DIR:-$SAVED_BRAINS}"
  # Токен уже развёрнутого бота: чтобы прочитать .env инстанса (600, владелец — юзер
  # движка), install.sh должен идти от root/sudo — так он и запускается.
  [ -n "$TOKEN" ] || TOKEN="$(token_from_instance)"

  if [ "$NON_INTERACTIVE" = "true" ]; then
    [ -n "$TOKEN" ] || { echo "нет ответа: UNPACKER_BOT_TOKEN (токен бота из @BotFather)" >&2; exit 2; }
    [ -n "$USERS" ] || { echo "нет ответа: UNPACKER_ALLOWED_USERS (твой Telegram user id)" >&2; exit 2; }
    [ -n "$BRAINS_DIR" ] || BRAINS_DIR="$BRAINS_BASE"
  else
    say ""
    say "  1/3. Токен бота из @BotFather (ввод не видно — так и должно быть)."
    if [ -n "$TOKEN" ]; then
      say "       нашёл токен уже развёрнутого бота — оставляю его."
    else
      printf '       токен: ' >&2
      read -rs TOKEN || TOKEN=""
      printf '\n' >&2
    fi
    say "  2/3. Кому можно писать боту — твой Telegram user id (узнать: напиши @userinfobot)."
    USERS="$(ask "       id через запятую" "$USERS")"
    say "  3/3. Где будут лежать папки-мозги агентов."
    BRAINS_DIR="$(ask "       путь" "${BRAINS_DIR:-$BRAINS_BASE}")"
  fi

  # ── валидация: сообщения не эхоят введённое (кривой ввод может быть настоящим секретом)
  if ! printf '%s' "$TOKEN" | grep -Eq '^[0-9]+:[A-Za-z0-9_-]+$'; then
    echo "✗ токен бота не похож на токен: у BotFather он вида 123456789:AAE... (цифры, двоеточие, буквы)." >&2
    echo "  Открой @BotFather → /mybots → твой бот → API Token и скопируй целиком." >&2
    exit 2
  fi
  if ! printf '%s' "$USERS" | grep -Eq '^[0-9]+(,[0-9]+)*$'; then
    echo "✗ id пользователей — это ЧИСЛА через запятую (напр. 123456789), не @username." >&2
    echo "  Свой id узнай так: напиши в Telegram боту @userinfobot — он пришлёт число." >&2
    exit 2
  fi

  export TG_BRAINS_BASE="$BRAINS_DIR"
  asuser "mkdir -p '$BRAINS_DIR'"
  say "    принято: пользователей — $(printf '%s' "$USERS" | tr ',' ' ' | wc -w | tr -d ' '), мозги — $BRAINS_DIR"
  say "    оплата работы агента: подписка Claude Pro/Max (внутренний контур §8.1)."
  say "    Контур API-ключа для внешних клиентов — Фаза 4, движок его пока не поднимает."
  say "    (напоминание: вызовы движка расходуют лимит твоей подписки — как будто ты сам сидишь в Claude Code)"

  # Секреты в install.conf НЕ пишем: токен живёт только в .env инстанса (chmod 600).
  if [ "$DRY_RUN" = "true" ]; then
    echo "[dry-run] запомнил бы ответы в $INSTALL_CONF (без секретов, chmod 600)"
  else
    run_root mkdir -p "$ETC_DIR"
    write_root_file "$INSTALL_CONF" "$(printf '# Ответы ученика, чтобы не спрашивать их второй раз. Секретов здесь нет.\nUNPACKER_ALLOWED_USERS=%s\nUNPACKER_BRAINS_DIR=%s\nUNPACKER_AUTH_MODE=%s\nUNPACKER_RUN_USER=%s\n' \
      "$USERS" "$BRAINS_DIR" "$AUTH_MODE" "$RUN_USER")" 0600
    # Старое место (внутри дерева движка) убираем: дерево обязано быть неизменяемым.
    if [ -f "$ENGINE_DIR/.install.conf" ]; then run_root rm -f "$ENGINE_DIR/.install.conf"; fi
  fi
}

# ── машинный конфиг: одна вселенная путей для всех точек входа (Р2) ──────────
# Без него личность движка выводилась из окружения вызывающего (TG_RUN_USER → SUDO_USER →
# id -un): root вручную, sudo от агента и cron читали ТРИ разных вселенных путей — и каждая
# тихо рапортовала успех. Здесь же лечится и потеря TG_BRAINS_BASE при env_reset (ADV-09).
write_machine_config() {
  local body
  # TG_VENV обязателен: venv вынесен из дерева движка (Р1), и без этой строки `uv` у deploy.sh
  # уйдёт в дефолтный $RUNTIME/.venv — то есть в root-owned каталог, куда run-user писать не
  # может. Симптом был бы «деплой нового бота падает на правах», причина — невидима.
  body="$(printf '# Машинный конфиг движка «Распаковщик» — единая карта путей.\n# Пишет install.sh. Читают deploy/_common.sh и update.sh: ПОСТРОЧНО, без source.\n# Правь только если знаешь, что делаешь: сюда смотрят все точки входа.\nTG_RUN_USER=%s\nTG_RUNTIME=%s\nTG_AGENTS_BASE=%s\nTG_BRAINS_BASE=%s\nTG_UV_BIN=%s\nTG_VENV=%s\n' \
    "$RUN_USER" "$ENGINE_DIR" "$AGENTS_BASE" "$BRAINS_DIR" "$UV_BIN" "$UV_PROJECT_ENVIRONMENT")"
  if ! run_root mkdir -p "$ETC_DIR" 2>/dev/null; then
    block "не создать каталог $ETC_DIR" "sudo mkdir -p $ETC_DIR"
    flush_blockers
  fi
  write_root_file "$ENGINE_CONF" "$body" 0644
  run_root chown root:root "$ENGINE_CONF" 2>/dev/null || true
  # Проверяем ФАКТ записи, а не отсутствие ошибки: write_root_file применяет права
  # best-effort, и молча потерянная карта путей вернула бы весь класс ADV-05/ADV-09
  # («каждая точка входа читает свою вселенную путей и рапортует успех»).
  if [ ! -s "$ENGINE_CONF" ] && [ "$DRY_RUN" != "true" ]; then
    block "карта путей $ENGINE_CONF не записалась" "проверь права на $ETC_DIR и запусти установщик снова"
    flush_blockers
  fi
  say "    карта путей: $ENGINE_CONF"
}

# ── шаг 5: bootstrap Распаковщика (§10.5 — детерминированно, скриптом) ──────
deploy_step() {
  say "==> шаг 5: разворачиваю Распаковщика"
  local brain="$ENGINE_DIR/brains/unpacker" dep="$ENGINE_DIR/deploy/deploy.sh"
  # Целостность кода проверяем, только когда код реально лежит на диске: в --dry-run до
  # первой установки его ещё нет, и ругаться на «нет мозга» было бы ложной тревогой.
  if [ -d "$ENGINE_DIR/.git" ] || [ "$DRY_RUN" != "true" ]; then
    if [ ! -f "$brain/CLAUDE.md" ]; then
      block "в репо нет мозга brains/unpacker (или он без CLAUDE.md) — код скачан неполностью. Проверь: ls $ENGINE_DIR/brains/unpacker, при пустоте — восстанови: git -C $ENGINE_DIR checkout ."
      flush_blockers
    fi
    [ -f "$dep" ] || { block "не нашёл $dep — код скачан неполностью, склонируй репо заново"; flush_blockers; }
  else
    say "  · код ещё не скачан — мозг Распаковщика проверю на боевом прогоне"
  fi

  # --role unpacker обязателен: без него мета-агент разворачивается БЕЗ прав (нет systemd
  # drop-in с NoNewPrivileges=no и нет sudoers-whitelist) и не может развернуть ни одного
  # бота — то есть главное обещание продукта не работает (C3).
  local args=(--surface tg --role unpacker --name "$AGENT_NAME" --brain "$brain" --users "$USERS")
  export TG_RUNTIME="$ENGINE_DIR"
  export TG_UV_BIN="$UV_BIN"
  if [ "$DRY_RUN" = "true" ]; then
    # Токен в план не печатаем: dry-run часто копируют в чат/поддержку.
    echo "[dry-run] $dep ${args[*]} --token-file <файл 600, удаляется после деплоя>"
    return 0
  fi
  ensure_secret_dir
  args+=(--token-file "$(secret_file bot.token "$TOKEN")")
  [ -n "$CC_TOKEN" ] && args+=(--cc-token-file "$(secret_file cc.token "$CC_TOKEN")")
  "$dep" "${args[@]}"
  # Файл с секретом живёт ровно на время деплоя.
  cleanup_secrets; SECRET_DIR=""
}

# ── шаг 6: финал + шпаргалка ────────────────────────────────────────────────
final_step() {
  say ""
  # В dry-run нельзя рапортовать «бот поднят»: ничего не делали. Ложный успех в плане —
  # это ученик, который ищет живого бота там, где его нет.
  if [ "$DRY_RUN" = "true" ]; then
    say "==> это был только план — ничего не изменено."
    say "    Всё устраивает? Запусти по-настоящему, без --dry-run:  bash install.sh"
    return 0
  fi
  say "==> готово. Бот поднят."
  say ""
  say "    ЧТО ДЕЛАТЬ СЕЙЧАС: открой своего бота в Telegram и напиши ему /start."
  say "    Дальше говори с ним словами: «разверни мозг из $BRAINS_DIR/<папка> в телеграм»."
  say ""
  if [ -z "$CC_TOKEN" ]; then
    say "    ОДИН ХВОСТ (5 минут, иначе бот замолчит через час):"
    say "      выдай боту долгоживущий токен подписки —"
    say "      1) $SUDO -u $RUN_USER -H claude setup-token"
    say "      2) увидишь ссылку → открой её в браузере на своём компьютере, войди в Claude,"
    say "         скопируй короткий код и вставь обратно в терминал;"
    say "      3) получишь строку вида sk-ant-oat01-… — это и есть токен;"
    say "      4) положи его в ФАЙЛ (в командной строке секреты светятся всей машине):"
    say "           umask 077; cat > /root/cc-token.txt      ← вставь токен, потом Ctrl+D"
    say "      5) запусти снова: UNPACKER_CC_TOKEN_FILE=/root/cc-token.txt bash $SELF_PATH"
    say ""
  fi
  say "    ШПАРГАЛКА (команды даны в форме, разрешённой правами мета-агента):"
  say "      бот молчит?          спроси у него самого, а если он мёртв —"
  say "                           $SUDO $ENGINE_DIR/deploy/agentctl.sh doctor $AGENT_NAME"
  say "      логи                 $SUDO journalctl -u agent-tg@$AGENT_NAME -n 50 --no-pager"
  say "      перезапуск           $SUDO systemctl restart agent-tg@$AGENT_NAME"
  say "      обновление движка    $SUDO $ENGINE_DIR/update.sh"
  say "      откат обновления     $SUDO $ENGINE_DIR/update.sh --rollback"
  say "      базы сессий назад    $SUDO $ENGINE_DIR/update.sh --restore-db"
  say "      инструкция целиком   $ENGINE_DIR/README.md"
}

harden
check_env
say "==> юзер движка"
ensure_run_user
uv_step
claude_cli_step
code_step
resolve_paths
dialog_step
write_machine_config
deploy_step
final_step
