# 第三方来源与许可

本版先在官方 v1.7.2 上完成现有补丁的独立兼容回归，再复验第三方报告的边界。

参考版本：wushuai0802/workbuddy2api-hub-ops-patch，tag `v1.6.20-ops.20261012.1`。
源码：https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/tree/v1.6.20-ops.20261012.1

- `wb_accounts.py` 中 `_RefreshDeadline`、`_DeadlineConnectionMixin`、两个连接类及 `_deadline_json` 改编自该版本的同名实现。普通生成请求仍使用官方连接池；有截止时间的凭据刷新使用独立连接、不跟随重定向，保持原账号代理出口语义。DNS 查询本身不能由 Python 标准库立刻取消，超时后不再连接或发送凭据。
- 拒答透传、多候选拒绝、Responses incomplete 终止事件依据复验结果按本地六条接口路径实现；测试证明修复前存在拒答丢失/额外恢复、多候选混合、终止事件类型不符。
- 未引入历史思考总量裁剪、工具图片替换、混合复读硬拦截或它们的默认策略。
- DSML 流式大小写与全角符号跨分块问题由本次迁移测试发现并修正；没有有效结束信号或存在流错误时不生成文本恢复的工具调用，有原生工具时不重复恢复。保留官方“完整、可解析的调用即使上游误标 length 也可恢复”行为；未完成的参数不恢复。

以下为该第三方仓库 LICENSE 原文，随改编代码保留：

```text
MIT License

Copyright (c) 2026

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
