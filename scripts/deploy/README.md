# SafeRAG 打包与安装

把整套 SafeRAG(后端 + Qwen 引擎 + 前端 + 模型 + 离线依赖)打成**两个全离线 deb**,
拷贝到目标盒子(无外网)上两条命令装完。

- `saferag-models_*.deb` —— 模型(≈6G),装到 `/data2/models`
- `saferag_*.deb` —— 程序 + 前端 + 依赖 + nginx 站点配置 + systemd 服务

> 打包必须在 **Debian 环境**(目标盒子上最理想,或 aarch64 Debian 容器)跑。
> x86 机器只能打 x86 包,且 `--make-wheels` 会抓错轮子。

---

## 第 1 步:准备"离线料"(按情况决定,不一定全要)

判断标准不是"你打包有没有网",而是"**装软件的盒子有没有网、有没有 nginx**"。
打包机有网 ≠ 目标盒子有网;这套 deb 的核心场景就是目标盒子全离线。

### A. Python 轮子(wheels)—— 不用提前备,打包时现下

wheels 交给打包脚本现场收:第 2 步传 `--make-wheels`,在打包机(aarch64)上
`pip download` 按盒子真实平台解析依赖。

> ⚠ 别照搬"手动 `pip download --platform manylinux2014_aarch64 ...`"的老写法:
> 这个过死的平台标签把新版 onnxruntime 一刀切(它已改用更新的 manylinux 基线),
> 实测只能下到 1.16.3 → 报 `No matching distribution for onnxruntime==1.23.2`。
> `--make-wheels` 不带这些标签,按盒子本机平台解析,则没事。
> (若打包机不是 aarch64,`--make-wheels` 本就跑不了,见第 2 步前提。)

### B. nginx 离线包(offline-apt)—— 目标机没 nginx 且没网才需要

```bash
apt-get download nginx nginx-common libpcre3 libssl3 zlib1g
mkdir -p offline-apt && mv *.deb offline-apt/
```

| 目标盒子情况 | 要不要备 offline-apt |
|---|---|
| 已装 nginx | 不用 |
| 有网 | 不用(但那就不是纯离线盒子了) |
| 没 nginx 且没网 | **必须备**,否则 postinst 只打警告、不会自动装 nginx |

> 打包机有网+ aarch64 的省事组合:轮子用 `--make-wheels` 现下;offline-apt 按上面表格决定。

## 第 2 步:打包(一条命令,产出两个 deb)

打包机必须是 aarch64 且有网(现场下轮子)。最省事的做法:直接在目标盒子上打包,
`--models` 还能省(默认就用盒子上 `/data2/models` 的真模型)。

```bash
# 打包机是目标盒子,有网 → 轮子现场下
bash scripts/deploy/make_deb.sh \
    --make-wheels \
    --offline-apt offline-apt/ \
    --version 1.0.0 \
    -o ./release
# → release/saferag-models_1.0.0_arm64.deb + release/saferag_1.0.0_arm64.deb
```

### 所有参数

| 参数 | 作用 | 默认 |
|---|---|---|
| `--offline-apt DIR` | nginx/.deb 目录(第 1 步 B) | 无(有则打进包) |
| `--make-wheels` | 现场 `pip download` 抓 arm64 轮子(推荐) | 需 aarch64 本机且有网 |
| `--wheels DIR` | 已有现成 wheelhouse 则塞进包(备选,省得现场下) | 无(有则打进包) |
| `--frontend DIR` | 前端静态目录 | `/opt/emergency-platform/frontend`,再退 `../emergency-platform/frontend` |
| `--models DIR` | 模型目录 | 优先盒子 `/data2/models`(真含 bmodel),否则仓库根 `models/` |
| `--version X.Y.Z` | 版本号 | `1.0.0` |
| `-o, --out DIR` | 输出目录 | 系统临时目录 |

模型目录两种结构都认:仓库平铺(`models/qwen3.5-4b...bmodel`)或盒子分组(`/data2/models/Qwen3_5/...`)。
在盒子上打包直接 `--models /data2/models`,省掉 5.9G 副本。

## 第 3 步:装机(目标盒子上,全离线)

```bash
dpkg -i saferag-models_1.0.0_arm64.deb    # 模型 → /data2/models(先装,应用包依赖它)
dpkg -i saferag_1.0.0_arm64.deb           # 程序 + 前端 + 服务 + nginx 站点
```

装完应用包时,postinst 自动完成:

1. 目标机**没有 nginx** → 用包内 `offline-apt/` 离线补装;**已有则跳过**
2. 离线装 Python 依赖(包内 `wheels/`,没有则尝试在线)
3. 生成 `/etc/saferag/api.env`(JWT 随机;已存在则复用)
4. 启动三个服务 `qwen / qwen_chat / saferag`
5. 铺 nginx:停用自带 `default` 站 → 启用 SafeRAG 站点 → `nginx -t` → 重启

### 验证

```bash
systemctl status qwen qwen_chat saferag   # 三个服务都 active
curl http://127.0.0.1/                    # 前端页面
curl http://127.0.0.1/docs                # API 文档
# 从外面:  http://<盒子IP>/     http://<盒子IP>/docs
```

## 落盘路径(别改乱)

| 包内 | 落到盒子 | 谁在用 |
|---|---|---|
| `data2/models/bge-small-zh-v1.5/` | embedding 模型 | 默认 `EMBEDDING_MODEL_PATH` |
| `data2/models/bge-reranker-base/` | 精排模型 | `api.env RERANKER_MODEL_PATH` |
| `data2/models/Qwen3_5/`(.bmodel + `config/`) | 引擎权重+配置 | `qwen.service` / `qwen_chat.service` |
| `data/SafeRAG/` | 后端+引擎代码 | `saferag.service` |
| `opt/emergency-platform/frontend/` | 前端静态页 | nginx SafeRAG 站点 root |
| `etc/nginx/sites-{available,enabled}/SafeRAG` | nginx 站点(80 默认站) | nginx |
| `etc/systemd/system/*.service` | 三个服务 | systemd |
| `opt/saferag/wheels`、`opt/saferag/offline-apt` | 离线依赖 | 装机时消费 |

## 升级 / 回滚

- **升级**:只重打应用包(`--version 1.1.0`),`dpkg -i saferag_1.1.0...`。模型包不用动。
- **回滚**:`apt-get install ./saferag_1.0.0_arm64.deb`(装旧版)或 `dpkg -r saferag`。
- 数据(知识库/账号/文档)在 `/data/SafeRAG/backend/data` —— deb 不碰,打包时已排除。

## 常见问题

- **`dpkg -i` 报 `trying to overwrite ... which is also in package nginx-common`**
  → 用了旧版打包脚本(曾把 nginx 自带文件整树打进包)。**重新跑一遍第 2 步**生成新 deb 即可;新版只带 SafeRAG 自己的站点配置,不再接管 nginx 文件。
- **全新盒子知识库为空**:前端上传文件,或 `python3 scripts/build_knowledge_base.py` 重建索引。
- **目标机 python 必须 3.10 aarch64**(引擎 `.so` 硬绑定)。
- **模型 .bmodel 文件名**必须与 `qwen*.service` ExecStart 里的完整文件名一致(打包器会校验)。
- **装了 nginx 却打不开前端**:`nginx -t` 看报错;SafeRAG 站点已设为 80 默认站,若机器上还有别的 80 站点需停用。