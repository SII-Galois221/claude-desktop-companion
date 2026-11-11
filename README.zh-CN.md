# Claude Desktop Companion

[English](README.md) · [历史操作说明](docs/history.md) · [网络监测说明](docs/network.md) · [隐私说明](PRIVACY.md)

一个 macOS 菜单栏工具：**备份当前 Claude Desktop 账号的本地 Code 历史，把保存的备份导入当前账号，并按需监测已有代理连接。**

这是早期源码版本，界面为中文。本地历史功能目前仅支持已检查的 **Claude Desktop 5.1231.2**。项目与 Anthropic 无隶属关系；不承诺防封号，也不提供完整网络隔离。

## 两个主要按钮

| 操作 | 做什么 |
| --- | --- |
| **备份当前账号** | 保存本地 Code 会话索引、关联正文和附属文件，生成校验过的备份。 |
| **导入到当前账号** | 选择已保存的备份，预览差异，先备份目标，再补齐缺少的历史。 |

账号登录仍在官方 Claude 客户端完成。工具不保存密码、不切换登录令牌、不迁移网页聊天，也不会上传备份。

## 安装与打开

需要 macOS 13+、Xcode Command Line Tools、Python 3.11+ 和官方 Claude Desktop。Python 后端仅使用标准库。GUI 会查找 Apple Silicon／Intel Homebrew 的 Python；自定义解释器见 [开发说明](CONTRIBUTING.md)。

```bash
xcode-select --install       # 已安装命令行工具可跳过
git clone https://github.com/SII-Galois221/claude-desktop-companion.git
cd claude-desktop-companion
bash build-app.sh
open "build/Claude Desktop Companion.app"
```

构建会执行 Swift 模型检查并进行本机临时签名。初始版本仅提供源码，**没有公证的预编译安装包**。请保持应用名称不变；构建后也可复制到 `~/Applications/`。

未配置网络监测时，该功能保持关闭，可以单独使用备份与导入。

## 使用步骤

1. 在官方客户端登录，打开一个**本地 Code** 会话。每个账号首次使用时，在仓库目录登记一次别名：

   ```bash
   printf '%s\n' '{"alias":"personal"}' | python3 backend/history_service.py register
   ```

2. 从菜单栏打开 **备份与导入**（快捷键 **⌘1**），点击 **备份当前账号**，等待完成。
3. 在官方客户端登录另一个账号；首次使用时，用 `work` 等其他别名登记，然后刷新本工具。
4. 选择保存的备份，点击 **预览导入**，确认后点击 **导入到当前账号**。写入前需要完全退出 Claude，当前后台任务可能被中断。

已有会话索引保留。目标正文若是备份正文的较新延续，会保留较新版本；内容分歧则停止导入。新增入口采用手动权限和待确认续跑状态；已有记录保留原设置。完整的版本限制、数据范围和异常恢复流程见 [历史操作说明](docs/history.md)。

## 可选：代理监测

需要你已经配置好专用的本机 HTTP 代理入口，以及 Mihomo 的 Unix 控制 socket。工具登记指定出口节点、预期公网 IP 和配置文件校验值，**不会替你安装代理、购买家宽或修改 Claude 的代理设置**。具体配置见 [网络说明](docs/network.md)。

配置后，打开应用开始监测，暂停或退出会停止自动退出 Claude 的功能；没有独立的开机常驻服务。

它是定时监测工具，**不是防火墙或 TUN**。本地配置／代理／控制器异常，或连续探测失败，可以触发正常终止 Claude；抽样连接或公网 IP 不符目前只显示警告。无法覆盖每个短连接、所有 UDP 或独立启动的终端 CLI。绿色表示近期检查通过，不代表每个数据包都已验证。周期探测会产生少量网络请求，实际流量随代理和传输方式变化。

## 数据和隐私

所有本机数据放在：

```text
~/Library/Application Support/Claude-Desktop-Companion/
```

`history/` 保存账号登记、历史备份、预览和操作凭据；`network.json` 是可选网络基线；`runtime/` 保存监测状态和日志。

备份**没有由本工具加密**，对话正文可能包含敏感信息。请勿把这个目录、真实聊天记录、API 密钥或代理配置上传到 GitHub。仓库只包含合成测试数据。访问范围和外部探测地址见 [隐私说明](PRIVACY.md)。

卸载时先退出工具，再移除应用。历史备份会保留；仅在确实不再需要时单独删除数据目录。本工具不会移除 Claude 自身的代理设置。

## 开发与检查

```bash
python3 -B -m unittest discover -s backend -p 'test_*.py'
python3 scripts/check_public_tree.py
bash build-app.sh
```

测试仅使用临时合成数据。参见 [贡献指南](CONTRIBUTING.md)、[安全问题报告](SECURITY.md) 和 [MIT 许可证](LICENSE)。

文档组织参考了 [Stats](https://github.com/exelban/stats)、[Maccy](https://github.com/p0deje/Maccy) 和 [Clash Verge Rev](https://github.com/clash-verge-rev/clash-verge-rev)，没有复制这些项目的源码、图片或文档。
