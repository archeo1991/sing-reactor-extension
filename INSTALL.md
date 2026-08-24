# 声迹浏览器扩展安装说明

这份说明面向普通用户。你只需要安装浏览器扩展；歌词识别服务由维护者部署在服务器上，扩展包里已经写好了服务地址。

## 1. 解压扩展包

下载并解压：

```text
sing-reactor-extension-<version>.zip
```

解压后会得到一个扩展目录，里面应包含：

```text
manifest.json
config.js
background.js
content.js
protocol.js
storage.js
popup.html
logo.png
INSTALL.md
```

不要把 zip 文件本身直接拖进浏览器，也不要选择 zip 的上一级目录。

## 2. 安装到 Chrome 或 Edge

Chrome：

```text
chrome://extensions
```

Edge：

```text
edge://extensions
```

1. 打开扩展管理页面。
2. 开启“开发者模式”。
3. 点击“加载已解压的扩展程序”。
4. 选择解压出的 `sing-reactor-extension-<version>` 文件夹。
5. 看到“声迹”出现在扩展列表中即安装完成。

## 3. 使用

1. 打开 B站视频或番剧播放页面。
2. 页面右侧会出现声迹面板。
3. 设置歌曲开始和结束时间。
4. 点击“获取歌词”。
5. 获取完成后，可以点击歌词跳转、逐句暂停、循环练习或标记掌握。

## 常见问题

### 扩展提示无法连接声迹服务

请确认当前网络可访问维护者提供的声迹服务。如果仍然失败，请把错误提示和当前页面链接发给服务维护者。

### Chrome 无法加载扩展

请确认选择的是解压后的扩展文件夹，并且该文件夹内有 `manifest.json`。
