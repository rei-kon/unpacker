.PHONY: install test lint fmt canary canary-remote

# Канарейки гоняются на Linux-VPS: там живой claude CLI, токен подписки и те самые
# Linux-пути, на которых ломались скиллы (#268). На маке они бессмысленны.
VPS ?= openclaw
VPS_DIR ?= /root/canary-unpacker

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

# Прогон на самой машине (запускать НА VPS)
canary:
	uv run pytest canaries -v -s

# Прогон с мака: доставить код на VPS и запустить там.
# Без --delete осознанно: VPS_DIR переопределяем переменной, а rsync --delete
# в неверный каталог сносит чужое. Каталог изолированный, мусор не мешает.
canary-remote:
	rsync -az --exclude '.venv' --exclude '.git' --exclude '__pycache__' \
		--exclude '.pytest_cache' --exclude '.mypy_cache' --exclude '.ruff_cache' \
		./ $(VPS):$(VPS_DIR)/
	ssh $(VPS) 'cd $(VPS_DIR) && make canary'
