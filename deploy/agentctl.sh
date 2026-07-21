#!/usr/bin/env bash
# deploy/agentctl.sh — read-only операции над развёрнутыми инстансами движка «Распаковщик».
#
# Спутник deploy.sh (который ПРОВИЗИОНИТ). agentctl только НАБЛЮДАЕТ — не стартует, не
# останавливает, ничего не мутирует, поэтому безопасен против живых ботов.
#
# У движка НЕТ tmux (бот = `python -m engine`, не CC-панель) — проверяем systemd + .env +
# health-маркер + resume-БД, без tmux-логики боевого рантайма.
#
# Использование:
#   agentctl.sh list                 # все инстансы + их systemd-состояние
#   agentctl.sh status <name>        # один агент: systemd, .env, health, resume-БД
#   agentctl.sh logs   <name> [N]    # последние N строк journal для agent-tg@<name>
#   agentctl.sh health <name>        # OK/DEGRADED одной строкой (скриптовый exit code)
#   agentctl.sh doctor <name>        # полная самодиагностика + подсказки (для ученика)
#
# Env (совпадают с deploy.sh): TG_AGENTS_BASE, TG_RUN_USER, TG_RUNTIME.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/_common.sh
. "$HERE/_common.sh"
resolve_run_identity

_unit() { printf 'agent-tg@%s' "$1"; }

# Валидация имени: тот же slug, что в deploy.sh — иначе `status ../../etc` читал бы чужие пути
# (path-traversal на чтение через stat/grep).
_check_name() {
  valid_slug "$1" || { echo "недопустимое имя '$1' (^[a-z0-9]([a-z0-9-]*[a-z0-9])?\$)" >&2; exit 2; }
}

# Значение ключа из .env инстанса (или пусто).
_envval() {
  local envf="$AGENTS_BASE/$1/.env"
  [ -f "$envf" ] || return 0
  grep -E "^$2=" "$envf" | head -1 | cut -d= -f2- | tr -d '"'"'"'' || true
}

# Абсолютный путь до файла состояния; относительный резолвится под инстанс.
_statepath() {
  local v; v="$(_envval "$1" "$2")"; [ -n "$v" ] || v="$3"
  case "$v" in /*) echo "$v" ;; *) echo "$AGENTS_BASE/$1/$v" ;; esac
}

# Health-маркер (§5.4): движок пишет health.json со status ok|degraded. Echo:
#   "ok"              — маркер говорит ok
#   "degraded:<r>"    — маркер говорит degraded
#   "unknown"         — файла нет: бот ещё не обрабатывал сообщение ЛИБО упал до записи.
# НЕ трактуем отсутствие как "ok": свежий бот с уже мёртвым токеном не должен читаться здоровым.
_health_marker() {
  local f; f="$(_statepath "$1" HEALTH_PATH state/health.json)"
  [ -f "$f" ] || { echo "unknown"; return 0; }
  if command -v jq >/dev/null 2>&1; then
    local status reason
    status="$(jq -r '.status // "ok"' "$f" 2>/dev/null || echo ok)"
    reason="$(jq -r '.reason // ""' "$f" 2>/dev/null || echo "")"
    [ "$status" = "degraded" ] && echo "degraded:${reason:-unknown}" || echo "ok"
  else
    # Без jq: матчим именно пару status→degraded, не литерал 'degraded' где угодно
    # (reason="recovered from degraded" при status=ok не должен давать ложный DEGRADED).
    if grep -Eq '"status"[[:space:]]*:[[:space:]]*"degraded"' "$f"; then echo "degraded:marker"; else echo "ok"; fi
  fi
}

# systemd ActiveState юнита, либо "absent" (не установлен / нет systemctl — напр. macOS).
_svc_state() {
  command -v systemctl >/dev/null 2>&1 || { echo "absent"; return 0; }
  local u; u="$(_unit "$1")"
  if systemctl cat "$u" >/dev/null 2>&1; then
    systemctl is-active "$u" 2>/dev/null || true
  else
    echo "absent"
  fi
}

# resume: есть ли БД сессий (переживание restart/reboot держится на ней).
_resume_db() {
  local f; f="$(_statepath "$1" DB_PATH state/state.db)"
  [ -f "$f" ] && echo "present" || echo "absent"
}

cmd_list() {
  printf '%-20s %-10s %-9s %s\n' "AGENT" "SYSTEMD" "RESUME" "INSTANCE"
  if [ ! -d "$AGENTS_BASE" ]; then echo "(нет инстансов в $AGENTS_BASE)"; return 0; fi
  local d name
  for d in "$AGENTS_BASE"/*/; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    printf '%-20s %-10s %-9s %s\n' "$name" "$(_svc_state "$name")" "$(_resume_db "$name")" "$d"
  done
}

cmd_status() {
  local name="${1:?usage: agentctl.sh status <name>}"; _check_name "$name"
  local inst="$AGENTS_BASE/$name"
  echo "agent:    $name"
  echo "instance: $inst $( [ -d "$inst" ] && echo '(present)' || echo '(MISSING)')"
  echo "systemd:  $(_svc_state "$name")"
  if [ -f "$inst/.env" ]; then
    local perms; perms="$(stat -c '%a' "$inst/.env" 2>/dev/null || stat -f '%Lp' "$inst/.env" 2>/dev/null || echo '?')"
    echo ".env:     present (perms $perms)$( [ "$perms" = "600" ] || echo '  ⚠ ожидалось 600')"
  else
    echo ".env:     MISSING ⚠"
  fi
  echo "resume:   $(_resume_db "$name") (state.db)"
  echo "auth:     $(_health_marker "$name")"
}

