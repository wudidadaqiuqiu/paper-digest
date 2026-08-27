# Paper Digest

邮箱论文整理 Agent — 读取谷歌学术邮件 → LLM 评分筛选 + 翻译 → 生成 HTML 摘要

## 文件清单

| 文件 | 作用 |
|------|------|
| `main.py` | 核心 pipeline：取邮件、LLM 评分、生成 HTML |
| `server.py` | HTTP 服务：导航页、手动触发、进度轮询、Basic Auth |
| `show_digest.py` | 将 HTML 摘要转为人类可读文本 |
| `start.sh` | 后台启动 server.py，自动加载 `.env` |
| `.env` | QQ/LLM 密钥（gitignored） |
| `config.yaml` | 邮箱、LLM、兴趣、server 配置 |
| `state.json` | 已处理邮件 UID（本地运行时状态） |
| `output/` | HTML 输出目录（gitignored） |

## Claude 需知

### 批量处理逻辑

- **每天只处理一批**：从最新未处理邮件开始，逐封累积论文
- 当论文数 ≥ `max_papers_per_run`（默认 15），**完成当前邮件**（含该邮件所有论文），然后处理这一批并停止
- 剩余未处理邮件留给明天
- 不足阈值也照常处理，有多少处理多少

### 状态追踪

- `state.json`：记录已处理的 IMAP UID（本地持久化，gitignored）
- 不依赖 IMAP 已读/未读标记
- UID 比序号稳定，邮箱生命周期内不变

### 密钥管理

- `.env`：`QQ_PASSWORD` + `LLM_API_KEY`（gitignored）
- `start.sh` 自动 source `.env`
- 不要显式写出密钥，用 `.env` 或环境变量

### 兴趣配置

`config.yaml` → `profile.interest`：无线光通信 + 光束指向（排除大气湍流）

### 启动

```bash
bash start.sh  # 后台运行 server.py
```

服务地址：`http://localhost:8080`，Basic Auth（用户名 admin，密码见 config.yaml）

## 代码流程（`run_pipeline`）

每次触发处理时按以下顺序执行：

```
1. load_state()              → 读 state.json，获取已处理邮件 UID 列表
2. connect_imap()            → 连 QQ 邮箱 IMAP
3. fetch_scholar_emails()    → UID 搜索所有 Scholar 邮件，过滤已处理，最新优先
4. 逐封累积论文              → 每封邮件解析 HTML 提取论文，加入 pending_papers
   ├─ 累计 ≥ max_papers_per_run（15）→ break，停止读邮件
   └─ 不足 15                   → 继续读下一封
5. dedup_papers()            → 按 URL 去重（同一篇论文可能出现在多封邮件）
6. process_papers()          → DeepSeek LLM 逐篇评分(1-10) + 中英翻译（耗时主要在这）
7. generate_html()           → 写入 output/index.html + output/digest-日期.html
8. save_state()              → 更新 state.json 标记已处理（HTML 成功后写，中途崩溃不丢论文）
```

## 测试

**触发一次处理**：
```bash
curl -u "admin:密码" -X POST http://localhost:8080/process
```

**看进度**：
```bash
curl -u "admin:密码" http://localhost:8080/progress
```

**看结果**：处理后打开 `output/index.html`，或浏览器访问服务地址。

**重置状态（从头重新处理所有邮件）**：删除 `state.json` 即可。

**空跑测试（不连邮箱不用 LLM）**：
```bash
python3 main.py --dry-run
```

### 通过 cc-connect 测试

当用户在飞书/微信等平台通过 cc-connect 要求测试时，按以下步骤：

1. **触发处理**：
   ```bash
   curl -u "admin:密码" -X POST http://localhost:8080/process
   ```

2. **发送结果**：处理完成后，用 `show_digest.py` 转成可读文本，以文件形式发送：
   ```bash
   python3 show_digest.py > /tmp/digest.txt && cc-connect send --file /tmp/digest.txt
   ```

3. **询问是否回退状态**：测试后 state.json 已更新（邮件标记为已处理），需要问用户：
   > 测试完成。state.json 已更新，需要回退吗？（回退后下次处理同一批邮件）

   如果用户要求回退，**state.json 和 HTML 必须同步回退**：
   ```bash
   # 如果之前已 commit（git 有记录），一起回退
   git checkout state.json output/index.html output/digest-*.html
   # 如果全新未 commit，删除 state.json，生成空占位页面
   rm state.json
   # 删除旧的 digest 文件，写入空 HTML
   rm -f output/digest-*.html
   python3 -c "
   from pathlib import Path; from datetime import datetime, timezone, timedelta
   tz = timezone(timedelta(hours=8))
   today = datetime.now(tz).strftime('%Y-%m-%d')
   out = Path('output'); out.mkdir(parents=True, exist_ok=True)
   empty = f'<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>论文摘要 — {today}</title></head><body><h1>论文摘要 — {today}</h1><p>暂无新论文</p></body></html>'
   (out / 'index.html').write_text(empty, encoding='utf-8')
   print('已重置')
   "
   ```
