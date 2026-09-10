# Docker 打包流程(在源环境产出「部署文件夹」)

> 打包 ≠ 部署:打包在**源环境**做(任意 aarch64 + 有网 + docker),产出物是一个**自包含的
> 「部署文件夹」**,拷到目标盒子,`sudo bash install.sh` 就起来。怎么跑见 [DEPLOYING.md](DEPLOYING.md)。

## 0. 打包成果物:一个「部署文件夹」

标准形态(文件夹自包含, 模型/前端都在里面, 拷到哪都能跑):

```
saferag-1688/                          ← 部署文件夹(bundle)
├── saferag-images.tar.gz              三镜像: backend / qwen / nginx:alpine(一个包)
├── docker-compose.yml                 自包含相对路径版(挂载 ./models ./emergency-platform/frontend)
├── install.sh                         一键部署脚本
├── nginx/saferag.conf                 与 compose 同目录(compose 相对引用)
├── models/                            ← 模型, 命名必须 bge-small-zh-v1.5
│   ├── Qwen3_5/
│   │   ├── config/  tokenizer.json / chat_template.jinja ...
│   │   ├── qwen3.5-4b_w4bf16_bm1688.bmodel
│   │   └── qwen3.5-2b-int4-..._111707.bmodel
│   ├── bge-small-zh-v1.5/             ← 必须叫 v1.5(后端固定只找这个名字)
│   └── bge-reranker-base/
└── emergency-platform/frontend/       前端(index.html 等)
```

**不放进文件夹、留在宿主的**:`/opt/sophon/libsophon-current`(NPU 运行时, 与盒子驱动配对)、
`/dev/bm*` 设备、(可选)后端运行数据 `/data/SafeRAG/backend/data`。

## 1. 准备打包环境

- aarch64 + docker + 能访问 docker hub 与 pypi(拉不到就配代理, 见 DEPLOYING.md §1.3)
- 有代码:`backend/`、`Qwen3_5/python_demo/`、`scripts/deploy/docker/`
  (git clone, 或从源盒 `rsync -a --exclude backend/data --exclude models ...`)

## 2. 构建镜像

```bash
cd <仓库目录>
bash scripts/deploy/docker/build_images.sh 1.0.0
docker images | grep saferag        # → saferag-backend:1.0.0  saferag-qwen:1.0.0
```

- 引擎镜像可移植:不烤 libsophon,运行时每个盒子挂自己 `/opt/sophon/libsophon-current`。
- 只在改代码后才重建:`backend/` 变更重建 backend;`Qwen3_5/python_demo/` 变更重建 qwen。

## 3. 打镜像 tar(三个镜像一个包)

```bash
# ⚠ compose 的 nginx 服务直接引用 nginx:alpine, 必须进 tar:
#   docker images | grep nginx   没有就先: docker pull nginx:alpine
mkdir -p ~/safe && cd ~/safe
docker save saferag-backend:1.0.0 saferag-qwen:1.0.0 nginx:alpine | gzip > saferag-images.tar.gz
ls -lh saferag-images.tar.gz       # ≈ 810M
```

> 只打两个镜像 → 目标机 `compose up` 会去 registry 拉 nginx,拉不到(`EOF`)整个卡死——已实测踩过。

## 4. 组装「部署文件夹」

```bash
mkdir -p saferag-1688/nginx saferag-1688/emergency-platform
cp ~/safe/saferag-images.tar.gz                        saferag-1688/
cp scripts/deploy/docker/docker-compose.yml            saferag-1688/
cp scripts/deploy/docker/install.sh                    saferag-1688/
cp scripts/deploy/docker/nginx/saferag.conf            saferag-1688/nginx/
cp -a <模型目录>/models/*                              saferag-1688/models/     # Qwen3_5 + bge-*(embedding 用 v1.5 名)
cp -a <前端目录>                                       saferag-1688/emergency-platform/frontend/
```

- 模型里的 embedding 目录**必须命名为 `bge-small-zh-v1.5`**(后端/镜像固定找这个名字)。
- 可选迁移:已有运行数据的话, `cp -a <盒子>/data/SafeRAG/backend/data saferag-1688/data`
  (install.sh 检测到 `data/` 就拷到目标机 `/data/SafeRAG/backend/data`)。

## 5. 版本一致性与自查

- compose 里 `image: saferag-<x>:<v>` 与构建的版本号一致。
- load 自查:tar 应能恢复 3 个镜像(尤其 `nginx:alpine`)。

## 6. 传过去

把整个 `saferag-1688/` 文件夹拷到目标盒子(rsync / scp / U 盘),然后按 DEPLOYING.md 跑 `sudo bash install.sh`。

---

## 常见问题

| 现象 | 原因/处理 |
|---|---|
| build 卡在 "Sending build context" 巨大 | 上下文带进了模型/仓库根; 用 `build_images.sh` 它会组装干净上下文 |
| 拉基础镜像失败 | 网络/代理, 见 DEPLOYING.md §1.3 |
| 目标机 `up` 卡在拉 nginx | tar 少了 `nginx:alpine`, 重打三镜像 tar |
| 换芯片(BM1684X) | 镜像不重打; 换目标盒对应 bmodel 与 `devices:` 列表(DEPLOYING.md 常见坑) |