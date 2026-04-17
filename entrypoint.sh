#!/bin/sh
set -e

if [ -z "$REGISTRY_CLIENT_USERNAME" ] || [ -z "$REGISTRY_CLIENT_PASSWORD" ]; then
    echo "ERROR: REGISTRY_CLIENT_USERNAME and REGISTRY_CLIENT_PASSWORD must both be set" >&2
    exit 1
fi

htpasswd -Bbn "$REGISTRY_CLIENT_USERNAME" "$REGISTRY_CLIENT_PASSWORD" > /etc/nginx/.htpasswd

envsubst '${PORT}' < /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf

exec nginx -g "daemon off;"
