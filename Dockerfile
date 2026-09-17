# 天工应用镜像：单进程 uvicorn，代码以可编辑方式安装，保持 BASE_DIR=/app（outputs/ data/ logs/ config/ 相对路径不变）
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -u 1000 -m -d /app -s /usr/sbin/nologin tiangong

WORKDIR /app

# 先只装依赖（从 pyproject 读取），代码改动不重装依赖
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY README.md ./
COPY app ./app
COPY config ./config
COPY scripts ./scripts
RUN pip install --no-deps -e . \
    && mkdir -p outputs data/knowledge logs backups \
    && chown -R tiangong:tiangong /app

USER tiangong
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

# 只能单进程：不要加 --workers（进程内内存态 + 数据库回写，启动时有实例锁）。
# X-Forwarded-For 由应用按 TIANGONG_TRUSTED_PROXIES 自行采信，这里不开 uvicorn 的代理头解析。
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "10"]
