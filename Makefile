.PHONY: up down logs build migrate migration seed test test-unit psql shell ps

up:            ## build + start the stack
	docker compose up -d --build

down:
	docker compose down

ps:
	docker compose ps

logs:
	docker compose logs -f api poller worker

build:
	docker compose build

migrate:
	docker compose run --rm migrate

migration:     ## make migration msg="add x"
	docker compose run --rm api alembic revision --autogenerate -m "$(msg)"

seed:
	docker compose run --rm api python -m horizon.seed

test:
	docker compose run --rm api pytest -q

test-unit:
	pytest -q -m "not integration"

psql:
	docker compose exec postgres psql -U $${POSTGRES_USER:-horizon} -d $${POSTGRES_DB:-horizon}

shell:
	docker compose run --rm api python
