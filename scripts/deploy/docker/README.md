# SafeRAG Docker 部署(盒子: BM1688 / Ubuntu 22.04 / arm64)

> **三份文档分工**:
> - [PACKAGING.md](PACKAGING.md) —— 打包流程:在源环境产出可搬的镜像 tar 和清单
> - [DEPLOYING.md](DEPLOYING.md) —— 部署流程:目标盒子上把系统跑起来(假设传输物已在盒上)
> - 本文档 —— 在本机(已跑着 systemd 的盒子)从 systemd 切到 Docker 的构建/切换/回滚
>
> 搬新机 = 先看 PACKAGING 再照 DEPLOYING。

把盒子上的 SafeRAG 从 systemd 三服务迁到 Docker。四个容器, 全网 host, 与 systemd
时代同一套 `127.0.0.1:8000/8001/8081 + 宿主 80` 拓扑, 后端代码零改动。

| 容器 | 镜像 | 跑什么 | 端口 |
|---|---|---|---|
| `qwen-doc` | `saferag-qwen`(自建) | 4B bmodel 文档引擎 | 8000 |
| `qwen-chat` | `saferag-qwen`(自建, 同一镜像) | 2B int4 对话引擎 | 8001 |
| `backend`  | `saferag-backend`(自建) | uvicorn backend.main:app | 8081 |
| `nginx`    | `nginx:alpine`(官方) | 前端静态页 + 反代 | 80 |

**模型不烤进镜像。** 模型 17G 在 `/data2/models`, 三个容器只读挂载到 `/models`。
**前端不烤进镜像。** `/data2/www/emergency-platform/frontend` 静态托管给 nginx 容器。
**数据是卷。** `/data/SafeRAG/backend/data`(sqlite + 知识库 + 上传文档)读写挂载给 backend。

---

## 一、前提(已在本盒实测)

- Docker 20.10.12 + compose v5.4.0, data-root 已挪到 `/data/docker`(45G)
- 盒子有网: docker hub / pypi / 清华·阿里源均可达 → 构建现场拉依赖, 不需要离线轮子
- NPU 设备节点 `/dev/bm-tpu0` `/dev/bmdev-ctl` `/dev/ion`(666), 引擎容器直接 `devices:` 透传
- Sophon 运行时在宿主 `/opt/sophon/libsophon-current/lib`(与内核驱动同版本, 每台盒子配对)
  → **不烤进引擎镜像**。运行时由 compose 把宿主这份 lib 挂进容器 + `LD_LIBRARY_PATH`——
  每台盒子用自己的运行时, 引擎镜像因此可同架构跨机搬运(docker save/load),
  也不会因驱动版本错配起不来

## 二、构建(在盒子上)

```bash
cd /data/SafeRAG
bash scripts/deploy/docker/build_images.sh 1.0.0
# → saferag-qwen:1.0.0  saferag-backend:1.0.0
docker images | grep saferag
```

> 只在**改代码之后**才需要重建: 后端改 `backend/` → 重建 backend; 引擎改 `Qwen3_5/python_demo/`
> → 重建 qwen(构建脚本临时组装上下文, 不会把 17G 模型拖进 build)。

## 三、切换(第一次上 Docker)

```bash
# 1. 停宿主 systemd(可逆, 备份在这)
systemctl stop qwen qwen_chat saferag nginx
systemctl disable qwen qwen_chat saferag nginx

# 2. 起容器(可选先收紧密钥: cp .env.example .env 改 JWT_SECRET)
cd /data/SafeRAG/scripts/deploy/docker
docker compose up -d
docker compose ps

# 3. 验证
curl -s http://127.0.0.1:8000/health    # 引擎(4B)  → {"status":"ok"}
curl -s http://127.0.0.1:8001/health    # 引擎(2B)
curl -s http://127.0.0.1/health         # 经 nginx
curl -s http://127.0.0.1/ | head        # 前端首页
curl -s http://127.0.0.1/docs           # 后端 API 文档
bm-smi                                  # 看 NPU 双引擎占用
```

## 四、回滚(恢复 systemd)

```bash
cd /data/SafeRAG/scripts/deploy/docker
docker compose down
systemctl enable --now qwen qwen_chat saferag nginx
curl -s http://127.0.0.1/ | head
```

## 五、升级

```bash
# 拉新代码 → 重建对应镜像 → 重起
cd /data/SafeRAG
bash scripts/deploy/docker/build_images.sh 1.0.1
cd scripts/deploy/docker
sed -i 's/:1\.0\.0/:1.0.1/g' docker-compose.yml   # 或手动改 4 处 image 名
docker compose up -d
```

## 六、常见坑

- **引擎容器起不来且日志报设备/权限**: `docker run --rm -v /dev:/dev:rw --privileged` 兜底;
  优先确认三个设备节点在宿主存在(`ls -l /dev/bm-* /dev/ion`)。
- **端口已占用**: 宿主 systemd 服务没停干净, `ss -lntp | grep -E '8000|8001|8081|:80 '` 查。
- **两个引擎同时占 NPU**: 与 systemd 时代一致, BM1688 双核可同载 4B + 2B; 若 `bm-smi`
  显存不足, 先起 qwen-doc 等加载完再起 qwen-chat(compose `depends_on` 已排队)。
- **镜像可跨同架构机搬**: `backend`、`nginx`、`qwen`(运行时挂载宿主 libsophon)都可
  `docker save | docker load`。唯一前提是**目标机也有** `/opt/sophon/libsophon-current`
  和对应的 NPU 驱动(§一), 谁家的 driver 就用谁家的运行时, 镜像本身不用为它重建。
- **JWT 收紧会踢登录**: 改 `JWT_SECRET` 后所有人要重登一次, 按需做。