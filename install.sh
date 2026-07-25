#!/usr/bin/env bash
# install.sh — поставить движок «Распаковщик» на свежий VPS ОДНОЙ командой (§10 конституции).
#
# Для кого: у тебя есть VPS (Ubuntu 22+/24), подписка Claude Pro/Max и бот из @BotFather.
# Опыт в Linux не нужен: каждая непройденная проверка печатает не «ошибку», а инструкцию
# «сделай вот это».
#
# Запуск (от root на свежем сервере):
#   bash install.sh
#
# Идемпотентно: повторный запуск — обновление, а не поломка. Ответы (кроме секретов)
# запоминаются, второй раз их не спрашивают.
#
# Флаги:
#   --dry-run              показать план, ничего не менять
#   --non-interactive      не задавать вопросов, ответы взять из переменных окружения:
#                          UNPACKER_BOT_TOKEN, UNPACKER_ALLOWED_USERS, UNPACKER_BRAINS_DIR,
#                          UNPACKER_AUTH_MODE (subscription|api), UNPACKER_CC_TOKEN
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
# Секреты: токен бота вводится БЕЗ эха, не попадает ни в вывод, ни в history, ни в argv
# install.sh. Живёт только в .env инстанса с правами 600.
set -euo pipefail

# ── значения по умолчанию ────────────────────────────────────────────────────
DRY_RUN="false" NON_INTERACTIVE="false"
ENGINE_DIR="/opt/unpacker" RUN_USER="unpacker" AGENT_NAME="unpacker"
REPO_URL="${UNPACKER_REPO:-https://github.com/rusanovproject-dotcom/unpacker.git}"
REF="" RAM_MB="" MIN_DISK_MB="3000" SSH_KEYS=""
DO_UFW="true" DO_SSH="true" DO_UNATTENDED="true"

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

SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
run() { if [ "$DRY_RUN" = "true" ]; then echo "[dry-run] $*"; else "$@"; fi; }

# Команда от имени юзера движка. Через `bash -lc`: на VPS per-user claude/uv лежат в
# ~/.local/bin, а он есть только в login-PATH.
asuser() {
  if [ "$DRY_RUN" = "true" ]; then echo "[dry-run] (as $RUN_USER) $*"
  elif [ "$RUN_USER" = "$(id -un)" ]; then bash -lc "$*"
  else $SUDO -u "$RUN_USER" -H bash -lc "$*"; fi
}

