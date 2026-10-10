# Разработка SimpleTorrent

## Устройство

Браузер обращается к FastAPI по HTTP. FastAPI управляет Transmission через приватный JSON RPC. Два процесса видят один и тот же каталог `/data`; файлы уходят в браузер напрямую из FastAPI, без загрузки всего содержимого в память.

| Часть | Ответственность |
| --- | --- |
| `app/main.py` | Маршруты, резервирование места, монитор диска, HTTP Range, потоковый ZIP |
| `app/engine.py` | Асинхронный RPC, Basic Auth, повтор запроса после 409 с новым session ID |
| `app/metainfo.py` | Ограниченный bencode-парсер, проверка структуры, имён и объёмов |
| `app/security.py` | Подписанные cookies, срок сессии, CSRF, проверка Origin, ограничение входов |
| `app/static/` | Русский интерфейс без сборки, внешних зависимостей и демонстрационных данных |
| `scripts/transmission.py` | Генерация настроек движка с сохранением пользовательских лимитов |
| `scripts/setup.py` | Безопасное первоначальное создание `.env` |

Очередь и метаданные принадлежат Transmission. Собственной БД нет. Каждый торрент получает каталог `downloads/<SHA-1 info dictionary>`, поэтому одинаковые имена из разных торрентов не пересекаются. Операции добавления, продолжения и удаления сериализуются внутри одного API-процесса. **Запускайте один worker**: блокировка, ограничение попыток входа и счётчик активных передач находятся в памяти.

Добавление сначала валидирует `.torrent` и резервирует свободное место с учётом всех оставшихся объёмов. Движок получает торрент на паузе, затем API запускает его. Если запуск не удался, запись остаётся видимой на паузе. Вручную добавленные в движок торренты вне управляемых папок не отображаются и не могут быть удалены через API.

Сессия действует 7 дней. Cookie содержит только подписанные срок и CSRF-токен, имеет `HttpOnly` и `SameSite=Strict`. Для изменений нужен заголовок `X-CSRF-Token`. Неверные входы ограничены восемью попытками за 5 минут с IP; счётчик сбрасывается после рестарта. Это личный однопользовательский сервис, без управления аккаунтами. Logout удаляет cookie в текущем браузере; для отзыва всех подписанных сессий замените `ST_SESSION_SECRET`.

Ссылки на скачивание также требуют cookie. Проверяется готовность и фактический размер файла; запрещены пути за пределами управляемой папки и символические ссылки. Отдельный файл открывается с `O_NOFOLLOW` и передаётся блоками до 1 МиБ. Есть одинарные Range-запросы, включая суффиксные. ZIP хранит файлы без сжатия и создаётся на лету. Пока HTTP-передача активна, удаление этого торрента возвращает 409.

## API

Все маршруты `/api/*`, кроме login, требуют сессию. Все методы изменения дополнительно требуют `X-CSRF-Token` из login/session. Ошибки возвращают `{ "detail": "…" }`.

| Метод | Маршрут | Назначение |
| --- | --- | --- |
| POST | `/api/login` | JSON `{password}` → cookie и `{csrf}` |
| GET | `/api/session` | Проверка сессии, текущий CSRF |
| POST | `/api/logout` | Удаление cookie |
| GET | `/api/torrents` | Список, суммарные скорости и состояние хранилища |
| POST | `/api/torrents` | Multipart, поле `file` с `.torrent`; ответ 201 `{id,name}` |
| POST | `/api/torrents/{hash}/pause` | Приостановить |
| POST | `/api/torrents/{hash}/resume` | Продолжить после проверки места |
| DELETE | `/api/torrents/{hash}?delete_data=false` | Удалить запись, опционально содержимое |
| GET | `/api/torrents/{hash}/files` | Список файлов и их готовность |
| GET | `/api/torrents/{hash}/files/{index}/download` | Готовый файл; 200/206, 416 для недопустимого Range |
| GET | `/api/torrents/{hash}/archive` | Вся готовая загрузка ZIP-архивом |
| GET | `/api/settings` | Лимиты и версия движка |
| PUT | `/api/settings` | JSON `{download_kib,upload_kib}`; 0 = без ограничения |
| GET | `/healthz` | 200, если API может обратиться к движку; иначе 503 |

## Без Docker

Нужны Python 3.12+ и Transmission 3.x/4.x. Для локальной разработки можно запустить RPC на loopback и использовать временный каталог:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
mkdir -p data/downloads data/config

# Задайте секреты в текущем терминале, не коммитьте их.
export ST_PASSWORD='your-private-dev-password'
export ST_SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export ST_RPC_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
export ST_DOWNLOAD_DIR="$PWD/data/downloads"
export ST_ENGINE_CONFIG="$PWD/data/config"
export ST_RPC_URL=http://127.0.0.1:9091/transmission/rpc
export ST_RPC_USERNAME=simpletorrent

python scripts/transmission.py
# В другом терминале с теми же переменными:
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8080 --no-proxy-headers
```

Скрипт движка предназначен прежде всего для изолированной Docker-сети и слушает RPC на `0.0.0.0`. При нативном запуске ограничьте 9091 файрволом или измените `rpc-bind-address` в скрипте на `127.0.0.1`. Не используйте один каталог конфигурации сразу в двух daemon-процессах.

## HTTPS-прокси

Для внешнего доступа завершайте TLS на доверенном reverse proxy. В `.env` включите `ST_SECURE_COOKIE=true`. Прокси должен сохранять исходный Host и передавать корректный протокол; Origin-проверка использует внешний scheme и Host.

По умолчанию API игнорирует proxy headers. За доверенным прокси переопределите command сервиса `app` в Compose: уберите `--no-proxy-headers`, добавьте `--proxy-headers --forwarded-allow-ips=<IP-прокси>`. Не доверяйте `*`, если к backend могут подключаться посторонние. Ограничьте HTTP-порт на сервере для прокси и настройте его таймауты под длительные передачи. RPC-порт не публикуется.

## Проверки

Тесты API используют отдельные временные каталоги. Интеграционный тест запускает настоящий daemon, отдаёт свой тестовый контент через loopback-webseed, сверяет скачанные байты и ZIP, перезапускает движок и проверяет сохранённую очередь. Публичная P2P-сеть для теста не нужна.

```bash
pytest -q
ST_INTEGRATION=1 pytest -q tests/test_integration.py
node --check app/static/app.js
python -m compileall -q app scripts
```

Контейнерные сборки и проверка `docker compose up --wait` выполняются в CI. Тесты на настоящей Raspberry Pi нужны для оценки скорости SSD/Wi-Fi, поведения конкретного накопителя и длительных загрузок.
