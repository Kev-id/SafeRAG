# 新机搬运(同架构 aarch64 / BM1688)

> 本文件只是索引。**搬运 = 打包 + 部署**,两步拆开、各管各的:
>
> 1. **打包**(在任何 aarch64 + docker 的源环境):产出**自包含「部署文件夹」** → [PACKAGING.md](PACKAGING.md)
> 2. **部署**(把整个部署文件夹拷到目标盒):装 docker → 确认 NPU → `cd 文件夹 && sudo bash install.sh` → [DEPLOYING.md](DEPLOYING.md)

## 一句话流程

```
PACKAGING:  build_images.sh 构建 → docker save 打 tar(backend/qwen/nginx 三镜像)
          + 组装部署文件夹: tar + compose + nginx conf + models/ + frontend/
          → 整个文件夹拷到新盒

DEPLOYING: 新盒装 docker(必要时挪 data-root/配代理) → 确认 NPU 设备 + libsophon
          → sudo bash install.sh(load 镜像 + compose up + 验证)
```

## 关键提醒(细枝末节都在上面两份文档里)

- **模型在部署文件夹的 `models/` 里**,随文件夹走,不用再单独放 `/data2`
  (`Qwen3_5/{两个 bmodel, config/}`、`bge-small-zh-v1.5/`、`bge-reranker-base/`)
- **前端在部署文件夹的 `emergency-platform/frontend/` 里**,同样随文件夹走
- **运行数据固定宿主路径** `/data/SafeRAG/backend/data`,不进文件夹
- **引擎镜像可跨盒跑**——它不烤 libsophon,新盒挂自己的 `/opt/sophon/libsophon-current`
- 换芯片(BM1684X):镜像不重打,换 bmodel + 调 `devices:`
- 从前一盒 systemd 方案迁移时,检查 DEPLOYING.md §7 的残留进程问题