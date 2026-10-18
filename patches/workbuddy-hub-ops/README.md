# WorkBuddy2API Hub 运维增强补丁

面向 [ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub) v1.7.2；固定文件摘要和提交信息见 `BASELINE.json`，第三方许可见 `THIRD-PARTY-zh.md`。

此目录发布通用增强模块、补丁安装器和离线测试。排除了真实请求记录、部署验收报告、私有配置和原始会话样例；短语测试使用新生成的虚构示例。

## 使用

在独立备份和严格匹配的上游源码副本上运行：

```sh
python apply.py --help
python apply.py --target /path/to/matching/upstream
```

安装器会核对基线。请先阅读 `BASELINE.json`，不要强制应用到不匹配的版本。`Dockerfile.ops` 接受 `BASE_IMAGE` 构建参数，必须使用与基线一致的上游镜像。

```sh
docker build -f Dockerfile.ops --build-arg BASE_IMAGE=YOUR_MATCHING_UPSTREAM_IMAGE -t workbuddy-hub-ops:1.7.2 .
```

测试源码保留于 `tests/`。归集只校验可发布源码与检查结果，不包含生产账号、请求内容或运行数据库，也不改变任何现有服务。
