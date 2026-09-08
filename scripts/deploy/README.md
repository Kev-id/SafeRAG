# SafeRAG 部署打包 —— 目录与用法

把 SafeRAG 打成**两个全离线 deb**:模型包 + 应用包。脚本在 Debian 环境(aarch64 盒子最理想)运行。

```
scripts/deploy/
├── make_deb.sh            打包器：一次产出两个 deb
├── nginx/                 盒子 /etc/nginx 配置快照（整树铺到目标 /etc/nginx）
├── systemd/               三个服务（qwen/qwen_chat/saferag，铺到 /etc/systemd/system）
└── debian/
    ├── app/   control postinst prerm conffiles    应用包控制脚本
    └── models/ control                              模型包控制
```

## 先准备两份“离线料”（各自只需准备一次）

```bash
# ① Python 依赖 wheels —— 在 aarch64 盒子上(或 aarch64 Debian 容器`sudo docker run --platform linux/arm64 -it debian:12`里)：
python3 -m pip download -r backend/requirements.txt \
    -d wheelhouse/ \
    --platform manylinux2014_aarch64 --python-version 310 \
    --implementation cp --abi cp310 --only-binary=:all:
#    检查没有 .tar.gz：  ls wheelhouse | grep -c '\.tar\.gz$'  应为 0

# ② 系统包(目标机若没 nginx) —— 同样在 aarch64 Debian 里：
apt-get download nginx nginx-common libpcre3 libssl3 zlib1g
mkdir -p offline-apt && mv *.deb offline-apt/
```

## 打包

```bash
bash scripts/deploy/make_deb.sh \
    --wheels wheelhouse/ \
    --offline-apt offline-apt/ \
    --version 1.0.0 -o ./release
# → release/saferag-models_1.0.0_arm64.deb + release/saferag_1.0.0_arm64.deb
```

`--make-wheels` 可现场生成 wheels（须本机 aarch64）;`--frontend` 默认取
`../emergency-platform/frontend`,没有则 `/data2/www/emergency-platform/frontend`。
`--models` 默认取仓库根 `models/`,没有则 `--models /data2/models`(盒子上打包直接指过去,
省掉 5.9G 副本;两种目录结构都认: 仓库平铺 或 盒子 Qwen3_5/ 分组)。

## 装机（目标盒子上, 全离线, 两条命令）

```bash
dpkg -i saferag-models_1.0.0_arm64.deb    # ≈6G 模型 → /data2/models
dpkg -i saferag_1.0.0_arm64.deb           # 代码→/data, nginx→/etc, 服务→systemd
```

应用包 postinst 自动: 补装 nginx(offline-apt)→ 离线装 Python 依赖 → 生成
`/etc/saferag/api.env`(JWT 随机) → enable+start 三个服务 → nginx -t 重启。

## 落盘路径映射（重要，别改乱）

| 包内 | 落到盒子 | 谁在用 |
|---|---|---|
| `data2/models/bge-small-zh-v1.5/` | embedding | `config.EMBEDDING_MODEL_PATH`(默认即此) |
| `data2/models/bge-reranker-base/` | 精排 | `api.env RERANKER_MODEL_PATH` |
| `data2/models/Qwen3_5/*.bmodel` + `config/` | 引擎权重+config | `qwen.service`/`qwen_chat.service` ExecStart |
| `data/SafeRAG/*` | 后端+引擎代码 | `saferag.service` WorkingDirectory |
| `data2/www/emergency-platform/frontend/` | 前端静态 | `nginx sites-available/SafeRAG` root |
| `etc/nginx/*`, `etc/systemd/system/*` | 系统配置 | 各服务/nginx |
| `opt/saferag/wheels`, `opt/saferag/offline-apt` | 离线依赖 | 装机时消费 |

## 升级 / 回滚

- 升级: 重打 app deb(`--version 1.1.0`), `dpkg -i saferag_1.1.0...` —— **模型包不动**
- 回滚: `apt-get install ./saferag_1.0.0_arm64.deb`(装旧版)或 `dpkg -r saferag`
- 数据(知识库/账号/文档在 `/data/SafeRAG/backend/data`)—— deb 不碰, 打包时已排除 `backend/data`

## 常见坑

- `--make-wheels` 在 x86 上跑会抓错轮子; 一定要 aarch64 环境
- 目标机 python 必须是 **3.10 aarch64**(引擎 `.so` 硬绑定)
- 模型 bmodel 文件名与 `qwen*.service` ExecStart 中的完整文件名必须一致(打包器会校验)
- 全新盒子知识库为空: 用前端上传接口添加法规文件; 或 `python3 scripts/build_knowledge_base.py` 重建索引