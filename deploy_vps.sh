#!/bin/bash
# Run this directly on the VPS: bash /root/herald-v2/deploy_vps.sh
set -e

echo "=== HERALD V2 Production Deploy ==="
cd /root/herald-v2

echo "--- Git pull ---"
git pull origin main

echo "--- DB URI check and fix ---"
EXISTING=$(grep 'SUPABASE_DB_URI' /root/herald/.env 2>/dev/null | grep -v 'ASYNC' | head -1 | cut -d= -f2-)
if [ -n "$EXISTING" ]; then
    ASYNC_URI=$(echo "$EXISTING" | sed 's|postgresql://|postgresql+asyncpg://|')
    if ! grep -q 'SUPABASE_DB_URI_ASYNC' /root/herald-v2/.env 2>/dev/null; then
        echo "SUPABASE_DB_URI_ASYNC=$ASYNC_URI" >> /root/herald-v2/.env
        echo "Added SUPABASE_DB_URI_ASYNC to .env"
    else
        echo "SUPABASE_DB_URI_ASYNC already present"
    fi
else
    echo "WARNING: Could not find SUPABASE_DB_URI in /root/herald/.env"
fi

echo "--- Syntax check ---"
python3 -c 'import py_compile; py_compile.compile("app.py"); print("SYNTAX OK")'

echo "--- Restart pm2 ---"
pm2 stop herald-v2 2>/dev/null || true
pm2 delete herald-v2 2>/dev/null || true
pm2 start 'chainlit run app.py --host 0.0.0.0 --port 8002' --name herald-v2 --cwd /root/herald-v2
pm2 save

echo "--- Waiting 15s for startup ---"
sleep 15

echo "--- Recent logs ---"
pm2 logs herald-v2 --lines 30 --nostream

echo "--- HTTP check ---"
curl -s -o /dev/null -w 'HTTP status: %{http_code}\n' http://localhost:8002

echo "--- Run test suite ---"
python3 /root/herald-v2/tests/test_regressions.py -v 2>&1 | tail -5
python3 /root/herald-v2/tests/test_hardening.py -v 2>&1 | tail -5

echo "=== Deploy complete ==="
