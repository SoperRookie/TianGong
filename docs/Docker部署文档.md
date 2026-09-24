# 天工 Docker 部署文档

适用版本：v1.0.0。用 `docker compose` 在一台 Linux 服务器上拉起 MySQL、应用、Nginx 三个容器。传统裸机部署见 `docs/服务器部署文档.md`，两者的环境变量、数据迁移与运维事项一致，本文只写 Docker 相关部分。

## 1. 架构

```
浏览器 ──80/443──▶ nginx 容器（固定 IP 172.28.0.10）──▶ app 容器 :8000 ──▶ mysql 容器 :3306
                                                        │
                                                        ├─ ./outputs        上传原件 / 导出 / 执行附件 / 实例锁
                                                        ├─ ./data/knowledge 嵌入式向量库
                                                        ├─ ./logs
                                                        └─ ./config/models.yaml（目录挂载，页面可改）
                                                        │
                                                        └──▶ 公网 OpenAI API（text-embedding-3-large Embedding）
                                                        └──▶ 公网 OpenAI API（GPT；其他厂商可配）
```

设计要点：

- **应用只跑一个容器、一个进程。** 用户 / 项目 / 任务是进程内内存态加数据库回写，启动时在 `outputs/.instance.lock` 加文件锁，`docker compose up --scale app=2` 或加 `--workers` 都会被拒绝。
- **有状态数据全部在宿主机目录或命名卷**：`mysql-data` 卷、`./outputs`、`./data`。镜像可随时重建，数据不丢。
- **Nginx 固定 IP。** 应用只采信来自 `TIANGONG_TRUSTED_PROXIES` 所列地址的 `X-Forwarded-For`，且按精确 IP 匹配，所以 compose 里给 nginx 容器固定 `172.28.0.10`，操作日志才能记录真实客户端 IP。
- **模型配置目录挂载**：`config/models.yaml` 在宿主机，页面「模型配置」保存后热重载；手工改文件则重启 app 容器，不用重建镜像。挂目录而非单文件，是因为保存时原子替换文件，单文件绑定挂载会失败。
- Redis 出现在依赖中但 v1.0.0 不需要，compose 里没有它。

相关文件：

| 文件 | 作用 |
|---|---|
| `Dockerfile` | 应用镜像，`python:3.12-slim`，非 root 用户运行，内置健康检查 |
| `.dockerignore` | 密钥、运行产物、文档、测试不进镜像 |
| `docker-compose.yml` | 三个服务、卷、固定网段 |
| `docker/nginx.conf` | 反向代理，含 HTTPS 示例 |
| `.env.example` | 环境变量样例，末尾有 Docker 专用段 |

## 2. 服务器准备

- Linux，Docker Engine 24 以上，Docker Compose v2（`docker compose version` 能输出即可）。
- 出网访问 `https://api.openai.com`（LLM 与 Embedding 同用）。
- 端口 80（及 443）未被占用。

## 3. 部署步骤

### 3.1 获取代码

```bash
sudo mkdir -p /opt/tiangong && sudo chown $USER /opt/tiangong
cd /opt/tiangong
git clone <仓库地址> .
git checkout v1.0.0
```

### 3.2 写 `.env`

```bash
cp .env.example .env
chmod 600 .env
```

至少填写：

```bash
OPENAI_API_KEY=sk-xxxx
TIANGONG_ADMIN_PASSWORD=<初始管理员口令>

MYSQL_ROOT_PASSWORD=<强密码>
MYSQL_PASSWORD=<强密码>
# HTTP_PORT=80
# TZ=Asia/Shanghai
```

不用填 `TIANGONG_DB_URL` 和 `TIANGONG_TRUSTED_PROXIES`，compose 文件已按容器网络写死。`MYSQL_PASSWORD` 会被拼进连接串，密码含 `@ # / % ?` 等符号也可以，应用会自动编码；但 compose 自身对 `$` 有特殊解释，密码里避免用 `$`。

### 3.3 模型配置

LLM 模型不预置：首次启动在挂载的 `config/` 目录里自动生成 `models.yaml`（模型清单为空），登录后在「系统设置 → 模型配置」添加，第一个模型自动成为默认。新模型的密钥写进 `.env` 后 `docker compose up -d app` 重建容器才会读到。

Embedding 段从 `config/models.example.yaml` 复制，默认为 OpenAI text-embedding-3-large，与 LLM 共用 `OPENAI_API_KEY`，无需内网服务。改接宿主机上的 Ollama 时把 `models.yaml` 里 `embeddings` 的 `base_url` 改为：

```yaml
base_url: http://host.docker.internal:11434/v1
```

compose 已通过 `extra_hosts` 把 `host.docker.internal` 指到宿主机。页面添加内网 Ollama 模型时 Base URL 同理。

### 3.4 创建数据目录并启动

```bash
mkdir -p outputs data/knowledge logs backups
sudo chown -R 1000:1000 outputs data logs     # 容器内用户 uid 1000
docker compose up -d --build
docker compose ps
docker compose logs -f app
```

首次启动 MySQL 初始化约半分钟，app 容器等 mysql 健康检查通过后才启动。app 日志出现「数据库已连接」「服务启动」即完成建表。

