# `main.py` 重写背景与实施记录

## 文档目的

这份文档用于给新的代码编写 thread 做上下文交接，避免重复解释以下内容：

- 为什么要重写 [main.py](/Users/wangyg/Development/Working/sing-box-geosite/main.py)
- 这次重写到底改了什么
- 实施过程里做过哪些关键决策
- 已经补了哪些测试
- 哪些坑已经踩过，不要再重复踩

这不是面向最终用户的说明文档，而是面向后续继续改代码的人。

## 一、改造目标

原脚本的职责是：

1. 读取 [links.txt](/Users/wangyg/Development/Working/sing-box-geosite/links.txt)
2. 下载每个链接对应的规则文件
3. 转换成 sing-box rule-set JSON
4. 再调用 sing-box 编译成 `.srs`

本次重写的目标，不是“修几处 bug”，而是把整个生成链路拉回到一个可维护状态。核心目标有这些：

1. 把旧脚本重写成一个结构清晰、可测试的 CLI
2. 生成 sing-box **Source Format v4** JSON，而不是继续沿用旧格式
3. 支持 DNS 专用拆分产物：
   - `filename.json`
   - `DNS_filename_domain.json`
   - `DNS_filename_ipcidr.json`
4. 删除 deprecated 字段处理：
   - `geoip`
   - `source_geoip`
   - `geosite`
5. 对 unsupported logical rules 明确 warning，不再静默乱转
6. 让 GitHub Actions 能先测试，再生成，再自动提交产物
7. 解决历史遗留生成物不会被清理的问题

## 二、为什么直接重写，而不是继续修旧脚本

这是一个明确的取舍，不是心血来潮。

旧脚本的问题不是单点 bug，而是整体设计烂掉了：

- 职责混杂，解析、转换、写文件、调用编译器糊在一起
- 可测试性很差
- 容错逻辑粗糙，很多地方靠宽泛 `try/except` 或静默跳过
- 对 sing-box 规则格式的抽象很弱
- 没有一套可靠的“生成产物清单 -> 清理旧文件”的机制

继续在旧脚本上补丁式修修补补，只会把债滚大。  
所以这次做了一个不讨好但正确的决定：**保留入口文件名 `main.py`，内部实现直接重写。**

## 三、实现前的明确边界

这次没有试图把所有 Clash 语法都一锅端，这是刻意收缩范围，不是能力问题。

### 支持的输入形态

- `.list` 风格的逐行规则
- YAML `payload` 风格
- 纯文本 domain / IP / CIDR 列表

### 支持转换的字段

- `domain`
- `domain_suffix`
- `domain_keyword`
- `domain_regex`
- `ip_cidr`
- `source_ip_cidr`
- `port`
- `port_range`
- `source_port`
- `source_port_range`
- `process_name`
- `process_path`
- `process_path_regex`
- `package_name`
- `network`

### 明确不做的事

- 不兼容 deprecated 的 `geoip` / `geosite` / `source_geoip`
- 不实现 Clash 逻辑规则 `AND` / `OR` / `NOT` 的自动重写
- 不伪造“看起来能跑”的错误转换结果

对这些不支持的输入，当前策略是：

- 记录 warning
- 跳过该条规则

这是保守策略，但比 silently wrong 强得多。

## 四、重写后的脚本结构

当前 [main.py](/Users/wangyg/Development/Working/sing-box-geosite/main.py) 基本按四层职责组织：

### 1. 输入层

- `read_links`
- `fetch_text`
- `extract_payload_items`

负责读取链接和原始规则文本。

### 2. 解析/归一化层

- `parse_rule_source`
- `parse_rule_item`
- `normalize_pattern`
- `infer_rule_item`
- `normalize_value`

负责把不同格式的规则文本归一成内部字段映射：

```python
dict[str, set[str]]
```

### 3. 产物构建层

- `build_documents`
- `build_rule_list`
- `canonical_stem`

负责从字段映射生成最终 JSON 文档。

### 4. 输出/编译/清理层

- `write_document`
- `compile_rule_set`
- `load_manifest`
- `write_manifest`
- `cleanup_stale_files`
- `discover_legacy_generated_files`
- `run`
- `main`

负责写入 JSON、调用 sing-box 编译 `.srs`、维护 manifest、删除旧产物。

## 五、DNS 拆分产物的规则

这是这次重写里最重要的业务规则之一。

### 通用产物

始终尽量生成：

- `filename.json`

该文件保留所有支持字段，供 route rule 或其他用途使用。

### DNS 专用产物

根据内容决定是否额外生成：

- `DNS_filename_domain.json`
- `DNS_filename_ipcidr.json`

其中：

