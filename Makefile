# bash + pipefail: иначе `| tee` в canary вернул бы код tee и замаскировал падение pytest
# (в т.ч. CANARY_STRICT-выход) — тихий провал ровно там, где он недопустим.
SHELL := bash
.SHELLFLAGS := -o pipefail -c

.PHONY: install test lint fmt canary canary-remote canary-preflight

# ── VPS-прогон канареек ──────────────────────────────────────────────────────
# Канарейки гоняются на Linux-VPS: там живой claude CLI, токен подписки и те самые
# Linux-пути, на которых ломались скиллы (#268). На маке они бессмысленны.
#
# ВАЖНО (перепроверено по бинарнику CLI): Claude Code ОТКАЗЫВАЕТСЯ работать с
# --dangerously-skip-permissions от root. Канарейки идут с bypassPermissions, поэтому
# их НЕЛЬЗЯ гонять под root — нужен отдельный непривилегированный юзер. Он же даёт
# изоляцию от боевого ~/.claude (иначе setting_sources=['user'] подсосёт хуки/скиллы
# живого офиса, а SubagentStop-хук напишет канареечный мусор в agent-captures).
#
# CANARY_SSH — user@host НЕПРИВИЛЕГИРОВАННОГО юзера (НЕ root@…).
# У этого юзера должен быть свой auth: `claude setup-token` под ним, либо
# CLAUDE_CODE_OAUTH_TOKEN в его окружении. Токен НЕ передаём флагами (утечка в ps/history).
CANARY_SSH ?= canary@openclaw
VPS_DIR    ?= /home/canary/canary-unpacker

install:
	uv sync --extra dev

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

fmt:
	uv run ruff format .
	uv run ruff check --fix .

# Прогон НА самой машине (запускать НА VPS под непривилегированным юзером).
# CANARY_STRICT=1 — нет живого SDK → громкая ошибка, а не тихий зелёный skip
# (иначе «канареек не было» неотличимо от «все прошли» — провал критерия Фазы 0).
# Артефакт прогона кладём в docs/canaries/last-run.log (gitignored) — не только в скроллбэк.
canary: canary-preflight
	CANARY_STRICT=1 uv run pytest canaries -v -s -ra 2>&1 | tee docs/canaries/last-run.log

# Быстрый префлайт: не root, есть claude, есть auth. Падает ДО жжения подписки.
canary-preflight:
	@[ "$$(id -u)" != "0" ] || { echo "СТОП: канарейки под root не запустятся (bypass+root запрещён CLI). Заведи отдельного юзера."; exit 1; }
	@command -v claude >/dev/null 2>&1 || python3 -c "import claude_agent_sdk" 2>/dev/null || { echo "СТОП: не найден claude CLI (ни в PATH, ни встроенный в SDK)."; exit 1; }
	@[ -n "$$CLAUDE_CODE_OAUTH_TOKEN" ] || [ -f "$${CLAUDE_CONFIG_DIR:-$$HOME/.claude}/.credentials.json" ] || { echo "СТОП: нет auth (ни CLAUDE_CODE_OAUTH_TOKEN, ни credentials)."; exit 1; }

# Прогон С МАКА: доставить код на VPS и запустить там под юзером CANARY_SSH.
# Предохранитель VPS_DIR: только /root/canary-* — иначе rsync без --delete молча
# ЗАПИШЕТ поверх одноимённых файлов живого сервиса (напр. при опечатке VPS_DIR).
# exclude .env/.env.* — секрет не должен уехать на VPS и осесть там (нет --delete).
canary-remote:
	@case '$(VPS_DIR)' in /home/*/canary-*|/root/canary-*) ;; *) echo "VPS_DIR='$(VPS_DIR)' вне */canary-* — отказ (защита от записи поверх чужого)"; exit 1;; esac
	rsync -az --exclude '.venv' --exclude '.git' --exclude '__pycache__' \
		--exclude '.pytest_cache' --exclude '.mypy_cache' --exclude '.ruff_cache' \
		--exclude '.env' --exclude '.env.*' \
		./ $(CANARY_SSH):$(VPS_DIR)/
	ssh $(CANARY_SSH) 'cd $(VPS_DIR) && make canary'
