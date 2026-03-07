# sing-box-geosite

这个仓库的作用很单一：

- 从 `links.txt` 读取远程规则源
- 将 Clash 风格规则转换为 sing-box rule-set **Source Format v4**
- 在需要时额外生成 DNS 专用拆分产物
- 调用 `sing-box 1.13.0` 编译出 `.srs`
- 通过 GitHub Actions 自动同步仓库中的生成文件

它不是通用规则编辑器，也不是手工维护的大杂烩规则仓库。这里的核心价值是把一组外部规则源稳定转换成当前项目需要的 sing-box 产物。

## 仓库结构

```text
.
├── .github/workflows/sync.yml   # 测试 + 生成 + 提交的 CI
├── links.txt                    # 远程规则源列表
├── main.py                      # 转换脚本
├── tests/                       # pytest 测试
├── rule/                        # 生成产物目录
└── custom-rule/                 # 手工维护的自定义规则
```

### `rule/` 目录中的产物

每个远程规则源最多生成三类 JSON，并分别编译成对应 `.srs`：

- `Name.json`
  - 通用 rule-set
  - 保留所有支持且未废弃的字段
- `DNS_Name_domain.json`
  - DNS 专用 rule-set
  - 只保留 `domain` / `domain_suffix` / `domain_keyword` / `domain_regex`
- `DNS_Name_ipcidr.json`
  - DNS 专用 rule-set
  - 只保留 `ip_cidr`

脚本还会维护：

- `rule/.generated-manifest.json`
  - 记录当前脚本管理的生成文件
  - 用于清理不再需要的旧 `.json` / `.srs`

## 为什么要拆分 DNS 专用产物

这是这个仓库和普通“规则格式转换”脚本最大的差别。

在 sing-box 中：

- `route rule` 对 `domain` 和 `ip_cidr` 的处理更接近普通匹配器语义
- `dns rule` 存在“先选候选 DNS server，再根据响应地址决定是否接受结果”的两阶段行为

因此，把 `domain` 和 `ip_cidr` 混在同一个 rule-set 里，同时拿给 DNS 和 Route 模块使用，语义并不稳定。

这个仓库采用的处理方式是：

- 通用输出继续保留完整字段，用于 Route 场景
- DNS 输出按用途拆分成 domain 专用和 `ip_cidr` 专用

这样能避免在 DNS 模块里把不同类别字段混成一条语义暧昧的规则。

## 输入格式

`links.txt` 中每行一个远程规则地址，空行和以 `#` 开头的行会被忽略。

当前脚本覆盖以下输入形态：

- Clash `.list` 风格逐行规则
- 含 `payload` 的 YAML
- 纯文本域名 / IP / CIDR 列表

支持转换的字段包括：

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

以下内容不会进入输出：

- 已废弃字段，如 `geoip`、`geosite`、`source_geoip`
- 复杂逻辑表达式，如 `AND` / `OR` / `NOT`
- 不支持或无效的条目

这些输入会被脚本记录 warning，然后跳过，而不是静默产出错误规则。

## 生成规则

对 `links.txt` 中的每个 URL，脚本使用 URL basename 作为输出 stem。例如：

```text
https://example.com/Foo.list
```

会生成以下之一：

1. 只有 domain 类字段时：

```text
Foo.json
Foo.srs
```

2. 只有 `ip_cidr` 时：

```text
Foo.json
Foo.srs
```

3. 只有非 DNS 专用字段时：

```text
Foo.json
Foo.srs
```

4. 有 domain 类字段，且无 `ip_cidr`，但存在其他字段时：

```text
Foo.json
Foo.srs
DNS_Foo_domain.json
DNS_Foo_domain.srs
```

5. 有 `ip_cidr`，且无 domain 类字段，但存在其他字段时：

```text
Foo.json
Foo.srs
DNS_Foo_ipcidr.json
DNS_Foo_ipcidr.srs
```

6. 同时有 domain 类字段和 `ip_cidr` 时：

```text
Foo.json
Foo.srs
DNS_Foo_domain.json
DNS_Foo_domain.srs
DNS_Foo_ipcidr.json
DNS_Foo_ipcidr.srs
```

## 本地使用

### 依赖

- Python 3.12+
- `requests`
- `pyyaml`
- `pytest`
- `sing-box 1.13.0`

### 安装依赖

```bash
python -m pip install requests pyyaml pytest
```

### 运行测试

```bash
python -m pytest -q
```

### 手动生成规则

```bash
python main.py --links links.txt --output-dir rule --sing-box-bin sing-box
```

参数说明：

- `--links`
  - 输入链接文件，默认 `links.txt`
- `--output-dir`
  - 输出目录，默认 `rule`
- `--sing-box-bin`
  - `sing-box` 可执行文件路径，默认 `sing-box`

## GitHub Actions

工作流位于 [sync.yml](/Users/wangyg/Development/Working/sing-box-geosite/.github/workflows/sync.yml)。

CI 行为如下：

1. checkout 仓库
2. 安装 Python
3. 安装测试和运行依赖
4. 安装 `sing-box 1.13.0`
5. 运行 `pytest`
6. 执行生成脚本
7. 提交 `rule/` 目录下的更新内容

触发方式：

- push 到 `main`
- 手动触发 `workflow_dispatch`
- 定时任务

## 自定义规则

`custom-rule/` 下的文件是手工维护内容，不受 `main.py` 的自动清理逻辑影响。

自动清理只会删除：

- 之前由脚本生成
- 当前已不再需要
- 并且被 `rule/.generated-manifest.json` 记录过的产物

这意味着脚本不会碰：

- `links.txt`
- `custom-rule/`
- 其他手工文件

## 测试策略

当前测试覆盖两类行为：

- 单元测试
  - 解析输入
  - 字段归一化
  - DNS 拆分逻辑
  - Source Format v4 输出
- 集成测试
  - 生成文件
  - manifest 写入
  - stale 文件清理
  - `sing-box` 编译调用的 mock 验证

测试样本使用自造的小型 fixture，不依赖大型真实规则文件。

## 注意事项

- 这个仓库现在以 **生成产物** 为中心，不再维护根目录下的历史 JSON 示例文件。
- 如果你修改了 `links.txt`，重新运行脚本后，`rule/` 中的产物集合可能变化，旧产物会被自动清理。
- 如果你要扩展支持新的规则类型，先补测试，再改 `main.py`。不要把新行为直接塞进脚本里赌结果。

## 致谢

规则源和思路参考了以下项目与社区贡献：

- [blackmatrix7](https://github.com/blackmatrix7)
- [izumiChan16](https://github.com/izumiChan16)
- [ifaintad](https://github.com/ifaintad)
- [NobyDa](https://github.com/NobyDa)
- [DivineEngine](https://github.com/DivineEngine)
