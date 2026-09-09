# Docker 打包流程(在源环境产出可搬的镜像)

> 本文件只讲**怎么打**出能搬走的产物;怎么在一台新盒子上跑起来,见 [DEPLOYING.md](DEPLOYING.md)。
> 打包 ≠ 部署:打包在**源环境**做(任何 aarch64 + 有网 + docker),产出物是镜像 tar 和一份清单。

## 0. 打包成果物(要拿到的东西)

| # | 产物 | 来源 | 流向 |
|---|---|---|---|
| ① | `saferag-images.tar.gz` | 下面 §2 打 | → 目标盒 `docker load` |
| ② | `scripts/deploy/docker/`(compose + nginx conf + 构建脚本) | 仓库 | → 目标盒(部署只用到 compose + `nginx/saferag.conf`) |
| ③ | 模型 `/data2/models` | 源盒/模型包 | → 目标盒同一路径 |
| ④ | 前端 `/data2/www/emergency-platform/frontend` | 源盒 | → 目标盒同一路径 |
| ⑤ |(可选)运行数据 `/data/SafeRAG/backend/data` | 源盒 | → 目标盒同一路径 |

核心是两个自建镜像 `saferag-backend` + `saferag-qwen`。**引擎镜像可移植** —— 它不烤 Sophon
运行时(NPU),运行时在部署时由每个盒子挂载自己 `/opt/sophon/libsophon-current/lib`,所以
镜像本身跨同架构机直接跑,不因驱动版本重建。

## 1. 准备打包环境

- aarch64 环境 + docker(`docker --version`)—— 在本项目就是 BM1688 盒子,也可以是别的 arm64 机
- 能访问 docker hub 与 pypi(打不了就配代理,见 DEPLOYING.md §1.3)
- 有代码:至少含 `backend/`、`Qwen3_5/python_demo/`、`scripts/deploy/docker/`
  (git clone, 或从源盒 `rsync -a --exclude backend/data --exclude models linaro@<源盒>:/data/SafeRAG/ ...`)

## 2. 构建镜像

```bash
cd <仓库目录>
bash scripts/deploy/docker/build_images.sh 1.0.0
docker images | grep saferag
# → saferag-backend:1.0.0   saferag-qwen:1.0.0
```

- 脚本临时组装干净上下文(只拷 python_demo / backend + 依赖清单,不含 17G 模型),避免把模型拖进 build。
- **引擎镜像不需要在目标盒构建、不需要为目标盒驱动版本重建**;只有当 `Qwen3_5/python_demo/`
  代码或 Python 依赖变更时才重建引擎镜像;`backend/` 变更才重建后端镜像。
- 换代码重建后,记得用**同一版本号**重打 tar(见 §3/§4 一致性)。

## 3. 打镜像 tar(可搬走)

```bash
mkdir -p ~/safe && cd ~/safe
docker save saferag-backend:1.0.0 saferag-qwen:1.0.0 | gzip > saferag-images.tar.gz
ls -lh saferag-images.tar.gz
```

- nginx 用官方镜像,通常不打包:目标盒联网即可拉 `nginx:alpine`。
  **若目标盒可能离线**,也一并打走:`docker save nginx:alpine | gzip >> ...`(或单独 tar)。
- 全离线模式:把 `scripts/deploy/docker/nginx/saferag.conf` 和 compose 一起带着就行。

## 4. 版本一致性与自查

- 构建版本号 → compose 里 `image: saferag-<x>:<v>` 的标签必须一致。
- 打包完自查:

```bash
gunzip -c saferag-images.tar.gz | docker load   # 在别的机器上能 load 出来
docker images | grep saferag
```

## 5. 传输

按 DEPLOYING.md §3 把 ①②③④⑤ 传到目标盒(rsync / scp / U 盘均可)。

---

## 常见问题

| 现象 | 原因/处理 |
|---|---|
| build 卡在 "Sending build context" 巨大 | 上下文带进了模型/仓库根; 确认从 `scripts/deploy/docker/` 的 `build_images.sh` 跑, 它组装干净上下文 |
| 拉基础镜像失败 | 网络/代理问题, 见 DEPLOYING.md §1.3 给 daemon 配代理 |
| 换芯片(BM1684X) | **镜像本身不用重打**; 只需在目标盒换对应 bmodel 与设备节点(见 DEPLOYING.md 常见坑) |