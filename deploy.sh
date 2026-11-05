#!/usr/bin/env bash
# Установка и обновление бота «Вектор Знаний» на Ubuntu 24.04.
#
# Первый запуск: ставит Docker, скачивает проект, спрашивает токен и запускает бота.
# Повторный запуск (или команда vektor-update): подтягивает свежую версию и перезапускает бота.
#
# Публичный репозиторий:
#   curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/deploy.sh | sudo REPO_URL=https://github.com/OWNER/REPO.git bash
# Приватный репозиторий — по SSH-адресу, доступ через deploy key (скрипт создаст ключ и подскажет):
#   sudo REPO_URL=git@github.com:OWNER/REPO.git ./deploy.sh
#
# Переменные окружения (все необязательные):
#   REPO_URL       адрес репозитория; при обновлении берётся из уже скачанного проекта
#   BRANCH         ветка; при обновлении — текущая ветка проекта, иначе main
#   APP_DIR        куда положить проект (/opt/vektor-znaniy)
#   MAX_BOT_TOKEN  токен бота, чтобы не вводить его вручную
#   AI             yes/no — поднимать ли локальную нейросеть (Ollama)
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/vektor-znaniy}"
DEFAULT_REPO="https://github.com/vvmvv26/Idea_Fiks_24_VektorZnaniy.git"
DEPLOY_KEY=/root/.ssh/vektor_znaniy_deploy
DEFAULT_MODEL="qwen2.5:3b-instruct"

say() { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }

# Ввод с клавиатуры работает и при запуске через «curl ... | sudo bash»
has_tty() { { : < /dev/tty; } 2>/dev/null; }

ask() {
    local prompt="$1" secret="${2:-}" answer=""
    has_tty || die "нет терминала для ввода; задайте нужные значения через переменные окружения"
    if [ -n "$secret" ]; then
        read -rsp "$prompt" answer < /dev/tty
        echo > /dev/tty
    else
        read -rp "$prompt" answer < /dev/tty
    fi
    printf '%s' "$answer"
}

mem_mb() { awk '/MemTotal/ {printf "%d", $2 / 1024}' /proc/meminfo; }

# 3b занимает в памяти ~2,4 ГБ: на сервере от 3,5 ГБ она помещается вместе с системой и ботом
pick_model() {
    if [ -n "${MODEL:-}" ]; then
        echo "$MODEL"
    elif [ "$(mem_mb)" -ge 3500 ]; then
        echo "qwen2.5:3b-instruct"
    else
        echo "qwen2.5:1.5b-instruct"
    fi
}

# модель из настроек бота, чтобы обновление не меняло выбор
configured_model() {
    local m
    m=$(sed -n 's/^LLM_MODEL=//p' bot/.env 2>/dev/null | tail -1)
    echo "${m:-$DEFAULT_MODEL}"
}

install_packages() {
    local need=()
    command -v git >/dev/null || need+=(git)
    command -v ssh-keygen >/dev/null || need+=(openssh-client)
    if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
        need+=(docker.io docker-compose-v2)
    fi
    if [ ${#need[@]} -gt 0 ]; then
        say "Устанавливаю: ${need[*]}"
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}" >/dev/null
    fi
    systemctl enable --now docker >/dev/null 2>&1 || true
}

# git@github.com:owner/repo.git -> https://github.com/owner/repo/settings/keys/new
deploy_keys_page() {
    local path="${1#git@github.com:}"
    printf 'https://github.com/%s/settings/keys/new' "${path%.git}"
}

setup_ssh_access() {
    if [ ! -f "$DEPLOY_KEY" ]; then
        mkdir -p /root/.ssh && chmod 700 /root/.ssh
        ssh-keygen -q -t ed25519 -N "" -f "$DEPLOY_KEY" -C "vektor-znaniy@$(hostname)"
    fi
    export GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

    until git ls-remote "$REPO_URL" HEAD >/dev/null 2>&1; do
        cat <<EOF

Серверу нужен доступ к приватному репозиторию. Добавьте ключ ниже в GitHub:
  $(deploy_keys_page "$REPO_URL")
  Title — любое (например, «сервер бота»), Key — строка ниже, галочку «Allow write access» НЕ ставить.

$(cat "$DEPLOY_KEY.pub")

EOF
        has_tty || die "доступа к $REPO_URL нет; добавьте ключ выше и запустите скрипт ещё раз"
        ask "Когда добавите ключ, нажмите Enter (Ctrl+C — выход)... " >/dev/null
    done
}

sync_code() {
    local current_branch=""
    if [ -d "$APP_DIR/.git" ]; then
        REPO_URL="${REPO_URL:-$(git -C "$APP_DIR" remote get-url origin)}"
        current_branch="$(git -C "$APP_DIR" rev-parse --abbrev-ref HEAD)"
    fi
    REPO_URL="${REPO_URL:-$DEFAULT_REPO}"
    BRANCH="${BRANCH:-${current_branch:-main}}"

    case "$REPO_URL" in
        git@*) setup_ssh_access ;;
        *)
            git ls-remote "$REPO_URL" HEAD >/dev/null 2>&1 \
                || die "нет доступа к $REPO_URL. Если репозиторий приватный, укажите SSH-адрес: REPO_URL=git@github.com:OWNER/REPO.git"
            ;;
    esac

    if [ -d "$APP_DIR/.git" ]; then
        say "Обновляю проект в $APP_DIR (ветка $BRANCH)"
        git -C "$APP_DIR" remote set-url origin "$REPO_URL"
        git -C "$APP_DIR" fetch -q origin "$BRANCH"
        # bot/.env и .env в .gitignore — токен и настройки при обновлении не трогаются
        git -C "$APP_DIR" checkout -q -B "$BRANCH" "origin/$BRANCH"
        git -C "$APP_DIR" reset -q --hard "origin/$BRANCH"
    else
        say "Скачиваю проект из $REPO_URL (ветка $BRANCH)"
        git clone -q --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
    fi
    if [ -n "${GIT_SSH_COMMAND:-}" ]; then
        # чтобы и ручной git pull в папке проекта ходил с этим ключом
        git -C "$APP_DIR" config core.sshCommand "$GIT_SSH_COMMAND"
    fi
    [ -f "$APP_DIR/docker-compose.yml" ] || die "в ветке $BRANCH нет docker-compose.yml — проверьте REPO_URL и BRANCH"
}