- `DNS_*_domain.json` 只保留：
  - `domain`
  - `domain_suffix`
  - `domain_keyword`
  - `domain_regex`
- `DNS_*_ipcidr.json` 只保留：
  - `ip_cidr`

### 当前拆分规则

1. 只有 domain 类字段  
   只生成 `filename.json`

2. 只有 `ip_cidr`  
   只生成 `filename.json`

3. 只有非 DNS 专用字段，例如 `port` / `process_name`  
   只生成 `filename.json`

4. 有 domain 类字段，且无 `ip_cidr`，但存在其他字段  
   生成：
   - `filename.json`
   - `DNS_filename_domain.json`

5. 有 `ip_cidr`，且无 domain 类字段，但存在其他字段  
   生成：
   - `filename.json`
   - `DNS_filename_ipcidr.json`

6. 同时有 domain 类字段和 `ip_cidr`  
   生成：
   - `filename.json`
   - `DNS_filename_domain.json`
   - `DNS_filename_ipcidr.json`

这套规则已经由测试覆盖，不要随便改。

## 六、Source Format v4 与编译链路

所有 JSON 现在都输出为：

```json
{
  "version": 4,
  "rules": [...]
}
```

编译阶段通过：

```bash
sing-box rule-set compile --output target.srs source.json
```

当前 workflow 和本地脚本都按 **sing-box 1.13.0** 对齐。

这里有一个现实约束：

- JSON 是 source format v4
- 编译行为依赖 sing-box 二进制
- 因此测试里对编译步骤做的是 mock，不是集成跑真实二进制

这是合理的，不是偷懒。

## 七、测试优先的实施过程

这次是按“先补测试，再重写脚本”的顺序做的，而不是先改一堆代码再补几个象征性测试。

当前测试文件是 [tests/test_main.py](/Users/wangyg/Development/Working/sing-box-geosite/tests/test_main.py)。

### 第一批测试覆盖

先围绕核心转换逻辑补了这些场景：

1. `.list` 规则解析
   - 支持普通规则
   - 对 deprecated / unsupported logical rule 发 warning

2. YAML `payload` 解析

3. 输出拆分规则
   - domain-only
   - ipcidr-only
   - other-only
   - domain + other
   - ipcidr + other
   - domain + ipcidr
   - domain + ipcidr + other

4. 非 DNS 字段只保留在通用产物中

### 第二批测试覆盖

围绕执行链路补了集成风格测试：

1. `run()` 生成 JSON / `.srs` / manifest
2. 编译调用参数是否正确
3. 纯 `ip_cidr` 输入时不再生成 DNS 专用 `ipcidr` 产物
4. 第二次运行时是否能删除不再需要的 DNS 专用产物

### 第三批测试覆盖

后面因为清理逻辑连续暴露出历史遗留问题，又补了两类测试：

1. **manifest 缺失时**，能否清掉旧时代遗留生成物
2. **manifest 已存在但不完整时**，能否清掉 manifest 之外的孤儿产物

这两条测试是后来补的，直接来源于线上仓库里 `Advertising.json/.srs` 删不掉的实际问题。

## 八、实施过程中的关键坑

### 坑 1：只靠 manifest 清理不够

第一版 manifest 清理逻辑只会删除：

- “上一次 manifest 里记录过”
- “这一次不再需要”

的问题文件。

这对全新生成流程没问题，但对仓库里的历史遗留产物没用。

#### 后果

如果仓库里早就有一批旧 `.json/.srs`，而这些文件从来没进过 manifest：

- 它们就永远不会被删

#### 修复

增加 bootstrap 思路：

- 当 manifest 不存在时，先扫描 `rule/` 目录里的历史 `.json/.srs`
- 把它们视为候选旧产物，再和本轮应生成文件做差集

对应提交：

- `f111445` `fix(generator): bootstrap cleanup for legacy artifacts`

### 坑 2：manifest 存在，也照样可能漏删孤儿文件

上一条修复还不够。

因为后来发现另一种情况：

- manifest 已经存在
- 但某些历史遗留文件根本不在 manifest 里
- 这些文件依然删不掉

最典型的例子就是：

- `Advertising.json`
- `Advertising.srs`

#### 根因

这类文件是 manifest 机制上线前的老产物。  
manifest 存在，并不意味着 manifest 完整。

#### 修复

清理逻辑改成：

```text
stale = (manifest 记录集 ∪ 当前目录中的生成候选集) - 本轮应保留集
```

而不是只看 manifest。

这才真正把“manifest 外孤儿文件”也覆盖掉。

对应提交：

- `71ba8e8` `fix(generator): remove orphaned generated artifacts`

### 坑 3：GitHub Actions 安装 sing-box 时路径不存在

