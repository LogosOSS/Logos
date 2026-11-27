#!/bin/sh
set -eu

case "${LOGOS_AGENT_MODEL_GATEWAY_MODE:-local}" in
    local) ;;
    remote)
        cp /etc/nginx/remote-model-gateway.conf.template /etc/nginx/templates/default.conf.template
        mkdir -p /etc/nginx/templates/includes /etc/nginx/conf.d/includes
        cp /etc/nginx/remote-model-proxy.conf.template /etc/nginx/templates/includes/remote-model-proxy.conf.template
        ;;
    *)
        echo "Unrecognized model gateway mode" >&2
        exit 1
        ;;
esac
