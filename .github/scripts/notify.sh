#!/bin/bash
# Sends $TEXT to every allowed Telegram chat.
set -euo pipefail

for chat_id in ${CHAT_IDS//,/ }; do
    curl -fsS "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${chat_id}" \
        --data-urlencode "text=${TEXT}" > /dev/null
done
