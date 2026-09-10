# Docker 部署流程(目标盒, 假设「部署文件夹」已在盒子上)

> 本文件只讲**部署**。一个自包含的「部署文件夹」(见 PACKAGING.md),拷到目标盒子上,按本文跑起来。
> 打包在源环境做,见 [PACKAGING.md](PACKAGING.md)。
>
> 部署动作:装 Docker → 确认 NPU → `install.sh`(load 镜像 + compose up) → 验证。

## 0. 部署假设(以下已具备, 不再讲怎么传)

一个完整的「部署文件夹」已在盒子上, 结构如下(模型/前端都在文件夹里, 自包含):

```
saferag-1688/
├── saferag-images.tar.gz      三镜像(backend / qwen / nginx:alpine)
├── docker-compose.yml         挂载 ./models ./emergency-platform/frontend
├── install.sh                 一键部署
├── nginx/saferag.conf         与 compose 同目录
├── models/
│   ├── Qwen3_5/               两个 bmodel + config/(文件名与 compose 一致)
│   ├── bge-small-zh-v1.5/     embedding —— 必须叫 v1.5
│   └── bge-reranker-base/
└── emergency-platform/frontend/  前端(index.html)
```

**模型在文件夹里 `models/`, 已随文件夹走;不需要搬到 `/data2`。**
**前端在文件夹里 `emergency-platform/frontend/`, 同样已就位。**
**运行数据(库/知识库/上传文档)固定在宿主 `/data/SafeRAG/backend/data`**:新盒子首次 `compose up`
时 docker 会自动创建;旧盒子迁移可把 `backend/data` 内容放进文件夹的 `data/`, 由 install.sh 拷过去。

## 快路: 一键脚本

```bash
cd saferag-1688                 # 部署文件夹
sudo bash install.sh
```
install.sh 做:预检(NPU 节点自适应 / libsophon / tar / 模型齐全)→ `docker load` 三镜像 →
清旧容器 → **裸 `docker run` 起 4 个容器**(**不依赖 docker-compose**, 老 docker 19.x 或没装 compose
的盒子照样跑)→ 验证引擎。compose 文件保留为参考/可选。下面 §1-§5 是它的内部步骤, 想手工来就照做。

## 1. 装 Docker

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker $USER      # 当前用户加 docker 组; 重登一次生效
```
> 备选:`curl -fsSL https://get.docker.com | sudo sh`。验证:`docker version; docker compose version`

## 2. 容器运行时/store 按需调整

**根盘小要挪 data-root**(镜像会压爆小根盘):
```bash
df -h /        # 只剩几 G 就走这步
```
`/etc/systemd/system/docker.service.d/docker.conf`(先 `mkdir -p` 该目录):
```ini
[Service]
ExecStart=
ExecStart=/usr/bin/dockerd --data-root /data/docker -H fd:// --containerd=/run/containerd/containerd.sock
```

**走代理**(拉镜像是 daemon 的活, 代理要写进 daemon, 不是 shell):
`/etc/systemd/system/docker.service.d/http-proxy.conf`:
```ini
[Service]
Environment="HTTP_PROXY=http://192.168.10.107:7897"
Environment="HTTPS_PROXY=http://192.168.10.107:7897"
Environment="NO_PROXY=localhost,127.0.0.1"
```
两者改完都:
```bash
sudo systemctl daemon-reload && sudo systemctl restart docker
```

## 3. 确认 NPU(引擎容器前提)

```bash
ls -l /dev/bm-tpu0 /dev/bmdev-ctl /dev/ion    # 设备节点随板卡而异, 以实际 ls /dev/bm* 为准
/opt/sophon/libsophon-current/bin/bm-smi      # 能看到设备即可
ls /opt/sophon/libsophon-current/lib           # 引擎容器要挂载这个目录(libbmrt.so.1.0 等)
```
带 BSP 的 Sophon 板子一般自带;没有就先装 Sophon SDK 的 driver + libsophon(两者配对)。
> **节点不是固定的**:BM1688 有 `/dev/bm-tpu0`;BM1684X 有的板也有 `/dev/bm-tpu0`(实测一台有),
> 有的只有 `bmdev-ctl + ion`。`install.sh` 会**按实际存在的节点自动透传**,缺哪个只跳过哪个;
> 手工用 compose 时若某板缺某节点, 删掉 compose 里对应 `devices:` 行即可。

## 4. 载入镜像(手工版, install.sh 的这一步)

```bash
cd <部署文件夹>
gunzip -c saferag-images.tar.gz | docker load      # 或 docker load -i saferag-images.tar.gz
docker images | grep saferag   # saferag-backend / saferag-qwen
docker images | grep nginx     # ⚠ 必须也有 nginx:alpine(没有 → 重新打三镜像 tar)
```

## 5. 起服务(手工版)

