# Развёртывание стенда

1. `git clone` на сервер, `cp .env.example .env`, вписать ключ LLM, `chmod 600 .env`.
2. `docker compose -p iik-pm -f deploy/docker-compose.yml up -d --build`
3. Проверка: `curl -s 127.0.0.1:8091/health`
4. Reverse proxy с HTTPS: `deploy/Caddyfile.snippet` (Caddy) или `deploy/nginx.conf.example` (nginx + certbot); DNS A-запись поддомена на IP сервера.
5. Остановить: `docker compose -p iik-pm -f deploy/docker-compose.yml down` (`-v` — удалить и данные).

Альтернатива без Docker — `deploy/iik-pm.service.example` (systemd).
