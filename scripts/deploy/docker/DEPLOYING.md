# Docker 部署流程(目标盒, 假设要搬的东西已经在盒子上)

> 本文件只讲**部署**。镜像 tar、模型、前端、数据、compose 文件都已传到盒子上,按本文把系统跑起来。
> 怎么把这些东西打出来,见 [PACKAGING.md](PACKAGING.md)。
>
> 部署动作:装 Docker → 确认 NPU → 放模型/前端/数据 → load 镜像 → compose up → 验证。

## 0. 部署假设(以下已具备, 不再讲怎么传)

- 镜像 `saferag-images.tar.gz`(saferag-backend + saferag-qwen)
- compose 与 nginx 配置(至少 `docker-compose.yml` + `nginx/saferag.conf`)
- 模型已放好、前端已放好、(可选)数据和已有盒子一致

**模型就放这个目录(重点)**, 结构/文件名必须与 compose 的 `command` 一字不差
(容器把 `/data2/models` 挂载为 `/models:ro`):

```
/data2/models/
├── bge-small-zh-v1.5/                    # embedding(ONNX 跑 CPU)
│   └── onnx/model_quantized.onnx  tokenizer.json
├── bge-reranker-base/                    # reranker(ONNX 跑 CPU)
│   └── onnx/model_quantized.onnx
└── Qwen3_5/
    ├── config/                           # tokenizer.json / chat_template.jinja 等
    ├── qwen3.5-4b_w4bf16_bm1688.bmodel
    └── qwen3.5-2b-int4-autoround_w4bf16_seq8192_bm1688_2core_history_dynamic_20260728_111707.bmodel
```

> 前端固定放 `/data2/www/emergency-platform/frontend`;数据固定放 `/data/SafeRAG/backend/data`

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
ls -l /dev/bm-tpu0 /dev/bmdev-ctl /dev/ion    # 三个节点必须在
/opt/sophon/libsophon-current/bin/bm-smi      # 能看到设备即可
ls /opt/sophon/libsophon-current/lib           # 引擎容器要挂载这个目录(libbmrt.so.1.0 等)
```
带 BSP 的 BM1688 板子一般自带;没有就先装 Sophon SDK 的 driver + libsophon(两者配对)。

## 4. 解包镜像 + 放 compose

```bash
gunzip -c saferag-images.tar.gz | docker load
docker images | grep saferag                 # saferag-backend / saferag-qwen
# compose 放哪都行, 但 nginx/saferag.conf 要和 docker-compose.yml 同目录(相对路径引用)
cd <compose 所在目录>
```

## 5. 起服务

```bash
# 清掉可能占 80/8000/8001/8081 的宿主旧服务(全新盒子一般没有, 从旧盒子迁来的必查, 见 §7)
ss -lntp | grep -E ':80 |:8000|:8001|:8081' || echo OK-free

docker compose up -d
docker compose ps        # 期望 4 个容器全部 Up
```

## 6. 验证

```bash
curl -s http://127.0.0.1/            # 前端首页 200
curl -s http://127.0.0.1/docs        # 后端 API 文档 200
curl -s http://127.0.0.1:8000/health # 引擎 4B → {"status":"ok",...}
curl -s http://127.0.0.1:8001/health # 引擎 2B
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
# 应该只剩容器进程(命令里是 /models 路径); 残留的任何宿主进程都 pgrep -f 揪出来停掉
# 宿主引擎的命令是 /data2/models 路径, 容器的是 /models, 一眼可辨
```

若还有 `Restart=always` 的 unit 在重生产物:`sudo systemctl stop qwen_chat && sudo systemctl reset-failed qwen_chat`
并确认 `systemctl show qwen_chat -p ActiveState,MainPID` 变 `inactive`/`0`。

## 8. 回滚 / 卸载

```bash
cd <compose 所在目录> && docker compose down          # 停容器(数据还在 /data/... 卷里)
# 若之前是 systemd 方案、要还原: sudo systemctl enable --now qwen qwen_chat saferag nginx
```

## 9. 常见问题

| 现象 | 排查 |
|---|---|
| `compose up` 报端口被占 | 宿主旧服务没停, 见 §7; `ss -lntp` 逐端口找主人 |
| 引擎容器起不来, 日志 `chat.cpp:348 Assertion 'true == ret' failed` | NPU init 失败: 模型文件名不符? `/dev/bm*` 缺? **宿主旧引擎占着 NPU**(杀干净, §7) |
| 引擎容器起来但 health 是宿主在答 | 同样宿主残留进程占着该端口, 清了以后 restart 容器再验 |
| 后端容器日志 `No module named 'backend'` | compose 拉到错镜像(image 标签漂移); 确认 `docker images` 里 `saferag-backend` 是后端镜像(1.4G), 必要时重跑 PACKAGING |
| 前端 404 | `/data2/www/emergency-platform/frontend/index.html` 不在 |
| 知识库空 | 数据卷没放/没挂; 用前端上传或 `scripts/build_knowledge_base.py` 重建 |
| 换芯片(BM1684X) | 换该芯片 bmodel; `docker-compose.yml` 里 `devices:` 按新机 `ls /dev/bm*` 调(1684X 一般无 `bm-tpu0`); 必要时 `--privileged`; 镜像不重打 |
| 想收紧 JWT | `cp .env.example .env` 改 `JWT_SECRET` 后 `docker compose up -d --force-recreate backend`(所有人重登一次) |