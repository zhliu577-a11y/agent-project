# ADR 0008：插件包生命周期与宿主注册表

- 状态：已采纳
- 日期：2026-09-15

## 背景

最初的插件体系只做磁盘发现：插件目录出现在 `plugins/` 下，loader 读取
`plugin.json` 并立即参与装配。这个模型适合内置插件，却没有宿主侧的安装、
启停、版本和来源状态：

- `plugin.json` 同时被当成包描述和运行时开关；
- 没有 staging、摘要或原子安装边界；
- 删除目录会直接丢失审计信息；
- loader 若负责写入状态，会破坏它原本的纯读取职责。

## 决策

插件包声明与宿主生命周期状态分离：

- `plugin.json` 描述包身份、版本、贡献和入口；
- `data/plugin-registry.json` 保存宿主状态，包括 installed、enabled、
  version、source、digest、时间和 runtimeStatus；
- `PluginManager` 是唯一生命周期写入方；
- `plugin.json` 中的 `enabled` 只属于包自身声明，外部安装包不通过改写该
  字段来启停。

目录布局：

```text
data/plugin-registry.json
data/plugin-store/
├── staging/
├── installed/
└── trash/
```

安装流程：

```text
validate
→ 解压/复制到 staging
→ 防止 zip 路径穿越
→ 拒绝符号链接
→ 限制文件和总大小
→ 定位唯一插件根
→ 静态检查入口
→ 计算确定性 sha256
→ 原子移动到 installed/<name>/<version>
→ 写入 plugin-registry.json
```

安装后的默认状态是 `installed=true, enabled=false`。`enable / disable`
只修改注册表。`remove` 先把包移动到 `trash/`，再更新注册表。
`runtimeStatus` 由 `HarnessRuntime` 在启动、失败和关闭时更新；registry 保存的
是最近一次实际运行结果，不把“用户希望启用”当成“当前进程已经加载”。

注册表写入采用“临时文件 → flush/fsync → `os.replace`”，并在进程内加锁。

## 后果

- loader 保持只读，不再修改 `plugin.json` 或宿主状态。
- 外部插件只有安装并显式启用后，才会加入 Runtime 的插件 roots。
- 启停后有明确的重载边界：第一版需要重启或重建 Runtime，不做热加载。
- 第一版不自动执行 `pip install`，插件依赖由安装者负责。
- 插件代码仍是可信执行边界，入口检查、大小限制和路径检查不是沙箱。
- `trash/` 当前保留审计和人工恢复空间，后续可以增加清理命令。

## 备选方案

### 把生命周期写回 `plugin.json`

不采用。这样会把包内容与宿主机状态混在一起，升级包时容易覆盖状态，也无法
可靠区分“作者声明禁用”和“宿主当前禁用”。

### 让 loader 直接读写注册表

不采用。loader 应保持无副作用；装配失败可以快速暴露，但安装、启停和删除
必须由显式的管理边界执行。

### 安装后自动启用

不采用。插件可能包含任意 Python 代码或子进程命令，安装与启用应是两个分离
的用户决策。
