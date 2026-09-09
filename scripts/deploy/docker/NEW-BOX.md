# 新机搬运(同架构 aarch64 / BM1688)

> 本文件只是索引。**搬运 = 打包 + 部署**,两步拆开、各管各的:
>
> 1. **打包**(在任何 aarch64 + docker 的源环境):产出镜像 tar 和搬运清单 → [PACKAGING.md](PACKAGING.md)
> 2. **部署**(在目标新盒,假设传输物已在盒上):装 docker → 确认 NPU → 放模型/前端/数据 → load 镜像 → compose up → 验证 → [DEPLOYING.md](DEPLOYING.md)

## 一句话流程

```
PACKAGING:  build_images.sh 构建 → docker save 打 saferag-images.tar.gz
          + 带上 models / frontend / (可选) backend data, 传到新盒

DEPLOYING: 新盒装 docker(必要时挪 data-root/配代理) → 放 /data2/models 等
          → gunzip -c tar.gz | docker load → docker compose up -d → 验证
```

## 关键提醒(细枝末节都在上面两份文档里)

- **模型放新盒 `/data2/models`**,结构/文件名和 compose 的 `command` 一致
  (`bge-small-zh-v1.5/`、`bge-reranker-base/`、`Qwen3_5/{config, *.bmodel}`)
- **引擎镜像可跨盒跑**—— 它不烤 libsophon,新盒挂自己的 `/opt/sophon/libsophon-current`
- 换芯片(BM1684X):镜像不重打,换 bmodel + 调 `devices:`
- 从前一盒 systemd 方案迁移时,检查 §7(DEPLOYING)的残留进程问题