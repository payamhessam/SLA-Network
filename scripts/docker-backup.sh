#!/bin/sh
set -eu
mkdir -p backups
stamp=$(date +%Y%m%d-%H%M%S)
docker compose exec -T postgres pg_dump -U network_sla -Fc network_sla > "backups/network-sla-$stamp.dump"
