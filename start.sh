#!/bin/bash
cd "$(dirname "$0")"
source .env
nohup python3 server.py > server.log 2>&1 &
echo "Paper Digest 已后台启动, PID=$!"
echo "日志: server.log"
