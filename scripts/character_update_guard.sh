#!/usr/bin/env bash
# Read-only, fail-closed gate: the legacy updater cannot back up multiple DBs.
# Takes the updater's already selected compose command as one argument.
set -uo pipefail
compose=$1
case "$compose" in 'docker compose'|'docker-compose') ;; *) exit 2 ;; esac
blocked() {
    echo '多角色数据库不能由单库更新脚本安全备份。请按 docs/character-isolation.md 停写、备份全部角色库并手工升级。' >&2
    exit 42
}
unknown() {
    echo '无法验证多角色备份范围，已停止更新；请启动数据库并检查 Compose 配置后重试。' >&2
    exit 2
}
# Effective Compose configuration includes environment overrides and .env values.
resolved="$($compose config 2>/dev/null)" || unknown
if printf '%s\n' "$resolved" | tr -d "\"'" | grep -Ei '^[[:space:]]+KIWI_CHARACTER_ISOLATION:[[:space:]]*true[[:space:]]*$' >/dev/null; then
    blocked
fi
# Configuration may have just been disabled while the existing service still runs.
gateway="$($compose ps -q kiwi-mem 2>/dev/null)" || unknown
if [ -n "$gateway" ]; then
    runtime_env="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$gateway" 2>/dev/null)" || unknown
    enabled="$(printf '%s\n' "$runtime_env" | grep -i '^KIWI_CHARACTER_ISOLATION=true$' || true)"
    [ -z "$enabled" ] || blocked
fi
# Keep archived role databases in scope even after disabling the feature.
exists="$($compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -X -A -t -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT to_regclass('\''public.kiwi_characters'\'') IS NOT NULL"' 2>/dev/null)" || unknown
case "${exists//$'\r'/}" in
    f) exit 0 ;;
    t) ;;
    *) unknown ;;
esac
count="$($compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -X -A -t -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM kiwi_characters WHERE id <> '\''default'\''"' 2>/dev/null)" || unknown
count="${count//$'\r'/}"
[[ "$count" =~ ^[0-9]+$ ]] || unknown
[ "$count" = 0 ] || blocked