### 3.5 验证

```bash
curl -s http://127.0.0.1/health                      # {"status":"ok"}
docker compose exec mysql mysql -utiangong -p"$MYSQL_PASSWORD" tiangong -e 'SHOW TABLES'
```

浏览器打开 `http://<服务器IP>`，用 `admin` 与 `.env` 中的口令登录。

## 4. 从本地环境迁移数据

在开发机导出（应用可运行状态）：

```bash
TIANGONG_DB_URL=<本地库连接串> scripts/migrate_prod.sh   # backups/tiangong-<时间>.sql.gz
tar czf outputs.tgz outputs
tar czf knowledge.tgz data/knowledge
```

在服务器，先只启动数据库并导入，再解开目录，最后启动全部：

```bash
cd /opt/tiangong
docker compose up -d mysql
gunzip -c tiangong-<时间>.sql.gz | docker compose exec -T mysql mysql -uroot -p"$MYSQL_ROOT_PASSWORD" tiangong
tar xzf outputs.tgz && tar xzf knowledge.tgz
rm -f outputs/.instance.lock
sudo chown -R 1000:1000 outputs data
docker compose up -d --build
```

启动日志中的「M4 执行迁移」「M2 需求迁移」是旧任务制数据的自动迁移，幂等。之后以管理员登录，在 项目管理 →（未指定）把遗留任务归属项目。

## 5. 备份与恢复

备份脚本 `backups/daily.sh`（crontab `0 2 * * *`）：

```bash
#!/usr/bin/env bash
set -e
cd /opt/tiangong
set -a; . ./.env; set +a
D=backups/$(date +%F); mkdir -p "$D"
docker compose exec -T mysql mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --default-character-set=utf8mb4 tiangong | gzip > "$D/db.sql.gz"
tar czf "$D/outputs.tgz" outputs --exclude=.instance.lock
tar czf "$D/knowledge.tgz" data/knowledge
find backups -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +
```

恢复：`docker compose stop app`，按第 4 节导入数据库并解开目录，`docker compose start app`。

## 6. 升级

```bash
cd /opt/tiangong
bash backups/daily.sh                  # 先备份
git fetch --tags && git checkout v1.0.1
docker compose up -d --build app       # 只重建应用镜像，mysql / nginx 不动
docker compose logs -f app
```

表结构变更与数据迁移在应用启动时自动执行。回滚：切回旧标签重新 `up -d --build app`，必要时用备份恢复数据库。

## 7. 日常运维

| 事项 | 命令 |
|---|---|
| 状态与健康检查 | `docker compose ps`（app 显示 healthy） |
| 应用日志 | `docker compose logs -f app`，或宿主机 `logs/tiangong_<日期>.log` |
| 重启应用 | `docker compose restart app` |
| 改模型配置后 | 页面「模型配置」保存即生效；手工编辑 `config/models.yaml` 后 `docker compose restart app`；新模型的密钥先写入 `.env` 再 `docker compose up -d app` |
| 改 `.env` 后 | `docker compose up -d app`（重建容器才会读取新环境变量） |
| 进入容器 | `docker compose exec app bash` |
| 数据库客户端 | `docker compose exec mysql mysql -utiangong -p tiangong` |
| 直连不经 Nginx | 在 compose 的 app 服务打开 `ports: "8000:8000"`，应用会把直连地址记为客户端 IP |
| 启用 HTTPS | 证书放 `docker/certs/`，打开 compose 的 443 端口与 certs 挂载，启用 `docker/nginx.conf` 的 443 块 |

## 8. 常见问题

**app 反复重启，日志「检测到另一个天工实例」。** `outputs/.instance.lock` 被另一进程持有，通常是宿主机上还跑着裸机版服务，或误起了第二个容器。停掉后 `docker compose restart app`。

**app 启动报权限错误（Permission denied）。** 宿主机目录属主不是 uid 1000，执行 `sudo chown -R 1000:1000 outputs data logs`。

**知识库上传或检索报错。** 多为 `OPENAI_API_KEY` 未配或容器出网受限：`docker compose exec app python -c "import urllib.request;print(urllib.request.urlopen('https://api.openai.com/v1/models',timeout=5).status)"` 验证出网（401 也算通）；若 `embeddings` 段改接了内网 Ollama，则按第 3.3 节核对地址。

**操作日志 IP 都是 172.28.0.10。** nginx 容器没有拿到固定 IP（例如手工改了网段），确认 `docker inspect tiangong-nginx` 的 IP 与 `TIANGONG_TRUSTED_PROXIES` 一致。

**上传大附件 413。** `docker/nginx.conf` 的 `client_max_body_size`，改后 `docker compose restart nginx`。

**MySQL 容器一直 unhealthy。** 首次初始化未完成或 `MYSQL_ROOT_PASSWORD` 与已有 `mysql-data` 卷里的口令不一致。全新部署可 `docker compose down -v` 清卷重来；有数据时不要带 `-v`。

**换了 Embedding 模型后检索不到旧知识。** 向量维度变化，已入库内容需重新上传。Embedding 模型一经确定不要更换。
