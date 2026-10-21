# tools/ · 打包脚本

一个文件，解决一个具体问题：**让插件能被 AstrBot 正确安装**。

## 为什么需要它

AstrBot 装插件时会解压 zip，然后找里面那个目录。这有两个硬要求：

**1. 必须是单层目录，且名字与 `metadata.yaml` 的 `name` 完全一致**

```
✅ astrbot_plugin_qinyu/metadata.yaml
❌ astrbot_plugin_qinyu-v1.2.2/metadata.yaml     （名字多了版本号）
❌ astrbot_plugin_qinyu/metadata.yaml 在根目录  （没有外层目录）
```

**2. 条目分隔符必须是正斜杠 `/`**

Windows 右键「压缩」和 PowerShell 的 `Compress-Archive` 会把条目写成反斜杠：

```
❌ astrbot_plugin_qinyu\main.py
```

Linux 容器里解压后，你会得到一个**文件名里带反斜杠的单文件**，目录根本不存在。安装时报错：

```
FileNotFoundError: [Errno 2] No such file or directory:
'.../plugin/astrbot_plugin_qinyu'
```

这个错很难查——因为 zip 在 Windows 里打开看着完全正常。

## 用法

```bash
cd astrbot_plugin_qinyu
python tools/build_plugin_zip.py                    # 输出到上级目录
python tools/build_plugin_zip.py D:/out/my.zip      # 指定输出路径
```

## 它做了什么

1. 读 `metadata.yaml` 拿到 `name`，作为 zip 内的根目录名
2. 递归收集文件，**排除** `__pycache__/`、`*.pyc`、`.git/`、`data/`、`*.zip`
3. 用 `zipfile` 逐个写入，路径统一转成正斜杠
4. **模拟 AstrBot 的安装逻辑验证一遍**：解压到临时目录，检查目标目录是否存在、必需文件是否齐全
5. 打印验证结论，不通过就报错退出

最后一步是关键——很多打包脚本只管生成，不管能不能装。

## 输出示例

```
[verify] metadata 条目：astrbot_plugin_qinyu/metadata.yaml
[verify] 必需文件齐全：13 项 OK
[verify] 结论：可被 AstrBot 正常解压安装
```

## 必需文件清单

脚本会检查这 13 项，缺一个就失败：

```
metadata.yaml  __init__.py  main.py  _conf_schema.json
README.md  LICENSE  logo.png
engines/__init__.py  engines/base.py
engines/mimo/__init__.py  engines/mimo/v2_5.py
views/singing/index.html
tools/build_plugin_zip.py
```

加新目录时记得同步更新这份清单——不然漏了文件，包能装上但运行时才报错。