# Файл под root'ом (конфиги в /etc). Отдельно от run(), чтобы содержимое не текло в argv.
write_root_file() {
  if [ "$DRY_RUN" = "true" ]; then echo "[dry-run] записал бы $1"
  else printf '%s' "$2" | $SUDO tee "$1" >/dev/null; fi
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
say()   { printf '%s\n' "$1"; }

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

# ── шаг 1: проверки среды (§10.1) ───────────────────────────────────────────
# Потолок тёплого пула считаем ТОЙ ЖЕ формулой, что engine/core/pool.py
# (compute_pool_ceiling): (RAM − 1.5ГБ) / 1ГБ на агента. Расхождение формул = ложное
# обещание «столько агентов поднимется».
POOL_RESERVE_MB=1536
POOL_PER_AGENT_MB=1024

detect_ram_mb() {
  if [ -r /proc/meminfo ]; then
    awk '/^MemTotal:/{printf "%d", $2/1024}' /proc/meminfo
  elif command -v sysctl >/dev/null 2>&1 && sysctl -n hw.memsize >/dev/null 2>&1; then
    echo $(( $(sysctl -n hw.memsize) / 1024 / 1024 ))
  else
    echo 0
  fi
}

# «Умеет ли эта машина ставить пакеты сама» — от этого зависит, блокер отсутствие
# инструмента или install.sh молча его доставит.
APT_OK="false"
apt-get --version >/dev/null 2>&1 && APT_OK="true"

# Инструмент есть и запускается? (`command -v` недостаточно: битый бинарь тоже находится.)
have() { "$1" --version >/dev/null 2>&1; }

# Нужный инструмент: доставить apt'ом либо выдать блокер с готовой командой.
need_tool() {
  local cmd="$1" pkg="$2"
  have "$cmd" && { say "  ✓ $cmd"; return 0; }
  if [ "$APT_OK" = "true" ]; then
    say "  · $cmd отсутствует — ставлю пакетом $pkg"
    run $SUDO apt-get install -y "$pkg"
    return 0
  fi
  block "$cmd не найден. Поставь: $SUDO apt-get update && $SUDO apt-get install -y $pkg"
}

check_env() {
  say "==> шаг 1: проверки среды"

  [ "$(uname -s)" = "Linux" ] || say "  ! это не Linux — install.sh рассчитан на Ubuntu 22+/24 (здесь только --dry-run)"

  # RAM → потолок тёплого пула
  [ -n "$RAM_MB" ] || RAM_MB="$(detect_ram_mb)"
  if ! printf '%s' "$RAM_MB" | grep -Eq '^[0-9]+$' || [ "$RAM_MB" -le 0 ]; then
    block "не смог измерить RAM. Задай вручную: --ram-mb <сколько ГБ×1024>"
  else
    POOL_CEILING=$(( (RAM_MB - POOL_RESERVE_MB) / POOL_PER_AGENT_MB ))
    if [ "$POOL_CEILING" -lt 1 ]; then
      block "RAM ${RAM_MB} МБ мало: движку нужно ~1 ГБ на активного агента плюс 1.5 ГБ серверу. Возьми VPS с 4 ГБ RAM и повтори."
    else
      say "  ✓ RAM ${RAM_MB} МБ → потолок тёплого пула: ${POOL_CEILING} активных агент(ов) одновременно"
      [ "$RAM_MB" -ge 4096 ] || say "  ! меньше 4 ГБ — рекомендованный минимум 4 ГБ (один агент будет впритык)"
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

  need_tool git git
  need_tool curl curl
  need_tool tmux tmux

  # uv ставится своим официальным установщиком (в apt его нет)
  if have uv; then
    say "  ✓ uv"
  else
    say "  · uv отсутствует — ставлю официальным установщиком"
    run bash -c "curl -LsSf https://astral.sh/uv/install.sh | sh"
  fi

  flush_blockers
  say "    среда готова"
}

# ── шаг 0: мини-hardening (§10.0) ───────────────────────────────────────────
# Дёшево снимает главные риски целевой аудитории (VPS с паролем наружу). Каждый пункт
# можно отклонить флагом: сервер чужой, навязывать нельзя.
harden() {
  say "==> шаг 0: базовая защита сервера"

  if [ "$DO_UFW" = "true" ]; then
    if have ufw; then
      # Порядок важен: сначала разрешаем ssh, только потом включаем deny-политику —
      # иначе enable отрезает текущую сессию вместе с доступом к серверу.
      run $SUDO ufw allow OpenSSH
      run $SUDO ufw default deny incoming
      run $SUDO ufw default allow outgoing
      run $SUDO ufw --force enable
      say "    файрвол: снаружи открыт только ssh (веб-порты откроются в Фазе 3, когда будет веб)"
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
      run $SUDO mkdir -p /etc/ssh/sshd_config.d
      write_root_file /etc/ssh/sshd_config.d/99-unpacker.conf \
        "PasswordAuthentication no
PermitRootLogin prohibit-password
"
      # Юнит зовётся то ssh, то sshd (зависит от дистрибутива), и необработанный отказ
      # под `set -e` убил бы установку посреди шага 0. Пробуем оба имени и не сдаёмся.
      if ! run $SUDO systemctl reload ssh 2>/dev/null; then
        if ! run $SUDO systemctl reload sshd 2>/dev/null; then
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
      run $SUDO apt-get install -y unattended-upgrades
      write_root_file /etc/apt/apt.conf.d/20auto-upgrades \
        'APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
'
      say "    автообновления безопасности включены"
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
    run $SUDO useradd --create-home --shell /bin/bash "$RUN_USER"
  fi
}

# ── шаг 2: Claude Code CLI ──────────────────────────────────────────────────
claude_cli_step() {
  say "==> шаг 2: Claude Code CLI"
  if have claude; then
    say "  ✓ claude уже стоит"
  else
    say "  · ставлю Claude Code CLI под юзером $RUN_USER"
    asuser "curl -fsSL https://claude.ai/install.sh | bash"
  fi
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
    run gh auth login --hostname github.com --git-protocol https --web
    run gh auth setup-git
    GIT_TERMINAL_PROMPT=0 git ls-remote --exit-code "$REPO_URL" >/dev/null 2>&1 && return 0
  fi
  block "нет доступа к репо $REPO_URL. Дружелюбный путь: поставь gh ($SUDO apt-get install -y gh) и запусти install.sh снова — он проведёт через вход по коду. Путь для продвинутых: сделай fine-grained PAT с правом Contents:Read и запусти с --repo https://<логин>:<PAT>@github.com/<owner>/<repo>.git"
  flush_blockers
}

code_step() {
  say "==> шаг 3: код движка в $ENGINE_DIR"
  local target=""
  if [ -d "$ENGINE_DIR/.git" ]; then
    say "  · репо уже на месте — обновляю"
    if ! run git -C "$ENGINE_DIR" fetch --tags --force; then
      say "  ! не смог обновить теги (сеть/доступ) — работаю с тем, что уже скачано"
    fi
  else
    ensure_repo_auth
    run $SUDO mkdir -p "$(dirname "$ENGINE_DIR")"
    run git clone "$REPO_URL" "$ENGINE_DIR"
  fi
  # Релиз = ТЕГ, не HEAD: один неудачный пуш владельца не должен приезжать ученикам (§10).
  target="$REF"; [ -n "$target" ] || target="$(latest_tag "$ENGINE_DIR")"
  if [ -n "$target" ]; then
    say "  · выкатываю релиз $target"
    run git -C "$ENGINE_DIR" checkout --quiet "$target"
  else
    say "  ! тегов-релизов в репо нет — остаюсь на текущей ветке (это dev-режим, для учеников не норма)"
  fi
  run $SUDO chown -R "$RUN_USER" "$ENGINE_DIR"
}

# Пути инстанса/мозгов резолвим ТЕМ ЖЕ кодом, что deploy.sh (deploy/_common.sh) — иначе
# install.sh искал бы .env не там, где его создал деплой.
resolve_paths() {
  export TG_RUN_USER="$RUN_USER"
  if [ -r "$ENGINE_DIR/deploy/_common.sh" ]; then
    # shellcheck source=deploy/_common.sh
    . "$ENGINE_DIR/deploy/_common.sh"
    resolve_run_identity
  else
    # репо ещё не склонирован (бывает только в --dry-run) — повторяем правило _common.sh
    RUN_HOME="$(getent passwd "$RUN_USER" 2>/dev/null | cut -d: -f6 || true)"
    [ -n "$RUN_HOME" ] || RUN_HOME="/home/$RUN_USER"
    AGENTS_BASE="${TG_AGENTS_BASE:-$RUN_HOME/agents}"
    BRAINS_BASE="${TG_BRAINS_BASE:-$RUN_HOME/brains}"
  fi
}

# ── шаг 4: четыре ответа (§10.4) ────────────────────────────────────────────
CONF="" TOKEN="" USERS="" BRAINS_DIR="" AUTH_MODE="" CC_TOKEN=""

# Токен уже развёрнутого инстанса: на повторном запуске его не спрашивают заново.
token_from_instance() {
  local envf="$AGENTS_BASE/$AGENT_NAME/.env"
  [ -r "$envf" ] || return 0
  grep -E '^TELEGRAM_BOT_TOKEN=' "$envf" 2>/dev/null | head -1 | cut -d= -f2- || true
}

ask() {  # ask <подсказка> <дефолт> → эхо ответа
  local prompt="$1" def="${2:-}" ans=""
  if [ -n "$def" ]; then printf '%s [%s]: ' "$prompt" "$def" >&2; else printf '%s: ' "$prompt" >&2; fi
  read -r ans || ans=""
  [ -n "$ans" ] || ans="$def"
  printf '%s' "$ans"
}

dialog_step() {
  say "==> шаг 4: четыре ответа"
  CONF="$ENGINE_DIR/.install.conf"
  # Ответы этого запуска запоминаем ДО чтения прошлых: `. "$CONF"` перезаписал бы
  # одноимённые переменные, и «поменял ответ и переустановил» тихо не срабатывало бы.
  local now_users="${UNPACKER_ALLOWED_USERS:-}" now_brains="${UNPACKER_BRAINS_DIR:-}"
  local now_auth="${UNPACKER_AUTH_MODE:-}" now_token="${UNPACKER_BOT_TOKEN:-}"
  # Прошлые ответы — дефолты (идемпотентность: второй раз ничего не переспрашиваем).
  # shellcheck disable=SC1090
  [ -r "$CONF" ] && . "$CONF"
  USERS="${now_users:-${UNPACKER_ALLOWED_USERS:-}}"
  BRAINS_DIR="${now_brains:-${UNPACKER_BRAINS_DIR:-}}"
  AUTH_MODE="${now_auth:-${UNPACKER_AUTH_MODE:-}}"
  CC_TOKEN="${UNPACKER_CC_TOKEN:-}"
  TOKEN="$now_token"
  # Токен уже развёрнутого бота: чтобы прочитать .env инстанса (600, владелец — юзер
  # движка), install.sh должен идти от root/sudo — так он и запускается.
  [ -n "$TOKEN" ] || TOKEN="$(token_from_instance)"

  if [ "$NON_INTERACTIVE" = "true" ]; then
    [ -n "$TOKEN" ] || { echo "нет ответа: UNPACKER_BOT_TOKEN (токен бота из @BotFather)" >&2; exit 2; }
    [ -n "$USERS" ] || { echo "нет ответа: UNPACKER_ALLOWED_USERS (твой Telegram user id)" >&2; exit 2; }
    [ -n "$AUTH_MODE" ] || AUTH_MODE="subscription"
    [ -n "$BRAINS_DIR" ] || BRAINS_DIR="$BRAINS_BASE"
  else
    say ""
    say "  1/4. Токен бота из @BotFather (ввод не видно — так и должно быть)."
    if [ -n "$TOKEN" ]; then
      say "       нашёл токен уже развёрнутого бота — оставляю его."
    else
      printf '       токен: ' >&2
      read -rs TOKEN || TOKEN=""
      printf '\n' >&2
    fi
    say "  2/4. Кому можно писать боту — твой Telegram user id (узнать: напиши @userinfobot)."
    USERS="$(ask "       id через запятую" "$USERS")"
    say "  3/4. Где будут лежать папки-мозги агентов."
    BRAINS_DIR="$(ask "       путь" "${BRAINS_DIR:-$BRAINS_BASE}")"
    say "  4/4. Чем платим за работу агента:"
    say "         1 — подписка Claude Pro/Max (для себя и сотрудников; дешевле в 10–20 раз)"
    say "         2 — API-ключ (обслуживание внешних клиентов; Фаза 4, пока не поддержано)"
    case "$(ask "       выбор" "1")" in
      2|api) AUTH_MODE="api" ;;
      *)     AUTH_MODE="subscription" ;;
    esac
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
  if [ "$AUTH_MODE" = "api" ]; then
    echo "✗ контур API-ключа (обслуживание внешних клиентов) пока не поддержан: он приходит в Фазе 4." >&2
    echo "  Сейчас движок работает на подписке Claude Pro/Max — это внутренний контур:" >&2
    echo "  ты и твои сотрудники. Выбери подписку (UNPACKER_AUTH_MODE=subscription)." >&2
    echo "  Обслуживать внешних клиентов с подписки нельзя — это нарушение правил Anthropic." >&2
    exit 2
  fi
  [ "$AUTH_MODE" = "subscription" ] || { echo "✗ неизвестный контур '$AUTH_MODE' (ожидалось subscription или api)" >&2; exit 2; }

  export TG_BRAINS_BASE="$BRAINS_DIR"
  asuser "mkdir -p '$BRAINS_DIR'"
  say "    принято: пользователей — $(printf '%s' "$USERS" | tr ',' ' ' | wc -w | tr -d ' '), мозги — $BRAINS_DIR, контур — подписка"
  say "    (напоминание: вызовы движка расходуют лимит твоей подписки — как будто ты сам сидишь в Claude Code)"

  # Секреты в .install.conf НЕ пишем: токен живёт только в .env инстанса (chmod 600).
  if [ "$DRY_RUN" = "true" ]; then
    echo "[dry-run] запомнил бы ответы в $CONF (без секретов, chmod 600)"
  else
    umask 077
    printf 'UNPACKER_ALLOWED_USERS=%s\nUNPACKER_BRAINS_DIR=%s\nUNPACKER_AUTH_MODE=%s\nUNPACKER_RUN_USER=%s\n' \
      "$USERS" "$BRAINS_DIR" "$AUTH_MODE" "$RUN_USER" > "$CONF"
    chmod 600 "$CONF"
  fi
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

  local args=(--surface tg --name "$AGENT_NAME" --brain "$brain" --users "$USERS" --token "$TOKEN")
  [ -n "$CC_TOKEN" ] && args+=(--cc-token "$CC_TOKEN")
  export TG_RUNTIME="$ENGINE_DIR"
  if [ "$DRY_RUN" = "true" ]; then
    # Токен в план не печатаем: dry-run часто копируют в чат/поддержку.
    echo "[dry-run] bash $dep --surface tg --name $AGENT_NAME --brain $brain --users $USERS --token [REDACTED]"
  else
    bash "$dep" "${args[@]}"
  fi
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
    say "      4) запусти снова: UNPACKER_CC_TOKEN=<токен> bash install.sh"
    say ""
  fi
  say "    ШПАРГАЛКА:"
  say "      бот молчит?          спроси у него самого, а если он мёртв — $ENGINE_DIR/deploy/agentctl.sh doctor $AGENT_NAME"
  say "      логи                 $SUDO journalctl -u agent-tg@$AGENT_NAME -n 50 --no-pager"
  say "      перезапуск           $SUDO systemctl restart agent-tg@$AGENT_NAME"
  say "      обновление движка    $SUDO bash $ENGINE_DIR/update.sh"
  say "      откат обновления     $SUDO bash $ENGINE_DIR/update.sh --rollback"
  say "      инструкция целиком   $ENGINE_DIR/README.md"
}

harden
check_env
say "==> юзер движка"
ensure_run_user
claude_cli_step
code_step
resolve_paths
dialog_step
deploy_step
final_step
