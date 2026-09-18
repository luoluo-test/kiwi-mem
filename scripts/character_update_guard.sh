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
identity_unknown() {
    echo '无法确认应用数据库与单库备份目标一致，已停止更新。自定义数据库连接请按 docs/character-isolation.md 手工备份升级。' >&2
    exit 2
}
# Read only Compose's canonical mapping layout. Unrecognized YAML is rejected,
# never evaluated as shell input. Credentials stay in variables, not diagnostics.
compose_env() {
    local value
    value="$(printf '%s\n' "$resolved" | awk -v service="$1" -v key="$2" '
        /^services:$/ {services=1; next}
        /^[^ ]/ {services=0}
        services && /^  [^ ]/ {selected=($0 == "  " service ":"); environment=0; next}
        selected && /^    [^ ]/ {environment=($0 == "    environment:"); next}
        selected && environment && index($0, "      " key ":") == 1 {
            count++; value=substr($0, length(key)+8); sub(/^[[:space:]]*/, "", value)
        }
        END {if (count != 1) exit 1; print value}
    ')" || return 1
    case "$value" in
        \"*\") value="${value:1:${#value}-2}" ;;
        \'*\') value="${value:1:${#value}-2}" ;;
    esac
    printf '%s\n' "$value"
}
runtime_value() {
    printf '%s\n' "$1" | awk -v key="$2" '
        index($0, key "=") == 1 {count++; value=substr($0, length(key)+2)}
        END {if (count != 1) exit 1; print value}'
}
# Effective Compose configuration includes environment overrides and .env values.
resolved="$($compose config 2>/dev/null)" || unknown
if printf '%s\n' "$resolved" | tr -d "\"'" | grep -Ei '^[[:space:]]+KIWI_CHARACTER_ISOLATION:[[:space:]]*true[[:space:]]*$' >/dev/null; then
    blocked
fi
# Configuration may have just been disabled while the existing service still runs.
gateway="$($compose ps -q kiwi-mem 2>/dev/null)" || unknown
runtime_env=""
if [ -n "$gateway" ]; then
    runtime_env="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$gateway" 2>/dev/null)" || unknown
    enabled="$(printf '%s\n' "$runtime_env" | grep -i '^KIWI_CHARACTER_ISOLATION=true$' || true)"
    [ -z "$enabled" ] || blocked
fi
# This updater supports only the standard db service URL with matching explicit
# POSTGRES_* values. Check both desired configuration and live backup container:
# switching a DATABASE_URL (or disabling isolation) must not hide retained roles.
db_user="$(compose_env db POSTGRES_USER)" || identity_unknown
db_password="$(compose_env db POSTGRES_PASSWORD)" || identity_unknown
db_name="$(compose_env db POSTGRES_DB)" || identity_unknown
for value in "$db_user" "$db_password" "$db_name"; do
    [[ "$value" =~ ^[A-Za-z0-9_.-]+$ ]] || identity_unknown
done
expected_url="postgresql://${db_user}:${db_password}@db:5432/${db_name}"
application_url="$(compose_env kiwi-mem DATABASE_URL)" || identity_unknown
[ "$application_url" = "$expected_url" ] || identity_unknown
database_container="$($compose ps -q db 2>/dev/null)" || identity_unknown
[ -n "$database_container" ] || identity_unknown
database_env="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$database_container" 2>/dev/null)" || identity_unknown
[ "$(runtime_value "$database_env" POSTGRES_USER)" = "$db_user" ] || identity_unknown
[ "$(runtime_value "$database_env" POSTGRES_PASSWORD)" = "$db_password" ] || identity_unknown
[ "$(runtime_value "$database_env" POSTGRES_DB)" = "$db_name" ] || identity_unknown
if [ -n "$gateway" ]; then
    application_url="$(runtime_value "$runtime_env" DATABASE_URL)" || identity_unknown
    [ "$application_url" = "$expected_url" ] || identity_unknown
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