cmd_logs() {
  local name="${1:?usage: agentctl.sh logs <name> [N]}"; _check_name "$name"
  local n="${2:-50}"
  journalctl -u "$(_unit "$name")" -n "$n" --no-pager 2>/dev/null || echo "нет journal для $(_unit "$name")"
}

# Скриптовый health: exit 0 = OK, 1 = DEGRADED. OK = .env есть И systemd active И auth НЕ degraded.
# auth=unknown (маркера ещё нет) не роняет в DEGRADED — живой процесс «up», аутентификация ленива;
# но и не выдаёт ложного «здоров» при явном degraded.
cmd_health() {
  local name="${1:?usage: agentctl.sh health <name>}"; _check_name "$name"
  local inst="$AGENTS_BASE/$name" svc auth ok=0
  svc="$(_svc_state "$name")"; auth="$(_health_marker "$name")"
  [ -f "$inst/.env" ] && [ "$svc" = "active" ] && ok=1
  case "$auth" in degraded*) ok=0 ;; esac
  if [ "$ok" = 1 ]; then echo "$name: OK (systemd=$svc auth=$auth)"; exit 0
  else echo "$name: DEGRADED (systemd=$svc env=$( [ -f "$inst/.env" ] && echo yes || echo NO) auth=$auth)"; exit 1; fi
}

# Человеческая самодиагностика ОДНОГО агента: каждая проверка + подсказка на провал.
cmd_doctor() {
  local name="${1:?usage: agentctl.sh doctor <name>}"; _check_name "$name"
  local inst="$AGENTS_BASE/$name" issues=0
  echo "==> doctor: $name  (run-user=$RUN_USER, base=$AGENTS_BASE)"

  if [ ! -d "$inst" ]; then
    echo "  ✗ инстанс-каталог отсутствует ($inst)"
    echo "      → не развёрнут здесь. Запусти deploy.sh --name $name ... (проверь TG_RUN_USER)"
    echo "==> verdict: NOT DEPLOYED"; exit 1
  fi
  echo "  ✓ instance dir: $inst"

  if [ -f "$inst/.env" ]; then
    local perms; perms="$(stat -c '%a' "$inst/.env" 2>/dev/null || stat -f '%Lp' "$inst/.env" 2>/dev/null || echo '?')"
    if [ "$perms" = "600" ]; then echo "  ✓ .env present (perms 600)"
    else echo "  ! .env perms $perms (ожидалось 600)  → chmod 600 '$inst/.env'"; issues=$((issues+1)); fi
    if grep -q '^CLAUDE_CODE_OAUTH_TOKEN=' "$inst/.env"; then echo "  ✓ cc-token в .env"
    else
      echo "  ! нет CLAUDE_CODE_OAUTH_TOKEN — упадёт в 401, когда ambient-auth протухнет"
      echo "      → 'claude setup-token' под $RUN_USER, затем deploy.sh --cc-token <TOKEN>"
      issues=$((issues+1)); fi
  else
    echo "  ✗ .env MISSING  → передеплой deploy.sh"; issues=$((issues+1))
  fi

  local svc; svc="$(_svc_state "$name")"
  case "$svc" in
    active) echo "  ✓ systemd: active" ;;
    absent) echo "  ✗ systemd юнит absent  → deploy.sh ставит+enable (нужен root/sudo)"; issues=$((issues+1)) ;;
    *)      echo "  ! systemd: $svc  → $([ "$(id -u)" -ne 0 ] && echo 'sudo ')systemctl status agent-tg@$name"; issues=$((issues+1)) ;;
  esac

  local resume; resume="$(_resume_db "$name")"
  if [ "$resume" = "present" ]; then echo "  ✓ resume: state.db есть (сессии переживут restart/reboot)"
  else echo "  · resume: state.db ещё нет (появится после первого сообщения)"; fi

  local auth; auth="$(_health_marker "$name")"
  case "$auth" in
    ok)        echo "  ✓ auth: ok" ;;
    unknown)   echo "  · auth: не проверялся (маркер появится после первого сообщения боту)" ;;
    degraded*) echo "  ✗ auth: $auth  → токен протух: 'claude setup-token' + deploy.sh --cc-token"; issues=$((issues+1)) ;;
  esac

  echo "  --- последние логи (8 строк) ---"
  cmd_logs "$name" 8 | sed 's/^/      /'

  if [ "$issues" -eq 0 ]; then echo "==> verdict: HEALTHY"; exit 0
  else echo "==> verdict: $issues проблем(а) — следуй подсказкам →"; exit 1; fi
}

case "${1:-}" in
  list)   shift; cmd_list "$@" ;;
  status) shift; cmd_status "$@" ;;
  logs)   shift; cmd_logs "$@" ;;
  health) shift; cmd_health "$@" ;;
  doctor) shift; cmd_doctor "$@" ;;
  -h|--help|"") awk 'NR==1{next} /^set -euo pipefail/{exit} {sub(/^# ?/,"");print}' "$0" ;;
  *) echo "неизвестная команда: $1 (см. --help)" >&2; exit 1 ;;
esac
