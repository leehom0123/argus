# 贡献指南

感谢考虑参与贡献。完整的贡献指南 —— 开发环境、代码风格、commit
约定、分支策略、DCO —— 都在仓库根目录的
[`CONTRIBUTING.md`](https://github.com/leehom0123/argus/blob/main/CONTRIBUTING.md)
里，GitHub 也是从那里读的，issue / PR 上的标准 *Contributing*
链接就是它。

## 快速链接

- [`CONTRIBUTING.md`](https://github.com/leehom0123/argus/blob/main/CONTRIBUTING.md)
  —— 完整贡献指南
- [`CODE_OF_CONDUCT.md`](https://github.com/leehom0123/argus/blob/main/CODE_OF_CONDUCT.md)
  —— Contributor Covenant 2.1
- [`SECURITY.md`](https://github.com/leehom0123/argus/blob/main/SECURITY.md)
  —— 安全漏洞上报渠道（私下）
- [`LICENSE`](https://github.com/leehom0123/argus/blob/main/LICENSE)
  —— Apache-2.0
- [`CITATION.cff`](https://github.com/leehom0123/argus/blob/main/CITATION.cff)
  —— 引用元数据

## 编辑这份文档

文档站基于 `docs/` 用 MkDocs Material 构建。

```bash
pip install -r requirements-docs.txt
mkdocs serve   # http://localhost:8000
```

热更新会同时盯着 `docs/`（英文）和 `docs/zh/`（简体中文）。每篇
英文页都必须在 `docs/zh/<同路径>.md` 有镜像；缺文件会让 CI 里的
`mkdocs build --strict` 失败。

文档站由 `.github/workflows/docs-deploy.yml` 在 `main` 上 `docs/`、
`mkdocs.yml` 或 `requirements-docs.txt` 变化时自动部署到 GitHub
Pages。

## 翻译

如果你看到某篇中文页读起来像 Google Translate 直译英文，欢迎开
PR 改。标准是 *地道*：技术结论保持准确，但用自然的中文句法写，
不要逐字镜像英文语序。

字段名、环境变量、命令名两边都保持英文 —— 翻译这些的话，文档里
的命令就没法直接复制粘贴到 shell 里跑了。
