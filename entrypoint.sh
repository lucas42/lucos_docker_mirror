#!/bin/sh
set -e

if [ -z "$REGISTRY_CLIENT_USERNAME" ] || [ -z "$REGISTRY_CLIENT_PASSWORD" ]; then
    echo "ERROR: REGISTRY_CLIENT_USERNAME and REGISTRY_CLIENT_PASSWORD must both be set" >&2
    exit 1
fi

htpasswd -Bbn "$REGISTRY_CLIENT_USERNAME" "$REGISTRY_CLIENT_PASSWORD" > /etc/nginx/.htpasswd

envsubst '${PORT}' < /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf

# Replace the symlink nginx:alpine ships at /var/log/nginx/access.log (→ /dev/stdout)
# with nothing, so nginx creates a real regular file when it first opens the path.
# Leave error.log symlink intact so 'docker logs' continues to capture nginx errors.
rm -f /var/log/nginx/access.log

exec nginx -g "daemon off;"
