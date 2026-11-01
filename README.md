# Проект для разбора расширения ansible с помощью python-модулей (Ansiballz)

Автор: Дмитрий Лемешонок

ansible-core==2.21.5
Ссылка на репозиторий проекта [вот](https://github.com/ansible/ansible/tree/v2.21.5)

В рамках проекта планируется реализовать кастомный python модуль для безопасного деплоя приложений через ansible и потенциально разных дополнительных фич (aka свистелок) поверх заявленного для демонстрации невероятных возможностей ansible. Прикладной интерес невероятный, я бы использовал эту штуку для деплоя сервисов на свои машины вместо того, чтобы каждый раз кастомно прописывать rescue в ansible.

## Запуск

Скорее всего у вас нет необходимого свободного линукс хоста, поэтому можете смело скипать шаг и переходить к [локальному стенду](#локально-в-docker-linux-и-windows), где есть два линукс-контейнера (контроллер и managed). Локальный стенд придуман нейронкой, так никто в разработке не делает, но это не требуе никаких виртуалок от вас.

Все команды выполняются из корня проекта. `USER@HOST` нужно заменить на
пользователя и адрес своего сервера.

### Удалённый хост: первоначальная подготовка

Нужен выделенный Ubuntu/Debian-хост с SSH, Python, systemd и sudo.
Порт 80 должен быть свободен. Один раз передайте demo и установите приложения
(команды работают из Linux/WSL или PowerShell с OpenSSH):

```bash
ssh USER@HOST python3 --version
scp -r demo USER@HOST:~/safe-release-demo
ssh -t USER@HOST 'sudo bash ~/safe-release-demo/install.sh && sudo bash ~/safe-release-demo/blue-green/install.sh'
```

Используйте новый каталог `safe-release-demo`: если он уже существует,
scp может создать вложенный каталог. Установщик blue-green предназначен
для первой подготовки; дальнейшие обновления выполняются playbook.

### Linux: запуск playbook на удалённом хосте

На управляющей машине нужен Python 3.12–3.14. Для Ubuntu/Debian сначала
установите `python3-venv` и `openssh-client`, если их ещё нет.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export ANSIBLE_CONFIG="$PWD/ansible.cfg"
cp inventory/hosts.example.yml inventory/hosts.yml
```

В `inventory/hosts.yml` укажите адрес, пользователя и путь к SSH-ключу.
Затем:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/minimal.yml
ansible-playbook -i inventory/hosts.yml playbooks/blue_green.yml -e target_release=v2
```

Для прогноза добавьте `--check`. Если sudo требует пароль, добавьте `-K`.
Локальный inventory и приватные ключи не следует публиковать.

### Windows: запуск на удалённом хосте через WSL

Управляющий Ansible запускается в Linux, поэтому используйте Ubuntu в WSL.
Если WSL ещё не установлен, выполните в PowerShell от администратора:

```powershell
wsl --install -d Ubuntu
```

После установки и, если потребуется, перезагрузки откройте Ubuntu, перейдите
в каталог проекта и выполните команды из раздела **Linux**. Для проекта
на диске C путь в WSL выглядит так: `/mnt/c/Users/ИМЯ/путь/к/проекту`.
SSH-ключ и подключение должны работать внутри WSL. Если сеть доступна только
Windows OpenSSH, используйте [описанный обходной вариант](docs/run.md)
и `inventory/windows-ssh.example.yml`.

Для просмотра удалённых приложений откройте туннель в отдельном терминале:

```bash
ssh -N -L 18080:127.0.0.1:8080 -L 18081:127.0.0.1:8081 USER@HOST
```

Страницы: [обычный режим](http://localhost:18080) и
[blue-green](http://localhost:18081). Терминал с туннелем оставьте работающим.

### Локально в Docker: Linux и Windows

Нужны Docker Engine либо Docker Desktop с **Linux containers** и Compose v2.
Команды одинаковы для Linux-терминала и PowerShell. Python, Ansible, WSL
и внешний сервер для этого варианта не нужны.

```bash
docker compose up -d --build --wait managed
docker compose run --rm control playbooks/docker_setup.yml
docker compose run --rm control playbooks/release.yml -e target_release=v2
docker compose run --rm control playbooks/blue_green.yml -e target_release=v2
docker compose run --rm control playbooks/blue_green.yml -e target_release=v1
docker compose run --rm control playbooks/blue_green.yml -e target_release=broken
```

Обычный режим: [localhost:18082](http://localhost:18082).
Blue-green: [localhost:18083](http://localhost:18083).

Для прогноза добавьте `--check`.

Остановка без потери состояния: `docker compose stop managed`;
продолжение: `docker compose start managed`.
Удаление контейнеров: `docker compose down` — после следующего up нужно
повторить setup. Полный сброс с удалением ключа и кэша внутренних образов:
`docker compose down --volumes`.

Управляемый контейнер использует privileged и собственный Docker daemon;
это локальная учебная среда. Сокет Docker компьютера не подключается.