重写 workflow 后，第一次线上跑挂了，不是脚本本身的问题，而是 CI 安装 sing-box 的目录没创建。

报错是：

```text
install: cannot create regular file '/home/runner/.local/bin/sing-box': No such file or directory
```

修复方式非常直接：

- 在 `install` 前先 `mkdir -p "$HOME/.local/bin"`

对应提交：

- `baef91e` `fix(ci): ensure local bin exists before installing sing-box (#1)`

这不是 `main.py` 逻辑问题，但它影响整个生成链路，所以必须记在这里。

## 九、配套修改

这次并不是只改了 `main.py`。

### 1. Workflow 重写

文件：

- [.github/workflows/sync.yml](/Users/wangyg/Development/Working/sing-box-geosite/.github/workflows/sync.yml)

关键变化：

- `actions/checkout@v4`
- `actions/setup-python@v5`
- 先跑 `pytest`
- 再跑生成脚本
- 安装 sing-box `1.13.0`
- `git add -A rule`
- 自动提交 rule 产物和 manifest

对应提交：

- `376b46e` `ci: modernize sync workflow`

### 2. README 重写

文件：

- [README.md](/Users/wangyg/Development/Working/sing-box-geosite/README.md)

目的不是美化，而是让仓库说明终于和实际行为一致。

对应提交：

- `dc336be` `docs: rewrite README and remove legacy root rules`

### 3. 删除历史残留文件

删掉了根目录无实际价值的老文件：

- `emby.json`
- `wechat.json`

这个动作和 `main.py` 本身没耦合，但它属于同一轮“清理历史债”的工作。

## 十、关键提交时间线

核心相关提交按意图大致是：

1. `5ab1441` `feat(generator): rewrite rule conversion pipeline`
2. `376b46e` `ci: modernize sync workflow`
3. `dc336be` `docs: rewrite README and remove legacy root rules`
4. `f111445` `fix(generator): bootstrap cleanup for legacy artifacts`
5. `71ba8e8` `fix(generator): remove orphaned generated artifacts`
6. `baef91e` `fix(ci): ensure local bin exists before installing sing-box (#1)`

如果后续要追变更原因，先看这几个提交，别从更早的历史垃圾里浪费时间。

## 十一、当前状态

截至这份文档生成时，当前系统状态是：

- [main.py](/Users/wangyg/Development/Working/sing-box-geosite/main.py) 已经完成重写
- [tests/test_main.py](/Users/wangyg/Development/Working/sing-box-geosite/tests/test_main.py) 已覆盖核心行为
- manifest 清理链路已经补上历史遗留和孤儿产物处理
- GitHub Actions 已改成“先测后生”
- sing-box 版本已切到 `1.13.0`

最近一次本地验证结果是：

```text
12 passed
```

## 十二、继续修改时的建议

后续如果要继续改 [main.py](/Users/wangyg/Development/Working/sing-box-geosite/main.py)，建议遵守这些原则：

1. **别把 parser 和 writer 再揉回去**
   现在的分层虽然不复杂，但已经比旧脚本好很多。别为了省几行代码把职责重新搅乱。

2. **新增字段前先想清楚 DNS 拆分语义**
   不是所有字段都应该出现在 DNS 专用输出里。别把 route 视角的字段硬塞给 DNS。

3. **改清理逻辑前先看现有测试**
   manifest 相关逻辑已经踩过两轮坑。没必要第三次再掉进去。

4. **不要装作支持 logical rules**
   如果没有打算认真实现语义保真转换，那就继续 warning + skip。半吊子支持只会制造更隐蔽的错误。

5. **如果要扩展输入格式，先补测试**
   这个文件已经从“纯脚本”进化成“有规则的生成器”了。别再退回拍脑袋改法。

## 十三、适合作为新 thread 的起始上下文

如果新 thread 要继续做代码相关工作，建议直接引用这几个文件：

- [main.py](/Users/wangyg/Development/Working/sing-box-geosite/main.py)
- [tests/test_main.py](/Users/wangyg/Development/Working/sing-box-geosite/tests/test_main.py)
- [.github/workflows/sync.yml](/Users/wangyg/Development/Working/sing-box-geosite/.github/workflows/sync.yml)
- [docs/main-py-rewrite-background.md](/Users/wangyg/Development/Working/sing-box-geosite/docs/main-py-rewrite-background.md)

并把问题收敛到下面这种粒度：

- “给 parser 增加某种新规则类型支持”
- “调整 DNS 拆分策略”
- “优化 manifest 清理规则”
- “补充某类失败场景测试”

别再开一个“帮我看看这个仓库发生了什么”的大杂烩 thread。那只会把上下文再次搞脏。