configure() {
    if [ -f bot/.env ]; then
        say "Настройки уже есть (bot/.env), оставляю их. Ввести заново: sudo rm $APP_DIR/bot/.env и запустить ещё раз"
        return
    fi

    local token="${MAX_BOT_TOKEN:-}" ai="${AI:-}" mem model
    while [ -z "$token" ]; do
        token="$(ask 'Токен бота МАКС (ввод не отображается): ' secret)"
    done
    if [ -z "$ai" ]; then
        mem=$(mem_mb)
        echo "Локальная нейросеть отвечает прямо в боте. Ей нужно от 3,5 ГБ памяти и ~2 ГБ на диске; на сервере $(awk -v m="$mem" 'BEGIN {printf "%.1f", m / 1024}') ГБ ОЗУ."
        if [ "$mem" -lt 3500 ]; then
            echo "Памяти мало: нейросеть может не поместиться, лучше без неё."
        else
            echo "Будет установлена модель $(pick_model)."
        fi
        ai="$(ask 'Поднять нейросеть? [y/N]: ')"
    fi
    case "$ai" in
        y|Y|yes|да|Да) ai=yes ;;
        *) ai=no ;;
    esac

    say "Сохраняю настройки в $APP_DIR/bot/.env"
    (
        umask 077
        {
            echo "MAX_BOT_TOKEN=$token"
            if [ "$ai" = yes ]; then
                model=$(pick_model)
                echo "LLM_API_KEY=ollama"
                echo "LLM_MODEL=$model"
                # на небольшом сервере длинный ответ идёт пару минут, а запросы стоят в очереди по одному
                echo "LLM_TIMEOUT=300"
            else
                echo "LLM_API_KEY="
            fi
        } > bot/.env
        # docker compose сам читает этот файл: профиль ai включает контейнер с Ollama
        if [ "$ai" = yes ]; then echo "COMPOSE_PROFILES=ai" > .env; else : > .env; fi
    )
}

start() {
    say "Собираю и запускаю контейнеры"
    docker compose up -d --build --remove-orphans

    if grep -q '^COMPOSE_PROFILES=ai' .env 2>/dev/null; then
        local model
        model=$(configured_model)
        say "Проверяю модель $model (первый раз скачивается 1–2 ГБ)"
        for _ in $(seq 1 30); do
            docker compose exec -T ollama ollama list >/dev/null 2>&1 && break
            sleep 2
        done
        docker compose exec -T ollama ollama pull "$model" >/dev/null \
            || warn "модель не скачалась; бот пока работает без нейросети. Запустите обновление позже"
    fi
    docker image prune -f >/dev/null 2>&1 || true

    sleep 5
    say "Готово. Последние строки лога бота:"
    docker compose logs --tail 15 bot || true
}

install_update_command() {
    cat > /usr/local/bin/vektor-update <<EOF
#!/usr/bin/env bash
exec env APP_DIR="$APP_DIR" bash "$APP_DIR/deploy.sh" "\$@"
EOF
    chmod 755 /usr/local/bin/vektor-update
}

main() {
    [ "$(id -u)" -eq 0 ] || die "запустите через sudo"

    if [ -z "${VEKTOR_SYNCED:-}" ]; then
        install_packages
        sync_code
        # дальше работает уже свежая версия этого скрипта из репозитория
        export VEKTOR_SYNCED=1 APP_DIR REPO_URL BRANCH
        exec bash "$APP_DIR/deploy.sh"
    fi

    cd "$APP_DIR"
    configure
    start
    install_update_command
    cat <<EOF

Бот работает в фоне и сам поднимется после перезагрузки сервера.
  Обновить:    sudo vektor-update
  Логи:        cd $APP_DIR && sudo docker compose logs -f bot
  Остановить:  cd $APP_DIR && sudo docker compose down
EOF
}

# Всё тело — в функциях: bash прочитает скрипт целиком до запуска, и обновление
# файла через git во время работы ему не помешает.
main "$@"
