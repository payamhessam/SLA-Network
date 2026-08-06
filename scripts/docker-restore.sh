#!/bin/sh
set -eu
test $# -eq 1 || { echo 'usage: docker-restore.sh BACKUP'; exit 2; }
backup=$(realpath "$1")
test -f "$backup" || { echo 'backup not found'; exit 2; }
docker compose exec -T postgres pg_restore -U network_sla -d network_sla --clean --if-exists < "$backup"