> `install.sh` 已用**裸 docker run** 起容器(不依赖 compose)。以下 compose 是参考方式;
> 若用 compose, 需盒子装了 `docker compose` 或 `docker-compose`, 且从部署文件夹里跑(相对路径挂载)。

```bash
cd <部署文件夹>
ss -lntp | grep -E ':80 |:8000|:8001|:8081' || echo OK-free   # 全新盒一般空; 有的板自带 nginx 占 80, 先停
docker compose up -d
docker compose ps        # 期望 4 个容器全部 Up
```
> compose 是相对路径挂载:`./models` → `/models:ro`、`./emergency-platform/frontend` → `/usr/share/nginx/html:ro`,
> 所以**一定要从部署文件夹里跑 compose**(install.sh 已经帮你 cd 好了)。

## 6. 验证

```bash
curl -s http://127.0.0.1/            # 前端首页 200
curl -s http://127.0.0.1/docs        # 后端 API 文档 200
curl -s http://127.0.0.1:8000/health # 引擎 4B → {"status":"ok",...}
curl -s http://127.0.0.1:8001/health # 引擎 2B(加载需 ~1 分钟)
curl -s http://127.0.0.1/v1/models   # 经 nginx 反代 4B 模型列表
# 真实生成一把:
curl -s http://127.0.0.1:8001/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"tpu-qwen3.5-2B","messages":[{"role":"user","content":"你好"}],"max_tokens":20}'
```

## 7. 从旧盒子迁来时的特别检查(实测踩过)

旧盒子原来用 systemd 跑 `qwen/qwen_chat/saferag/nginx`,`systemctl stop` **可能杀不干净**:
- `Restart=always` 的 unit 会一边失败一边被拉起重启(宿主进程继续占端口/NPU)
- 表现:容器 `qwen-doc` 反复重启、`init` assert、或容器日志报 "address already in use"

切换后必须核对:

```bash
systemctl is-active qwen qwen_chat saferag nginx   # 全部 inactive
ps -eo pid,args | grep -E 'server.py -m|uvicorn|nginx: master' | grep -v grep
# 应该只剩容器进程(命令里是 /models 路径); 残留宿主进程都揪出来停掉
# 宿主引擎命令是 /data2/models 路径, 容器的是 /models, 一眼可辨
```

若还有 `Restart=always` 的 unit 在重生产物:`sudo systemctl stop qwen_chat && sudo systemctl reset-failed qwen_chat`
并确认 `systemctl show qwen_chat -p ActiveState,MainPID` 变 `inactive`/`0`。

## 8. 回滚 / 卸载

```bash
cd <部署文件夹> && docker compose down      # 停容器(运行数据在宿主卷里, 不丢)
# 要还原 systemd 方案: sudo systemctl enable --now qwen qwen_chat saferag nginx
```

## 9. 常见问题

| 现象 | 排查 |
|---|---|
| `compose up` 报端口被占 | 宿主旧服务没停, 见 §7; `ss -lntp` 逐端口找主人 |
| 引擎容器起不来, 日志 `chat.cpp:348 Assertion 'true == ret' failed` | NPU init 失败: 模型文件名不符? `/dev/bm*` 缺? **宿主旧引擎占着 NPU**(杀干净, §7) |
| 引擎容器起来但 health 是宿主在答 | 宿主残留进程占着该端口, 清掉再 restart 容器验 |
| 后端容器日志 `No module named 'backend'` | compose 拉到错镜像(标签漂移); 确认 `saferag-backend` 是后端镜像(1.4G), 重跑 PACKAGING |
| 前端 404 | 部署文件夹里 `emergency-platform/frontend/index.html` 不在, 或没从该文件夹跑 compose |
| embedding 加载失败 | `models/` 里 embedding 目录名不是 `bge-small-zh-v1.5`(改名后重 up) |
| 知识库空 | 运行数据没放/没挂; 用前端上传或 `scripts/build_knowledge_base.py` 重建 |
| 盒子没装 docker-compose | 直接 `sudo bash install.sh`, 它用裸 docker run, 不需要 compose; 也可 `apt-get install -y docker-compose`(v1 也能跑 compose 文件) |
| 引擎容器 `import chat` 报符号/soname 错误 | 镜像里 chat.so 与盒子 libsophon 版本不兼容(不同代 SDK); 在该机用自己的 `Qwen3_5/python_demo` 重新构建 chat.so 后重打引擎镜像, 或用该机自带的 python_demo 替代 |
| `compose up`/`install.sh` 报 80 被占 | 板卡自带 nginx 常占 80: `sudo systemctl stop nginx` 后重跑 |
| 换芯片(BM1684X) | 换该芯片 bmodel(部署文件夹 models/ 里放 1684x 版); `devices:` 按目标机 `ls /dev/bm*` 调(install.sh 自动按存在透传); 必要时 `--privileged`; 镜像不重打 |
| 想收紧 JWT | `cp .env.example .env` 改 `JWT_SECRET` 后 `docker compose up -d --force-recreate backend`(所有人重登一次) |