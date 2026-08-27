# Paper Digest

邮箱论文整理 Agent — 读取谷歌学术邮件 → LLM 评分 + 翻译 → 生成 HTML 摘要。

详细逻辑见 `README.md`。

## 快速参考

- **启动**：`bash start.sh`
- **测试**：`python3 main.py --dry-run`
- **触发**：`curl -u admin:密码 -X POST http://localhost:8080/process`
- **重置**：`rm state.json`
